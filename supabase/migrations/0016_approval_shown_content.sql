-- An approval covers exactly what its Slack card showed. Additive and safe to run again.
--
-- 0014 bound an approval to the recipient, subject and body at the moment of the click. If a draft changed between the
-- card being posted and the click, the person would approve a text they never saw. Now the pending request records
-- the fingerprint of what the card shows (shown_sha256, written when the card is posted), and decide_email() refuses
-- a decision when the draft no longer matches it: nothing is decided, the request is closed, the lead goes back to
-- EMAIL_DRAFTED and the agent posts the draft as it is now. A request without a recorded fingerprint (older cards,
-- follow-ups) is decided as before, and the sending gate still compares the hash stored at the decision.

alter table approvals add column if not exists shown_sha256 text;

-- Returns approved | rejected | already_decided | expired | changed | unknown.
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
  v_shown text;
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
  select a.shown_sha256 into v_shown from approvals as a
   where a.kind = 'email' and a.ref_id = p_email and a.status = 'pending';
  if v_shown is not null and v_shown <> v_hash then
    update approvals set status = 'expired' where kind = 'email' and ref_id = p_email and status = 'pending';
    update companies set lead_status = 'EMAIL_DRAFTED', updated_at = now()
     where id = v_company and lead_status = 'PENDING_APPROVAL';
    return 'changed';
  end if;
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
