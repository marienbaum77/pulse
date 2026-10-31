-- Длина поста-разбора статьи: длинная статья из одного источника (аналитика, лонгрид) пересказывается разбором,
-- а не заметкой по первым абзацам, и ей нужно больше места, чем новости. Предел Telegram для текста — 4096 символов,
-- запас — под заголовок, источники и подпись канала.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS article_max_length integer NOT NULL DEFAULT 1500
  CHECK (article_max_length BETWEEN 300 AND 3500);
