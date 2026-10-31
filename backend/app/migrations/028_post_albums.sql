-- Ролик поста — видео (sendVideo) или GIF (sendAnimation); длительность не ограничена, только предел Telegram в 50 МБ.
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS video_kind text NOT NULL DEFAULT 'video' CHECK (video_kind IN ('video', 'animation'));
-- Ещё до трёх иллюстраций первоисточника или статьи: вместе с обложкой и видео пост уходит в Telegram альбомом.
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS extra_images jsonb NOT NULL DEFAULT '[]'::jsonb;
