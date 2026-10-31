-- Свои критерии интересности: чем канал считает материал сильным, а чем — слабым.
-- Пусто — стандартные критерии новостного или тематического канала.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS interest_criteria text NOT NULL DEFAULT '';
