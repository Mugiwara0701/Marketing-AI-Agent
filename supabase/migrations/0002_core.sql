-- Core schema: leads, outreach, content, control. Status machines follow the Architecture doc, s7.
-- Services connect with the service_role key / pooled DB URL; RLS blocks everything else by default.

create extension if not exists pgcrypto;

-- ---------- Leads ----------
create table if not exists companies (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  domain text not null unique,
  country text,
  industry text,
  status text not null default 'candidate'
    check (status in ('candidate','qualified','contact_found','verified','engaged','rejected','suppressed','closed')),
  fit_score numeric,
  fit_reason text,
  source text,
  source_url text,
  needs_review boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists companies_status_idx on companies (status);

create table if not exists lead_signals (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references companies(id) on delete cascade,
  kind text not null,                 -- job_post | github | alert_email | manual
  source_url text,
  raw_text text,
  extracted jsonb,
  content_hash text unique,           -- dedup of the same signal
  created_at timestamptz not null default now()
);

create table if not exists contacts (
  id uuid primary key default gen_random_uuid(),
  company_id uuid not null references companies(id) on delete cascade,
  name text,
  role text,
  email text,
  verification text not null default 'unverified'
    check (verification in ('unverified','verified','invalid','manual')),
  contact_form_url text,
  source_url text not null,           -- provenance is mandatory (compliance)
  collected_at timestamptz not null default now(),
  last_engaged_at timestamptz,
  unique (company_id, email)
);

-- ---------- Outreach ----------
create table if not exists campaigns (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  steps int not null default 3,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists emails (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid not null references contacts(id) on delete cascade,
  campaign_id uuid not null references campaigns(id),
  step int not null default 1,
  status text not null default 'drafted'
    check (status in ('drafted','approved','sending','sent','skipped','bounced','expired')),
  subject text,
  body text,
  mailbox text,
  message_id text,
  idempotency_key text not null unique,
  attempts int not null default 0,
  sent_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (contact_id, campaign_id, step)
);
create index if not exists emails_status_idx on emails (status);

create table if not exists replies (
  id uuid primary key default gen_random_uuid(),
  email_id uuid references emails(id) on delete set null,
  contact_id uuid references contacts(id) on delete set null,
  message_id text unique,
  in_reply_to text,
  status text not null default 'received'
    check (status in ('received','classified','acknowledged','handled','unsubscribed')),
  label text,                         -- interested | question | not_interested | ooo | bounce | unsubscribe
  body text,
  draft_response text,
  received_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists replies_status_idx on replies (status);

-- ---------- Content ----------
create table if not exists topics (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  brief text,
  status text not null default 'idea' check (status in ('idea','used','dropped')),
  embedding vector(1024),
  embedding_model text,
  created_at timestamptz not null default now()
);

create table if not exists content_posts (
  id uuid primary key default gen_random_uuid(),
  topic_id uuid references topics(id),
  title text,
  body_md text,
  status text not null default 'idea'
    check (status in ('idea','drafted','in_review','approved','published','rejected','publish_failed')),
  reviewer_note text,
  cms_url text,
  devto_url text,
  linkedin_url text,
  idempotency_key text unique,
  metrics jsonb,
  embedding vector(1024),
  embedding_model text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz
);
create index if not exists content_posts_status_idx on content_posts (status);

-- ---------- Control ----------
create table if not exists team_members (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  slack_user_id text unique,
  auth_user_id uuid unique,           -- Supabase Auth user, for the dashboard
  role text not null default 'viewer' check (role in ('viewer','approver','admin')),
  active boolean not null default true
);

-- Append-only: corrections are new rows.
create table if not exists approvals (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('lead','email','reply','post')),
  ref_id uuid not null,               -- companies / emails / replies / content_posts id
  status text not null default 'pending' check (status in ('pending','approved','rejected','expired')),
  slack_channel text,
  slack_ts text,
  decided_by text,                    -- slack user id
  decided_at timestamptz,
  decision_note text,
  expires_at timestamptz not null default now() + interval '72 hours',
  created_at timestamptz not null default now()
);
create index if not exists approvals_pending_idx on approvals (status) where status = 'pending';
create unique index if not exists approvals_one_pending_per_ref on approvals (kind, ref_id) where status = 'pending';

create table if not exists suppression_list (
  email_hash text primary key,        -- sha256 of lowercased address; survives contact deletion
  reason text not null,               -- unsubscribe | bounce | complaint | manual
  created_at timestamptz not null default now()
);

create table if not exists agent_runs (
  id uuid primary key default gen_random_uuid(),
  service text not null,
  job text not null,
  run_id text,
  status text not null check (status in ('running','succeeded','failed','skipped')),
  items_processed int not null default 0,
  items_failed int not null default 0,
  tokens_in bigint not null default 0,
  tokens_out bigint not null default 0,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);
create index if not exists agent_runs_job_idx on agent_runs (service, job, started_at desc);

create table if not exists send_counters (
  day date not null,
  mailbox text not null,
  sent int not null default 0,
  primary key (day, mailbox)
);

-- ---------- Functions ----------
create or replace function email_hash(addr text) returns text
language sql immutable as $$ select encode(digest(lower(trim(addr)), 'sha256'), 'hex') $$;

create or replace function is_suppressed(addr text) returns boolean
language sql stable as $$
  select exists (select 1 from suppression_list where email_hash = email_hash(addr))
$$;

-- Conditional increment: returns true only if the mailbox is still under its daily cap.
create or replace function try_increment_send(p_mailbox text, p_cap int) returns boolean
language plpgsql as $$
declare n int;
begin
  insert into send_counters (day, mailbox, sent) values (current_date, p_mailbox, 0)
    on conflict (day, mailbox) do nothing;
  update send_counters set sent = sent + 1
    where day = current_date and mailbox = p_mailbox and sent < p_cap;
  get diagnostics n = row_count;
  return n = 1;
end $$;

-- Decide an approval exactly once. Returns the row only if it was still pending and not expired.
create or replace function decide_approval(p_id uuid, p_status text, p_by text, p_note text default null)
returns approvals language plpgsql as $$
declare r approvals;
begin
  update approvals
     set status = p_status, decided_by = p_by, decided_at = now(), decision_note = p_note
   where id = p_id and status = 'pending' and expires_at > now()
   returning * into r;
  return r;
end $$;

create or replace function expire_approvals() returns int
language sql as $$
  with u as (update approvals set status = 'expired' where status = 'pending' and expires_at <= now() returning 1)
  select count(*)::int from u
$$;

-- Retention: contacts without engagement for 12 months are deleted (suppression hashes are kept).
create or replace function purge_stale_contacts() returns int
language sql as $$
  with d as (
    delete from contacts
     where coalesce(last_engaged_at, collected_at) < now() - interval '12 months' returning 1)
  select count(*)::int from d
$$;

create or replace function touch_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

do $$ declare t text; begin
  foreach t in array array['companies','emails','replies','content_posts'] loop
    execute format('drop trigger if exists %I_touch on %I', t, t);
    execute format('create trigger %I_touch before update on %I for each row execute function touch_updated_at()', t, t);
  end loop;
end $$;

-- ---------- Row Level Security ----------
-- Services use service_role (bypasses RLS). Dashboard users are authenticated approvers/viewers: read only.
do $$ declare t text; begin
  foreach t in array array['companies','lead_signals','contacts','campaigns','emails','replies','topics',
    'content_posts','team_members','approvals','suppression_list','agent_runs','send_counters'] loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

create or replace function is_team_member() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from team_members where auth_user_id = auth.uid() and active)
$$;

do $$ declare t text; begin
  foreach t in array array['companies','lead_signals','contacts','emails','replies','topics',
    'content_posts','approvals','agent_runs'] loop
    execute format('drop policy if exists %I on %I', t || '_team_read', t);
    execute format('create policy %I on %I for select to authenticated using (is_team_member())',
                   t || '_team_read', t);
  end loop;
end $$;
-- suppression_list, send_counters, team_members, campaigns: no policies => service role only.
