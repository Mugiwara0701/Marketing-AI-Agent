// Slack interactivity endpoint (Request URL). Verifies Slack's signature, checks who clicked, records the
// decision exactly once. Emails go through the SQL function decide_email() (migration 0007): it moves the email,
// records who approved or rejected it in `approvals`, and moves an intro's lead to APPROVED / REJECTED. The mailer
// only ever sends emails with such a recorded approval. Must answer Slack within 3 seconds.
import { db } from "../_shared/db.ts";

const enc = new TextEncoder();

async function verify(req: Request, body: string): Promise<boolean> {
  const ts = req.headers.get("x-slack-request-timestamp") ?? "";
  const sig = req.headers.get("x-slack-signature") ?? "";
  if (!ts || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false; // older than 5 min
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(Deno.env.get("SLACK_SIGNING_SECRET")!),
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
const ACTIONS: Record<
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
async function allowed(slackUser: string): Promise<string | null> {
  const list = (Deno.env.get("SLACK_ALLOWED_USERS") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  if (list.includes(slackUser)) return slackUser;
  const { data } = await db.from("team_members").select("name, role, active").eq("slack_user_id", slackUser)
    .maybeSingle();
  return data?.active && ["approver", "admin"].includes(data.role) ? data.name : null;
}

const ephemeral = (text: string) => Response.json({ response_type: "ephemeral", text });

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const raw = await req.text();
  if (!(await verify(req, raw))) return new Response("invalid signature", { status: 401 });

  const payload = JSON.parse(new URLSearchParams(raw).get("payload") ?? "{}");
  const act = payload.actions?.[0];
  const spec = act && ACTIONS[act.action_id];
  if (!spec) return new Response("", { status: 200 }); // unknown action: ignore

  const who = await allowed(payload.user?.id ?? "");
  if (!who) return ephemeral("You are not allowed to approve. Ask an admin to add your Slack user id.");

  // Button value is "<kind>:<row id>". Nothing about Slack is stored: the decision is written onto the row.
  const [kind, refId] = String(act.value ?? "").split(":");
  if (kind !== spec.kind || !/^[0-9a-f-]{36}$/.test(refId ?? "")) {
    return new Response("bad value", { status: 400 });
  }

  if (spec.kind === "email") {
    const { data: result, error } = await db.rpc("decide_email", {
      p_email: refId,
      p_approve: spec.status === "approved",
      p_by: `slack:${payload.user?.id ?? "unknown"} (${who})`,
    });
    if (error) return new Response("error", { status: 500 });
    if (result === "already_decided") return ephemeral("Already decided.");
    if (result === "unknown") return ephemeral("This draft no longer exists.");
  } else {
    // Exactly once: the update only matches while the row is still undecided.
    const { data: changed, error } = await db.from(spec.table).update({ status: spec.to })
      .eq("id", refId).in("status", spec.from).select("id");
    if (error) return new Response("error", { status: 500 });
    if (!changed?.length) return ephemeral("Already decided.");
  }

  const verb = spec.status === "approved" ? "Approved" : "Rejected";
  if (payload.response_url) {
    fetch(payload.response_url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ replace_original: true, text: `${verb} by ${who}` }),
    }).catch(() => {});
  }
  return new Response("", { status: 200 });
});
