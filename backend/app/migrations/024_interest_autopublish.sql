-- Немедленная публикация по интересности: сюжет, который модель оценила не ниже instant_interest,
-- публикуется сразу после оценки, не дожидаясь слота. NULL — выключено. Не больше instant_daily_limit таких постов за 24 часа.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS instant_interest real
  CHECK (instant_interest IS NULL OR instant_interest BETWEEN 0 AND 10);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS instant_daily_limit int NOT NULL DEFAULT 3
  CHECK (instant_daily_limit BETWEEN 1 AND 50);
-- Новостной сюжет из меньшего числа материалов, чем min_items, допускается к публикации,
-- если модель оценила его не ниже single_min_interest. NULL — одиночные сюжеты не публикуются.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS single_min_interest real
  CHECK (single_min_interest IS NULL OR single_min_interest BETWEEN 0 AND 10);
-- Аспекты темы составила модель: при смене темы они составляются заново, пока редактор не исправит их вручную.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS topic_aspects_auto boolean NOT NULL DEFAULT false;

-- Черновик создан по порогу интересности, а не слотом или редактором: по таким считается дневной лимит.
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS instant boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS drafts_project_instant ON drafts(project_id, created_at) WHERE instant;
