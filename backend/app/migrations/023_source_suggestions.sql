-- Автоподбор источников: кандидаты, найденные по теме канала, ждут решения редактора.
-- value — то, что вводится в форму источника (ссылка на канал, адрес сайта, сабреддит, @канал);
-- скрытый (dismissed) кандидат при следующем подборе не возвращается.
CREATE TABLE IF NOT EXISTS source_suggestions (
  id bigserial PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type text NOT NULL CHECK (type IN ('youtube', 'site', 'reddit', 'telegram')),
  value text NOT NULL,
  name text NOT NULL,
  reason text NOT NULL DEFAULT '',
  score real NOT NULL DEFAULT 0,
  hits int NOT NULL DEFAULT 0,
  samples jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'accepted', 'dismissed')),
  source_id bigint REFERENCES sources(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, type, value)
);
CREATE INDEX IF NOT EXISTS source_suggestions_project_status ON source_suggestions(project_id, status, score DESC);
