-- Статистика своих постов: просмотры и реакции из веб-превью t.me/s/<канал>, без бота и без входа.
ALTER TABLE publications ADD COLUMN IF NOT EXISTS views int;
ALTER TABLE publications ADD COLUMN IF NOT EXISTS reactions int;
ALTER TABLE publications ADD COLUMN IF NOT EXISTS stats_at timestamptz;
CREATE INDEX IF NOT EXISTS publications_channel_external ON publications(channel_id, external_id) WHERE status = 'sent';

-- Как заходят посты источника по сравнению с медианой канала (0.5–1.5); NULL — мало данных.
-- Умножается на authority при оценке сюжетов: ручная авторитетность остаётся такой, какой её задал редактор.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS performance real;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS performance_posts int NOT NULL DEFAULT 0;

-- Пост, на который новый отвечает (reply в Telegram): связь найдена без языковой модели — обновление сюжета,
-- тот же репозиторий или первоисточник, близкие векторы с общим именем в заголовках.
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS reply_to_draft_id bigint REFERENCES drafts(id) ON DELETE SET NULL;
-- За сколько дней искать связанный пост; 0 — не отвечать на прежние посты.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS reply_related_days int NOT NULL DEFAULT 14
  CHECK (reply_related_days BETWEEN 0 AND 60);
