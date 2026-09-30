-- Третий режим публикации: «полностью автоматически» — публикует без ожидания
-- редактора и без блокировки на автопроверках (в отличие от «auto», где сомнительные
-- посты остаются на проверке).
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_publish_mode_check;
ALTER TABLE projects ADD CONSTRAINT projects_publish_mode_check
  CHECK (publish_mode IN ('review', 'auto', 'full_auto'));
