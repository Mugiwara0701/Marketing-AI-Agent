-- Daily agent: richer lead facts, review notes on drafts, blog metadata. All additive.

alter table companies
  add column if not exists website text,
  add column if not exists location text,
  add column if not exists technologies text[],
  add column if not exists project_summary text;

alter table emails
  add column if not exists review_note text;

alter table content_posts
  add column if not exists metadata jsonb,
  add column if not exists tags text[];

-- One blog per day: idempotency_key is 'blog-YYYY-MM-DD' (unique already).
create index if not exists companies_created_idx on companies (created_at);
