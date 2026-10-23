-- Контент-план: слот расписания отбирает материалы нужного вида и может готовить черновик заранее,
-- а публикация уходит ровно ко времени слота.
-- content_kinds: пусто — любой материал; иначе news (новостной сюжет), video, article, post.
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS content_kinds text[] NOT NULL DEFAULT '{}';
ALTER TABLE schedules ADD CONSTRAINT schedules_content_kinds_check
  CHECK (content_kinds <@ ARRAY['news', 'video', 'article', 'post']::text[]);
-- За сколько минут до слота готовить черновик: редактор успевает проверить его к публикации.
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS lead_minutes int NOT NULL DEFAULT 0
  CHECK (lead_minutes BETWEEN 0 AND 1440);

-- Итог каждого срабатывания для истории плана: черновики или «нечего публиковать».
ALTER TABLE schedule_fires ADD COLUMN IF NOT EXISTS slot_at timestamptz;
ALTER TABLE schedule_fires ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'queued'
  CHECK (outcome IN ('queued', 'drafted', 'empty', 'failed'));
ALTER TABLE schedule_fires ADD COLUMN IF NOT EXISTS draft_ids bigint[] NOT NULL DEFAULT '{}';

-- Черновик знает свой слот: публикация не раньше scheduled_for.
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS schedule_id bigint REFERENCES schedules(id) ON DELETE SET NULL;
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS scheduled_for timestamptz;
CREATE INDEX IF NOT EXISTS drafts_project_scheduled ON drafts(project_id, scheduled_for) WHERE scheduled_for IS NOT NULL;

-- Не чаще одного поста из одного источника за N часов: один YouTube-канал не займёт всю ленту.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS source_cooldown_hours int NOT NULL DEFAULT 0
  CHECK (source_cooldown_hours BETWEEN 0 AND 168);
