// Slack interactivity URL. Verifies the signature, checks the allow-list, decides the approval
// exactly once, then dispatches the next workflow. Must answer Slack within 3 seconds.
import { db, dispatchGithub } from "../_shared/db.ts";

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

// action_id -> approval outcome, minimum role, and the workflow event fired on approve
const ACTIONS: Record<
  string,
  { status: "approved" | "rejected"; kind: string; event?: string; role: string }
> = {
  accept: { status: "approved", kind: "lead", event: "lead-accepted", role: "approver" },
  ignore: { status: "rejected", kind: "lead", role: "approver" },
  send_email: { status: "approved", kind: "email", event: "email-approved", role: "approver" },
  skip_email: { status: "rejected", kind: "email", role: "approver" },
  send_reply: { status: "approved", kind: "reply", event: "reply-approved", role: "approver" },
  skip_reply: { status: "rejected", kind: "reply", role: "approver" },
  approve_post: { status: "approved", kind: "post", event: "post-approved", role: "approver" },
  request_changes: { status: "rejected", kind: "post", role: "approver" },
};
const RANK: Record<string, number> = { viewer: 0, approver: 1, admin: 2 };

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const raw = await req.text();
  if (!(await verify(req, raw))) return new Response("invalid signature", { status: 401 });

  const payload = JSON.parse(new URLSearchParams(raw).get("payload") ?? "{}");
  const act = payload.actions?.[0];
  const spec = act && ACTIONS[act.action_id];
  if (!spec) return new Response("", { status: 200 }); // unknown action: ignore

  const slackUser: string = payload.user?.id;
  const { data: member } = await db.from("team_members").select("name, role, active")
    .eq("slack_user_id", slackUser).maybeSingle();
  if (!member?.active || RANK[member.role] < RANK[spec.role]) {
    return Response.json({ response_type: "ephemeral", text: "You are not allowed to do that." });
  }

  // Exactly-once: only succeeds while the approval is still pending and unexpired.
  const { data: decided, error } = await db.rpc("decide_approval", {
    p_id: act.value,
    p_status: spec.status,
    p_by: slackUser,
  });
  if (error) return new Response("error", { status: 500 });
  if (!decided?.id) {
    return Response.json({ response_type: "ephemeral", text: "Already decided or expired." });
  }
  if (decided.kind !== spec.kind) return new Response("kind mismatch", { status: 400 });

  if (spec.event && spec.status === "approved") {
    try {
      await dispatchGithub(spec.event, { approval_id: decided.id, ref_id: decided.ref_id });
    } catch (_) {
      // Decision is recorded; the daily sweep and the Retry button pick it up.
    }
  }

  // Replace buttons with the outcome.
  const verb = spec.status === "approved" ? "Approved" : "Rejected";
  if (payload.response_url) {
    fetch(payload.response_url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ replace_original: true, text: `${verb} by ${member.name}` }),
    }).catch(() => {});
  }
  return new Response("", { status: 200 });
});
