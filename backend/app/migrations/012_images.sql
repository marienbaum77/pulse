-- Картинки хранятся только как URL: сами файлы в БД не попадают и её не раздувают.
ALTER TABLE items ADD COLUMN IF NOT EXISTS image_url TEXT;
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS image_url TEXT;
