CREATE TABLE IF NOT EXISTS editorial_examples (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label smallint NOT NULL CHECK (label IN (0, 1)),
  source_kind text NOT NULL CHECK (source_kind IN (
    'reference_channel', 'reference_ad', 'reference_weak',
    'editor_approved', 'editor_rejected', 'stream_unlabeled'
  )),
  hook_text text NOT NULL,
  hook_embedding vector,
  sample_weight real NOT NULL DEFAULT 1.0,
  cluster_id bigint,
  external_url text,
  meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS editorial_examples_project_hook
  ON editorial_examples(project_id, md5(hook_text));
CREATE INDEX IF NOT EXISTS editorial_examples_project_cluster
  ON editorial_examples(project_id, cluster_id);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS reference_tg_channels text[] NOT NULL DEFAULT '{}';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS editorial_model_state jsonb;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS editorial_actions_since_train int NOT NULL DEFAULT 0;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS interest_model text;
