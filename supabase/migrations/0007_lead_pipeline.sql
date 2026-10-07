-- Lead pipeline: lead state machine, evidence and scores on companies, contact roles, a search-query log, and one
-- atomic decision function for email approvals (used by the Slack Edge Function and the CLI). Additive.

alter table companies
  add column if not exists lead_status text not null default 'DISCOVERED',
  add column if not exists name_key text,
  add column if not exists product text,
  add column if not exists opportunity text,
  add column if not exists project_signal text,
  add column if not exists page_type text,
  add column if not exists lead_score int not null default 0,
  add column if not exists score jsonb,
  add column if not exists evidence jsonb not null default '[]'::jsonb,
  add column if not exists source_urls text[] not null default '{}',
  add column if not exists qualification_notes jsonb not null default '[]'::jsonb,
  add column if not exists status_note text;

do $$ begin
  alter table companies add constraint companies_lead_status_check check (lead_status in (
    'DISCOVERED', 'QUALIFIED', 'CONTACT_FOUND', 'EMAIL_DRAFTED', 'PENDING_APPROVAL',
    'APPROVED', 'REJECTED', 'SENT', 'FAILED'));
exception when duplicate_object then null;
end $$;

create index if not exists companies_lead_status_idx on companies (lead_status);
create index if not exists companies_name_key_idx on companies (name_key);

alter table contacts
  add column if not exists linkedin text,
  add column if not exists confidence numeric,
  add column if not exists rank int;

alter table emails add column if not exists last_error text;

create table if not exists search_queries (
  query text primary key,
  family text,
  engine text,
  results int,
  opened int,
  last_run_at timestamptz not null default now()
);
alter table search_queries enable row level security;

-- Same normalization as agent.leadgen.identity.name_key(): 'Acme EV Technologies Pvt. Ltd.' -> 'acmeev'.
create or replace function lead_name_key(p_name text) returns text
language plpgsql immutable as $$
declare
  legal text [] := array['inc','incorporated','ltd','limited','llc','llp','gmbh','ag','corp','corporation','co',
    'company','pvt','private','plc','sa','sas','srl','bv','nv','oy','ab','as','kk','pte','pty','the'];
  generic text [] := array['technologies','technology','tech','systems','solutions','group','labs','lab','global',
    'international','industries','electronics','devices','innovations'];
  words text [];
  core text [];
  distinctive text [];
begin
  words := regexp_split_to_array(lower(coalesce(p_name, '')), '[^a-z0-9]+');
  select coalesce(array_agg(w), '{}') into core from unnest(words) w where w <> '' and not (w = any(legal));
  select coalesce(array_agg(w), '{}') into distinctive from unnest(core) w where not (w = any(generic));
  if cardinality(distinctive) = 0 then distinctive := core; end if;
  return array_to_string(distinctive, '');
end $$;

update companies set name_key = lead_name_key(name) where name_key is null;

-- Existing rows get the lead status their legacy status and email imply.
update companies co set lead_status = case
    when co.status in ('rejected', 'suppressed') then 'REJECTED'
    when co.status in ('engaged', 'closed') then 'SENT'
    when exists (select 1 from contacts as c inner join emails as e on c.id = e.contact_id
                  where c.company_id = co.id and e.step = 1 and e.status = 'sent') then 'SENT'
    when exists (select 1 from contacts as c inner join emails as e on c.id = e.contact_id
                  where c.company_id = co.id and e.step = 1 and e.status = 'skipped') then 'REJECTED'
    when exists (select 1 from contacts as c inner join emails as e on c.id = e.contact_id
                  where c.company_id = co.id and e.step = 1) then 'PENDING_APPROVAL'
    when co.status = 'contact_found' then 'CONTACT_FOUND'
    else 'QUALIFIED'
  end
where co.lead_status = 'DISCOVERED';

-- Emails approved before approvals were recorded have no record of who approved them: back to review.
update emails e set status = 'drafted', updated_at = now()
where e.status = 'approved'
  and not exists (select 1 from approvals as a where a.kind = 'email' and a.ref_id = e.id and a.status = 'approved');

-- Email approvals stay decidable for 30 days.
alter table approvals alter column expires_at set default now() + interval '30 days';

-- The one way an email draft is approved or rejected. Exactly once: only a 'drafted' email can be decided.
-- Records who decided (approvals), moves the email, and for an intro moves the lead to APPROVED / REJECTED.
-- Returns approved | rejected | already_decided | unknown.
create or replace function decide_email(p_email uuid, p_approve boolean, p_by text, p_note text default null)
returns text language plpgsql as $$
declare
  e emails;
  v_company uuid;
  v_decision text := case when p_approve then 'approved' else 'rejected' end;
begin
  select * into e from emails where id = p_email for update;
  if not found then return 'unknown'; end if;
  if e.status <> 'drafted' then return 'already_decided'; end if;
  update emails set status = case when p_approve then 'approved' else 'skipped' end, updated_at = now()
   where id = p_email;
  update approvals set status = v_decision, decided_by = p_by, decided_at = now(), decision_note = p_note
   where kind = 'email' and ref_id = p_email and status = 'pending';
  if not found then
    insert into approvals (kind, ref_id, status, decided_by, decided_at, decision_note)
    values ('email', p_email, v_decision, p_by, now(), p_note);
  end if;
  if e.step = 1 and e.campaign_id = (select id from campaigns where name = 'daily-outreach') then
    select company_id into v_company from contacts where id = e.contact_id;
    update companies
       set lead_status = case when p_approve then 'APPROVED' else 'REJECTED' end, updated_at = now()
     where id = v_company and lead_status in ('EMAIL_DRAFTED', 'PENDING_APPROVAL');
  end if;
  return v_decision;
end $$;
