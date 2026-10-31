-- Нижний порог интересности для автоматического выбора сюжета в слот: если даже лучший кандидат оценён ниже,
-- слот остаётся пустым (пустой слот лучше слабого поста). NULL — выключено, слот берёт лучшего из имеющихся.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS slot_min_interest real
  CHECK (slot_min_interest IS NULL OR slot_min_interest BETWEEN 0 AND 10);

-- Промпт оценки теперь снижает оценку за самопродвижение и не прощает рекламу в статьях:
-- оценки открытых сюжетов по прежнему промпту сбрасываются, модель оценит их заново на следующих проходах.
UPDATE clusters SET interest = NULL, interest_reason = NULL, interest_items = NULL WHERE state = 'open';
