-- Approvals bound to the exact email, campaign attribution, expiry, and test sends kept apart (audit 2026-10-17).
-- Additive and safe to run again.
--
-- 1. An approval records a hash of what the person saw: recipient, subject and body. The sending gate compares it with
--    the email as it is now, so a change after the approval (or a different recipient) is never sent under it.
-- 2. Once an email is decided, its subject, body and contact can no longer change; a contact's address can no longer
--    change while an approved email to it waits.
-- 3. Leads and emails remember the Campaign Agent campaign (and version) they were found for.
-- 4. A pending approval that expired can no longer be decided; the lead goes back to EMAIL_DRAFTED and is posted again.
-- 5. A mail redirected to TEST_RECIPIENT is 'test_sent', not 'sent': the real contact never got it.

-- 1. The hash. sha256() is built into Postgres 11+, so no extension (and no extensions schema) is needed.
create or replace function email_content_sha256(p_to text, p_subject text, p_body text)
returns text
language sql
immutable
as $$
  select encode(sha256(convert_to(
    lower(btrim(coalesce(p_to, ''))) || chr(10) || coalesce(p_subject, '') || chr(10) || coalesce(p_body, ''),
    'UTF8'
  )), 'hex')
$$;

alter table approvals add column if not exists content_sha256 text;

-- Emails approved before this migration and not sent yet: their approval covers the text as it is now.
update approvals as a set content_sha256 = email_content_sha256(c.email, e.subject, e.body)
from emails as e
inner join contacts as c on e.contact_id = c.id
where
  a.kind = 'email' and a.ref_id = e.id and a.status = 'approved' and a.content_sha256 is null
  and e.status in ('approved', 'sending');

-- 2. Frozen content.
create or replace function emails_freeze_decided() returns trigger
language plpgsql as $$
begin
  if old.status <> 'drafted' and (
    new.subject is distinct from old.subject
    or new.body is distinct from old.body
    or new.contact_id is distinct from old.contact_id
  ) then
    raise exception 'email % was already decided; its subject, body and recipient cannot change', old.id;
  end if;
  return new;
end $$;

drop trigger if exists emails_freeze_decided on emails;
create trigger emails_freeze_decided before update on emails
for each row execute function emails_freeze_decided();

create or replace function contacts_freeze_approved() returns trigger
language plpgsql as $$
begin
  if new.email is distinct from old.email and exists (
    select 1 from emails as e where e.contact_id = old.id and e.status in ('approved', 'sending')
  ) then
    raise exception 'contact % has an approved email waiting; its address cannot change', old.id;
  end if;
  return new;
end $$;

drop trigger if exists contacts_freeze_approved on contacts;
create trigger contacts_freeze_approved before update on contacts
for each row execute function contacts_freeze_approved();

-- 3. Campaign attribution (the Campaign Agent's public.campaign id; no foreign key: that table belongs to the dashboard).
alter table companies add column if not exists source_campaign_id uuid;
alter table companies add column if not exists source_campaign_version int;
alter table emails add column if not exists source_campaign_id uuid;
alter table emails add column if not exists source_campaign_version int;
create index if not exists emails_source_campaign_idx on emails (source_campaign_id) where status = 'approved';

-- 5. Test sends.
alter table emails drop constraint if exists emails_status_check;
alter table emails add constraint emails_status_check check (
  status in ('drafted', 'approved', 'sending', 'sent', 'test_sent', 'skipped', 'bounced', 'expired')
);

-- 4. Expiry: called by the agent's sender loop. An intro whose approval expired is posted again by the next pass.
create or replace function expire_approvals() returns int
language plpgsql as $$
declare n int;
begin
  with gone as (
    update approvals set status = 'expired'
    where status = 'pending' and expires_at <= now()
    returning kind, ref_id
  ),

  intro as (
    update companies as co set lead_status = 'EMAIL_DRAFTED', updated_at = now()
    from gone as g
    inner join emails as e on g.ref_id = e.id
    inner join contacts as c on e.contact_id = c.id
    where g.kind = 'email' and co.id = c.company_id and co.lead_status = 'PENDING_APPROVAL' and e.status = 'drafted'
    returning co.id
  )

  select count(*)::int into n from gone;
  return n;
end $$;

-- The one way an email draft is decided (replaces the version from 0007). Exactly once; never on an expired request;
-- the approval records the hash of the recipient, subject and body at the moment of the decision.
-- Returns approved | rejected | already_decided | expired | unknown.
create or replace function decide_email(p_email uuid, p_approve boolean, p_by text, p_note text default null)
returns text
language plpgsql
set search_path = public, pg_temp
as $$
declare
  e emails;
  v_to text;
  v_company uuid;
  v_hash text;
  v_decision text := case when p_approve then 'approved' else 'rejected' end;
begin
  select * into e from emails where id = p_email for update;
  if not found then return 'unknown'; end if;
  if e.status <> 'drafted' then return 'already_decided'; end if;
  -- An expired request with no newer pending one: the card in Slack is stale. A new one is posted by the agent.
  if exists (select 1 from approvals where kind = 'email' and ref_id = p_email and status = 'pending' and expires_at <= now())
     or (exists (select 1 from approvals where kind = 'email' and ref_id = p_email and status = 'expired')
         and not exists (select 1 from approvals where kind = 'email' and ref_id = p_email and status = 'pending')) then
    perform expire_approvals();
    return 'expired';
  end if;
  select c.email, c.company_id into v_to, v_company from contacts as c where c.id = e.contact_id;
  v_hash := email_content_sha256(v_to, e.subject, e.body);
  update emails set status = case when p_approve then 'approved' else 'skipped' end, updated_at = now()
   where id = p_email;
  update approvals set status = v_decision, decided_by = p_by, decided_at = now(), decision_note = p_note,
                       content_sha256 = v_hash
   where kind = 'email' and ref_id = p_email and status = 'pending';
  if not found then
    insert into approvals (kind, ref_id, status, decided_by, decided_at, decision_note, content_sha256)
    values ('email', p_email, v_decision, p_by, now(), p_note, v_hash);
  end if;
  if e.step = 1 and e.campaign_id = (select id from campaigns where name = 'daily-outreach') then
    update companies
       set lead_status = case when p_approve then 'APPROVED' else 'REJECTED' end, updated_at = now()
     where id = v_company and lead_status in ('EMAIL_DRAFTED', 'PENDING_APPROVAL');
  end if;
  return v_decision;
end $$;
