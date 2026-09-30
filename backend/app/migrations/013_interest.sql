-- Оценка интересности сюжета языковой моделью (0-10) и её краткое обоснование; пересчитывается, когда сюжет заметно вырос.
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS interest REAL;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS interest_reason TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS interest_items INTEGER;
