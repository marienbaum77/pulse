ALTER TABLE items ADD COLUMN publisher_name text;
ALTER TABLE sources ADD COLUMN last_entry_count int NOT NULL DEFAULT 0;
ALTER TABLE sources ADD COLUMN last_new_count int NOT NULL DEFAULT 0;
