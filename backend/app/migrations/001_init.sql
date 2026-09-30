CREATE TABLE users (
  id bigserial PRIMARY KEY,
  email text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin','editor','viewer')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  topic text NOT NULL DEFAULT '',
  topic_embedding vector,
  language text NOT NULL DEFAULT 'ru',
  tone text NOT NULL DEFAULT 'нейтральный, информативный',
  max_length int NOT NULL DEFAULT 900,
  prompt_template text NOT NULL DEFAULT '',
  prompt_version int NOT NULL DEFAULT 1,
  generation_mode text NOT NULL DEFAULT 'extractive' CHECK (generation_mode IN ('llm','extractive')),
  publish_mode text NOT NULL DEFAULT 'review' CHECK (publish_mode IN ('auto','review')),
  window_hours int NOT NULL DEFAULT 48,
  sim_threshold real NOT NULL DEFAULT 0.72,
  weights jsonb NOT NULL DEFAULT '{"coverage":0.15,"authority":0.075,"freshness":0.10,"velocity":0.075,"topic_fit":0.60}',
  context_items int NOT NULL DEFAULT 6,
  context_sentences int NOT NULL DEFAULT 3,
  min_items int NOT NULL DEFAULT 2,
  auto_retry_unknown boolean NOT NULL DEFAULT false,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sources (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type text NOT NULL CHECK (type IN ('rss','manual')),
  name text NOT NULL,
  url text NOT NULL DEFAULT '',
  authority real NOT NULL DEFAULT 0.5,
  poll_minutes int NOT NULL DEFAULT 30,
  enabled boolean NOT NULL DEFAULT true,
  etag text,
  last_modified text,
  next_poll_at timestamptz NOT NULL DEFAULT now(),
  last_fetched_at timestamptz,
  last_ok_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sources_project ON sources(project_id);

CREATE TABLE items (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id bigint REFERENCES sources(id) ON DELETE SET NULL,
  url text NOT NULL DEFAULT '',
  url_hash text NOT NULL,
  title text NOT NULL,
  text text NOT NULL DEFAULT '',
  published_at timestamptz NOT NULL,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','embedded','clustered','duplicate','error')),
  embedding vector,
  cluster_id bigint,
  dup_of bigint,
  UNIQUE (project_id, url_hash)
);
CREATE INDEX items_project_status ON items(project_id, status);
CREATE INDEX items_cluster ON items(cluster_id);
CREATE INDEX items_project_pub ON items(project_id, published_at);

CREATE TABLE clusters (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  centroid vector,
  title text NOT NULL DEFAULT '',
  first_seen timestamptz NOT NULL,
  last_seen timestamptz NOT NULL,
  item_count int NOT NULL DEFAULT 0,
  source_count int NOT NULL DEFAULT 0,
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','drafted','published','closed','excluded')),
  score real,
  score_breakdown jsonb,
  published_at timestamptz,
  item_count_at_publish int,
  published_centroid vector,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX clusters_project_state ON clusters(project_id, state);

CREATE TABLE drafts (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind text NOT NULL DEFAULT 'post' CHECK (kind IN ('post','digest')),
  cluster_ids bigint[] NOT NULL DEFAULT '{}',
  title text NOT NULL DEFAULT '',
  body text NOT NULL DEFAULT '',
  citations jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL DEFAULT 'generating' CHECK (status IN ('generating','pending_review','approved','rejected','failed')),
  model text,
  params jsonb NOT NULL DEFAULT '{}',
  checks jsonb NOT NULL DEFAULT '{}',
  error text,
  created_by bigint,
  approved_by bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX drafts_project_status ON drafts(project_id, status);

CREATE TABLE channels (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type text NOT NULL CHECK (type IN ('telegram','webhook','console')),
  name text NOT NULL,
  config jsonb NOT NULL DEFAULT '{}',
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE schedules (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name text NOT NULL,
  cron text NOT NULL,
  tz text NOT NULL DEFAULT 'UTC',
  kind text NOT NULL DEFAULT 'digest' CHECK (kind IN ('post','digest')),
  top_n int NOT NULL DEFAULT 5,
  enabled boolean NOT NULL DEFAULT true,
  last_fired_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE schedule_fires (
  schedule_id bigint NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  fire_time timestamptz NOT NULL,
  PRIMARY KEY (schedule_id, fire_time)
);

CREATE TABLE publications (
  id bigserial PRIMARY KEY,
  draft_id bigint NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  channel_id bigint NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','unknown','cancelled')),
  due_at timestamptz NOT NULL DEFAULT now(),
  attempts int NOT NULL DEFAULT 0,
  locked_until timestamptz,
  external_id text,
  error text,
  sent_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (draft_id, channel_id)
);
CREATE INDEX publications_due ON publications(status, due_at);

CREATE TABLE jobs (
  id bigserial PRIMARY KEY,
  kind text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  dedupe_key text,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
  run_at timestamptz NOT NULL DEFAULT now(),
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  locked_until timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX jobs_due ON jobs(status, run_at);
CREATE UNIQUE INDEX jobs_dedupe ON jobs(dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL;

CREATE TABLE llm_calls (
  id bigserial PRIMARY KEY,
  project_id bigint,
  kind text NOT NULL,
  model text NOT NULL,
  prompt_tokens int,
  completion_tokens int,
  latency_ms int NOT NULL,
  ok boolean NOT NULL,
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_created ON llm_calls(created_at);

CREATE TABLE audit_log (
  id bigserial PRIMARY KEY,
  user_id bigint,
  user_email text,
  action text NOT NULL,
  entity text NOT NULL,
  entity_id bigint,
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kv (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
