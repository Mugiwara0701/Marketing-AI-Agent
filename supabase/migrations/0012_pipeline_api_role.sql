-- A database login for the hosted pipeline API (api/, e.g. on Render) that can do exactly two things: read the
-- pipeline_control row and call request_pipeline(). No other table, no direct writes. If the API host or its
-- DATABASE_URL leaks, leads, emails and contacts stay out of reach.
-- The role is created without a password; `python -m agent api-credentials` sets one and prints the URL to use.

do $$ begin
  create role pipeline_api nologin;
exception when duplicate_object then null;
end $$;
alter role pipeline_api connection limit 10;  -- the API's pool uses at most 4

grant usage on schema public to pipeline_api;
grant select on pipeline_control to pipeline_api;
-- RLS is on with no policies (0011): without this policy the role would see no row.
drop policy if exists pipeline_api_read on pipeline_control;
create policy pipeline_api_read on pipeline_control for select to pipeline_api using (true);

-- Same function as in 0011, now with the owner's rights: it writes the row on the caller's behalf, so the role
-- needs no update grant. search_path is pinned, as for any security definer function.
create or replace function request_pipeline(p_state text, p_by text)
returns pipeline_control
language plpgsql
security definer
set search_path = public, pg_temp
as $$
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
-- Functions are executable by everyone by default, and Supabase exposes public functions to its anon and
-- authenticated API roles: nobody but the API role (and the owner) may start or stop the pipeline.
revoke execute on function request_pipeline(text, text) from public;
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke execute on function request_pipeline(text, text) from %I', r);
      execute format('revoke all on pipeline_control from %I', r);
    end if;
  end loop;
end $$;
grant execute on function request_pipeline(text, text) to pipeline_api;
