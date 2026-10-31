-- Короткое видео из статьи или её первоисточника (не длиннее 15 секунд). Telegram получает его вместо картинки;
-- image_url остаётся запасной обложкой — для ВКонтакте, Discord и на случай, если видео не загрузится.
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS video_url text;
