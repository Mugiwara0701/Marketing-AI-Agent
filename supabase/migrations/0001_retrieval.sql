-- Retrieval and prompt path tables (see docs: Retrieval and Prompt Path).
-- Embedding dimension 1024 = Qwen3-Embedding-0.6B. Changing model => new column/table + re-embed.
create extension if not exists vector;

create table if not exists examples (
  id uuid primary key default gen_random_uuid(),
  task text not null,
  input_text text not null,
  output_json jsonb not null,
  label text,
  source text,
  embedding vector(1024),
  embedding_model text,
  created_at timestamptz not null default now()
);
create index if not exists examples_task_idx on examples (task);

create table if not exists knowledge_docs (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  url text,
  kind text,
  body text not null,
  created_at timestamptz not null default now()
);

create table if not exists knowledge_chunks (
  id uuid primary key default gen_random_uuid(),
  doc_id uuid not null references knowledge_docs (id) on delete cascade,
  chunk_index int not null,
  content text not null,
  embedding vector(1024),
  embedding_model text,
  unique (doc_id, chunk_index)
);

create table if not exists prompt_versions (
  id uuid primary key default gen_random_uuid(),
  task text not null,
  version int not null,
  template text not null,
  active boolean not null default false,
  created_at timestamptz not null default now(),
  unique (task, version)
);
create unique index if not exists prompt_versions_one_active on prompt_versions (task) where active;

create table if not exists retrieval_logs (
  id uuid primary key default gen_random_uuid(),
  task text not null,
  prompt_version int,
  example_ids uuid[],
  chunk_ids uuid[],
  outcome text,
  created_at timestamptz not null default now()
);

-- Exact search is the default. Add HNSW only when needed (max 2000 dims).
-- create index on knowledge_chunks using hnsw (embedding vector_cosine_ops);

-- Lock down: services use the service_role key server-side only.
alter table examples enable row level security;
alter table knowledge_docs enable row level security;
alter table knowledge_chunks enable row level security;
alter table prompt_versions enable row level security;
alter table retrieval_logs enable row level security;
