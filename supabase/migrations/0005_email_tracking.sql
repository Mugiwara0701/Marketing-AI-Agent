-- Email tracking: provider ids, delivery/open/bounce timestamps, webhook event log, reply threading.
-- Filled by the resend-webhook Edge Function; read by the follow-up and reply steps of the daily agent.

alter table emails add column if not exists provider_id text;      -- Resend email id, maps webhook events back
alter table emails add column if not exists delivered_at timestamptz;
alter table emails add column if not exists opened_at timestamptz; -- first open only
alter table emails add column if not exists open_count int not null default 0;
alter table emails add column if not exists bounced_at timestamptz;
-- Replies we send are emails rows too (one send path). They live in their own campaign with
-- step = next free number for the contact, so unique (contact_id, campaign_id, step) still holds.
alter table emails add column if not exists reply_id uuid references replies (id) on delete set null;
alter table emails add column if not exists in_reply_to text;      -- Message-ID of the mail being answered

create unique index if not exists emails_provider_id_idx on emails (provider_id) where provider_id is not null;
create index if not exists emails_followup_idx on emails (status, sent_at) where status = 'sent';

alter table replies add column if not exists subject text;

-- One row per webhook delivery; provider_event_id makes redelivery a no-op.
create table if not exists email_events (
  id uuid primary key default gen_random_uuid(),
  provider_event_id text not null unique,
  email_id uuid references emails (id) on delete set null,
  type text not null,                 -- delivered | opened | bounced | complained | received | ...
  payload jsonb,
  received_at timestamptz not null default now()
);
create index if not exists email_events_email_idx on email_events (email_id);

alter table email_events enable row level security;
-- No policies: service role only (the payload can contain addresses).
