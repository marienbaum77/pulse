-- Контентная платформа: новые типы источников (YouTube, веб-поиск, сайт без RSS, Reddit, Telegram),
-- вид материала (статья/видео/пост) и профиль проекта, который решает, как материал попадает в публикацию.
ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_type_check;
ALTER TABLE sources ADD CONSTRAINT sources_type_check
  CHECK (type IN ('rss', 'manual', 'youtube', 'web_search', 'site', 'reddit', 'telegram'));
-- Параметры коннектора: запрос, вид YouTube-источника, сабреддит и т. п.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS config jsonb NOT NULL DEFAULT '{}'::jsonb;

-- kind — что это за материал (влияет на формат поста); mode — как он отбирается:
-- news — сюжеты из нескольких источников в коротком окне; curated — каждый материал сам по себе кандидат.
ALTER TABLE items ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'article'
  CHECK (kind IN ('article', 'video', 'post'));
ALTER TABLE items ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'news'
  CHECK (mode IN ('news', 'curated'));
-- Метаданные коннектора: автор, длительность, просмотры, исходная дата публикации и пр.
ALTER TABLE items ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE clusters ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'news'
  CHECK (mode IN ('news', 'curated'));
CREATE INDEX IF NOT EXISTS clusters_project_mode_state ON clusters(project_id, mode, state);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS content_profile text NOT NULL DEFAULT 'news'
  CHECK (content_profile IN ('news', 'curated', 'mixed'));
-- Сколько живёт кандидат в curated-режиме: подборочный контент не устаревает за двое суток, как новость.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS curated_window_hours int NOT NULL DEFAULT 336
  CHECK (curated_window_hours BETWEEN 24 AND 2160);

-- Память об уже виденном curated-контенте: материалы удаляются по сроку хранения, а поиск и YouTube
-- возвращают старые ссылки снова. Без этой таблицы удалённое видео вернулось бы как новый кандидат.
CREATE TABLE IF NOT EXISTS content_seen (
  project_id bigint NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  url_hash text NOT NULL,
  seen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, url_hash)
);
