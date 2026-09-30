// Dashboard "Start run" and Slack "Retry": authenticated team members only (Supabase JWT).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { db, dispatchGithub } from "../_shared/db.ts";

// Only these service/job pairs can be started from outside (dashboard Start run, Slack Retry).
const ALLOWED = new Set([
  "lead/collect_sources", "lead/ingest_alerts", "lead/qualify", "lead/enrich_contacts",
  "outreach/draft", "outreach/poll_replies", "content/plan_topics", "content/draft_post",
]);

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const jwt = (req.headers.get("Authorization") ?? "").replace("Bearer ", "");
  const userClient = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!);
  const { data: u } = await userClient.auth.getUser(jwt);
  if (!u?.user) return new Response("unauthorized", { status: 401 });

  const { data: member } = await db.from("team_members").select("role, active")
    .eq("auth_user_id", u.user.id).maybeSingle();
  if (!member?.active || !["approver", "admin"].includes(member.role)) {
    return new Response("forbidden", { status: 403 });
  }

  const { service, job } = await req.json().catch(() => ({}));
  if (!ALLOWED.has(`${service}/${job}`)) return new Response("unknown job", { status: 400 });
  // Handled by the `run-job` event in .github/workflows/dispatch.yml.
  await dispatchGithub("run-job", { service, job, requested_by: u.user.id });
  return Response.json({ ok: true }, { status: 202 });
});
