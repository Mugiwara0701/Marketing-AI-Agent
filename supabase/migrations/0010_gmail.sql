-- (written as 0007_gmail.sql on feature/email; renumbered: 0007 is the lead pipeline)
-- Gmail replaces Resend as the mail provider. Gmail ids are kept next to the RFC Message-ID:
--   message_id        RFC 5322 Message-ID header (used in In-Reply-To / References)
--   gmail_message_id  Gmail API id of one message
--   gmail_thread_id   Gmail conversation id (sent as threadId so replies stay in the thread)
-- Resend-era columns (provider_id, delivered_at, opened_at, open_count) and email_events stay for old rows.
-- email_events now also dedupes polled Gmail messages (provider_event_id = 'gmail:<message id>').

alter table emails add column if not exists gmail_message_id text;
alter table emails add column if not exists gmail_thread_id text;
create unique index if not exists emails_gmail_message_id_idx on emails (gmail_message_id) where gmail_message_id is not null;
create index if not exists emails_gmail_thread_idx on emails (gmail_thread_id) where gmail_thread_id is not null;
create index if not exists emails_message_id_idx on emails (message_id) where message_id is not null;

alter table replies add column if not exists gmail_message_id text;
alter table replies add column if not exists gmail_thread_id text;
create unique index if not exists replies_gmail_message_id_idx on replies (gmail_message_id) where gmail_message_id is not null;
