-- One row per platform rewrite of a daily blog post (dev.to, LinkedIn, ...).

create table if not exists content_variants (
  id uuid primary key default gen_random_uuid(),
  post_id uuid not null references content_posts (id) on delete cascade,
  platform text not null,
  title text,
  body text not null,                 -- final text for that platform (dev.to includes its front matter)
  tags text[],
  review_note text,                   -- format checks that failed, if any
  created_at timestamptz not null default now(),
  unique (post_id, platform)
);

alter table content_variants enable row level security;
