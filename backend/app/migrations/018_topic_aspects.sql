-- Тема проекта как набор аспектов (фраз) и тематическая оценка материала.
-- topic_score: максимальный косинус заголовка материала к аспектам темы (0..1);
-- topic_fit сюжета считается как среднее topic_score его материалов, а не по размытому центроиду.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS topic_aspects jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE items ADD COLUMN IF NOT EXISTS topic_score real;
CREATE INDEX IF NOT EXISTS items_topic_score_pending ON items(project_id) WHERE topic_score IS NULL;
