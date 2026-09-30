ALTER TABLE items
  ADD COLUMN article_content_status text NOT NULL DEFAULT 'pending'
    CHECK (article_content_status IN ('pending','running','done','failed')),
  ADD COLUMN article_content_started_at timestamptz;