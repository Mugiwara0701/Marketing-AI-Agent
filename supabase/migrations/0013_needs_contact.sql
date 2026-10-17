-- Qualified leads the agent found no contact for are kept (NEEDS_CONTACT) instead of rejected: the dashboard lists them,
-- a person types an email, and the agent drafts the email and asks for approval in Slack like for any other lead.
-- Additive: one more allowed lead_status, a one-time backfill, and one function the dashboard backend calls.

alter table companies drop constraint if exists companies_lead_status_check;
alter table companies add constraint companies_lead_status_check check (lead_status in (
  'DISCOVERED', 'QUALIFIED', 'NEEDS_CONTACT', 'CONTACT_FOUND', 'EMAIL_DRAFTED', 'PENDING_APPROVAL',
  'APPROVED', 'REJECTED', 'SENT', 'FAILED'));

-- Leads that gave up on contact discovery before this change (rejected after the last attempt, or a form-only company
-- left QUALIFIED): they have no contact and no email, so they belong in the list too.
update companies co set
    lead_status = 'NEEDS_CONTACT',
    status = case when co.status in ('suppressed', 'engaged', 'closed') then co.status else 'qualified' end,
    updated_at = now()
where co.contact_attempts >= 3
  and (co.lead_status = 'QUALIFIED' or (co.lead_status = 'REJECTED' and co.status_note like 'no contact after%'))
  and not exists (select 1 from contacts as c where c.company_id = co.id and c.email is not null);

-- The one way an email typed on the dashboard gets onto a lead. Only a NEEDS_CONTACT lead accepts it, exactly once:
-- it stores the contact (source 'manual:dashboard', which the agent recognises) and moves the lead to CONTACT_FOUND.
-- The agent then drafts the email and posts the approval request to Slack (it never sends anything by itself).
-- Returns ok | invalid | unknown | not_waiting | suppressed.
create or replace function submit_manual_contact(
  p_company uuid, p_email text, p_name text default null, p_role text default null, p_by text default null
) returns text
language plpgsql
security definer
set search_path = public, extensions, pg_temp  -- extensions: pgcrypto's digest(), used by is_suppressed()
as $$
declare
  v_email text := lower(btrim(coalesce(p_email, '')));
  co companies;
begin
  if length(v_email) > 254 or v_email !~ '^[^@[:space:],;<>"]+@[^@[:space:],;<>"]+\.[^@[:space:],;<>"]+$' then
    return 'invalid';
  end if;
  select * into co from companies where id = p_company for update;
  if not found then return 'unknown'; end if;
  if co.lead_status <> 'NEEDS_CONTACT' then return 'not_waiting'; end if;
  if is_suppressed(v_email) then return 'suppressed'; end if;
  insert into contacts (company_id, name, role, email, source_url, confidence, rank, verification)
  values (p_company, left(nullif(btrim(p_name), ''), 120), left(nullif(btrim(p_role), ''), 120), v_email,
          'manual:dashboard', 1, 0, 'manual')
  on conflict (company_id, email) do update
    set name = coalesce(excluded.name, contacts.name), role = coalesce(excluded.role, contacts.role),
        source_url = 'manual:dashboard', confidence = 1, rank = 0, verification = 'manual', collected_at = now();
  update companies
     set lead_status = 'CONTACT_FOUND', status = 'contact_found',
         status_note = left('contact added on the dashboard by ' || coalesce(nullif(btrim(p_by), ''), 'a team member'), 300),
         updated_at = now()
   where id = p_company;
  return 'ok';
end $$;

-- Not callable through the public API (anon / authenticated): only the database logins the dashboard backend and the
-- agent use.
revoke execute on function submit_manual_contact(uuid, text, text, text, text) from public;
do $$ declare r text; begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke execute on function submit_manual_contact(uuid, text, text, text, text) from %I', r);
    end if;
  end loop;
end $$;
