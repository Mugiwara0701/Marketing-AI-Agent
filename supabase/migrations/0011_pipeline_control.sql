-- Dashboard Start / Stop. The office machine is not reachable from the internet, so nothing calls it: the dashboard
-- writes the state it wants here (REST API in api/ -> request_pipeline()), and the agent polls this row every
-- few seconds, applies it and writes back what it is doing plus a heartbeat (agent/control.py).
--   desired_*   written by the dashboard (or `python -m agent pipeline start|stop`)
--   the rest    written by the agent; heartbeat_at older than a minute (or null) = the agent is offline

create table if not exists pipeline_control (
  id int primary key default 1 check (id = 1),  -- one row: one pipeline
  desired_state text not null default 'stopped' check (desired_state in ('running', 'stopped')),
  requested_by text,
  requested_at timestamptz,
  actual_state text not null default 'stopped' check (actual_state in ('running', 'stopped')),
  state_since timestamptz,
  current_step text,
  current_pass_started_at timestamptz,
  next_pass_at timestamptz,
  last_pass jsonb,
  sending_enabled boolean,
  test_mode boolean,
  agent_host text,
  heartbeat_at timestamptz,
  updated_at timestamptz not null default now()
);
insert into pipeline_control (id) values (1) on conflict (id) do nothing;
alter table pipeline_control enable row level security;  -- no policies: service role and the agent only

create or replace function request_pipeline(p_state text, p_by text)
returns pipeline_control language plpgsql as $$
declare
  r pipeline_control;
begin
  if p_state not in ('running', 'stopped') then
    raise exception 'pipeline state must be running or stopped, not %', p_state;
  end if;
  update pipeline_control
     set desired_state = p_state, requested_by = left(coalesce(p_by, 'unknown'), 200), requested_at = now(),
         updated_at = now()
   where id = 1
  returning * into r;
  return r;
end $$;
