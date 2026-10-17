-- A limited database login for the Marketing Agent service (audit 2026-10-17, F-06). Until now the service logged in as
-- the database owner, so a leaked .env gave full control of every table, including the Campaign Agent's publish
-- functions and the dashboard's data. This role can use the Marketing Agent's own tables and functions, report to the
-- Campaign Agent's ma_campaign_status row, and nothing else.
--
-- After applying: set a password (`alter role marketing_agent with login password '...'`), put the pooler URL with
-- this user in the Marketing Agent's DATABASE_URL, and keep the owner URL only where migrations are run.

do $$ begin
  create role marketing_agent nologin;
exception when duplicate_object then null;
end $$;

alter role marketing_agent connection limit 20;
alter role marketing_agent set idle_in_transaction_session_timeout = '60s';
alter role marketing_agent set lock_timeout = '30s';

grant usage on schema public to marketing_agent;

-- The agent's own tables: read and write. Row level security is on for most of them, so each also gets a policy.
do $$
declare t text;
begin
  foreach t in array array[
    'companies', 'lead_signals', 'contacts', 'campaigns', 'emails', 'replies', 'topics', 'content_posts',
    'content_variants', 'approvals', 'suppression_list', 'agent_runs', 'send_counters', 'email_events',
    'search_queries', 'examples', 'knowledge_docs', 'knowledge_chunks', 'prompt_versions', 'retrieval_logs'
  ] loop
    if to_regclass('public.' || t) is not null then
      execute format('grant select, insert, update, delete on public.%I to marketing_agent', t);
      execute format('drop policy if exists marketing_agent_all on public.%I', t);
      execute format(
        'create policy marketing_agent_all on public.%I for all to marketing_agent using (true) with check (true)', t
      );
    end if;
  end loop;
end $$;

grant usage, select on all sequences in schema public to marketing_agent;

-- Read only: who may approve in Slack.
grant select on public.team_members to marketing_agent;
drop policy if exists marketing_agent_read on public.team_members;
create policy marketing_agent_read on public.team_members for select to marketing_agent using (true);

-- The pipeline switch: the agent reads what is wanted and reports what it does.
grant select, update on public.pipeline_control to marketing_agent;
drop policy if exists marketing_agent_all on public.pipeline_control;
create policy marketing_agent_all on public.pipeline_control
for all to marketing_agent using (true) with check (true);

-- What the agent runs, for the Campaign Agent (its table; present once the Campaign Agent's 0016 is applied).
do $$ begin
  if to_regclass('public.ma_campaign_status') is not null then
    grant select, insert, update on public.ma_campaign_status to marketing_agent;
    drop policy if exists marketing_agent_all on public.ma_campaign_status;
    create policy marketing_agent_all on public.ma_campaign_status
    for all to marketing_agent using (true) with check (true);
  end if;
end $$;

-- Functions the agent calls.
grant execute on function public.decide_email(uuid, boolean, text, text) to marketing_agent;
grant execute on function public.expire_approvals() to marketing_agent;
grant execute on function public.purge_stale_contacts() to marketing_agent;
grant execute on function public.is_suppressed(text) to marketing_agent;
grant execute on function public.try_increment_send(text, int) to marketing_agent;
grant execute on function public.email_hash(text) to marketing_agent;
grant execute on function public.lead_name_key(text) to marketing_agent;
grant execute on function public.email_content_sha256(text, text, text) to marketing_agent;
grant execute on function public.request_pipeline(text, text) to marketing_agent;
