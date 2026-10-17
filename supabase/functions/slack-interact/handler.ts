// The Slack interactivity logic, apart from Deno.serve so it can be tested (handler_test.ts) with a fake database.
// Verifies Slack's signature, checks who clicked, records the decision exactly once. Emails go through the SQL
// function decide_email(): it moves the email, records who decided and the hash of what was approved in
// `approvals`, and moves an intro's lead to APPROVED / REJECTED. The mailer only sends emails with such a record.
import type { SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";

export type Db = Pick<SupabaseClient, "rpc" | "from">;

export interface Deps {
  db: Db;
  env: (name: string) => string | undefined;
  now?: () => number; // milliseconds, as Date.now()
  notify?: (url: string, text: string) => void; // replaces the Slack message after a decision
}

const enc = new TextEncoder();
export const MAX_AGE_S = 300; // Slack's own advice: refuse requests older than 5 minutes (replays)

// Slack's v0 signature over "v0:<timestamp>:<raw body>". Fails closed: no secret, no timestamp, a timestamp that is
// not a number or is too old or too new, or a wrong signature all return false.
export async function verify(
  secret: string | undefined,
  ts: string,
  sig: string,
  body: string,
  nowS: number,
): Promise<boolean> {
  if (!secret || !/^\d{1,12}$/.test(ts) || !sig) return false;
  if (Math.abs(nowS - Number(ts)) > MAX_AGE_S) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`v0:${ts}:${body}`));
  const expected = "v0=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
  if (expected.length !== sig.length) return false;
  let diff = 0; // constant-time compare
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ sig.charCodeAt(i);
  return diff === 0;
}

// action_id -> what it means. `table`/`from`/`to` is the state change applied to the referenced row.
export const ACTIONS: Record<
  string,
  {
    kind: "email" | "post" | "reply";
    status: "approved" | "rejected";
    table: string;
    from: string[];
    to: string;
  }
> = {
  approve_email: { kind: "email", status: "approved", table: "emails", from: ["drafted"], to: "approved" },
  skip_email: { kind: "email", status: "rejected", table: "emails", from: ["drafted"], to: "skipped" },
  // Replies: 'acknowledged' = approved; the daily agent then queues the draft as an email for the mailer.
  approve_reply: {
    kind: "reply",
    status: "approved",
    table: "replies",
    from: ["classified"],
    to: "acknowledged",
  },
  skip_reply: { kind: "reply", status: "rejected", table: "replies", from: ["classified"], to: "handled" },
  approve_post: {
    kind: "post",
    status: "approved",
    table: "content_posts",
    from: ["drafted", "in_review"],
    to: "approved",
  },
  reject_post: {
    kind: "post",
    status: "rejected",
    table: "content_posts",
    from: ["drafted", "in_review"],
    to: "rejected",
  },
};

// Who may decide: Slack user ids in SLACK_ALLOWED_USERS (comma separated), or active approver/admin rows
// in team_members.
export async function allowed(deps: Deps, slackUser: string): Promise<string | null> {
  if (!slackUser) return null;
  const list = (deps.env("SLACK_ALLOWED_USERS") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  if (list.includes(slackUser)) return slackUser;
  const { data } = await deps.db.from("team_members").select("name, role, active").eq(
    "slack_user_id",
    slackUser,
  ).maybeSingle();
  return data?.active && ["approver", "admin"].includes(data.role) ? data.name : null;
}

const ephemeral = (text: string) => Response.json({ response_type: "ephemeral", text });

export async function handle(req: Request, deps: Deps): Promise<Response> {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const raw = await req.text();
  const ok = await verify(
    deps.env("SLACK_SIGNING_SECRET"),
    req.headers.get("x-slack-request-timestamp") ?? "",
    req.headers.get("x-slack-signature") ?? "",
    raw,
    (deps.now ?? Date.now)() / 1000,
  );
  if (!ok) return new Response("invalid signature", { status: 401 });

  let payload: {
    actions?: { action_id?: string; value?: string }[];
    user?: { id?: string };
    response_url?: string;
  };
  try {
    payload = JSON.parse(new URLSearchParams(raw).get("payload") ?? "{}");
  } catch {
    return new Response("bad payload", { status: 400 });
  }
  const act = payload.actions?.[0];
  const spec = act?.action_id ? ACTIONS[act.action_id] : undefined;
  if (!spec) return new Response("", { status: 200 }); // unknown action: ignore

  const who = await allowed(deps, payload.user?.id ?? "");
  if (!who) return ephemeral("You are not allowed to approve. Ask an admin to add your Slack user id.");

  // Button value is "<kind>:<row id>". Nothing about Slack is stored: the decision is written onto the row.
  const [kind, refId] = String(act?.value ?? "").split(":");
  if (kind !== spec.kind || !/^[0-9a-f-]{36}$/.test(refId ?? "")) {
    return new Response("bad value", { status: 400 });
  }
  const by = `slack:${payload.user?.id ?? "unknown"} (${who})`;

  if (spec.kind === "email") {
    const { data: result, error } = await deps.db.rpc("decide_email", {
      p_email: refId,
      p_approve: spec.status === "approved",
      p_by: by,
    });
    if (error) return new Response("error", { status: 500 });
    if (result === "already_decided") return ephemeral("Already decided.");
    if (result === "unknown") return ephemeral("This draft no longer exists.");
    if (result === "expired") {
      return ephemeral("This request expired. The agent posts the draft again; decide on the new message.");
    }
    if (result === "changed") {
      return ephemeral(
        "This draft changed after this message was posted. Nothing was decided; decide on the new message.",
      );
    }
  } else {
    // Exactly once: the update only matches while the row is still undecided.
    const { data: changed, error } = await deps.db.from(spec.table).update({ status: spec.to })
      .eq("id", refId).in("status", spec.from).select("id");
    if (error) return new Response("error", { status: 500 });
    if (!changed?.length) return ephemeral("Already decided.");
    // The audit record: who decided. For a reply, the agent copies it onto the email it queues (agent/replies.py).
    const { error: recordError } = await deps.db.from("approvals").insert({
      kind: spec.kind,
      ref_id: refId,
      status: spec.status,
      decided_by: by,
      decided_at: new Date().toISOString(),
    });
    if (recordError) console.error("approval not recorded", spec.kind, refId, recordError.message);
  }

  const verb = spec.status === "approved" ? "Approved" : "Rejected";
  if (payload.response_url && deps.notify) deps.notify(payload.response_url, `${verb} by ${who}`);
  return new Response("", { status: 200 });
}
