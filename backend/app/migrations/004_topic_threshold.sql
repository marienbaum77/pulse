ALTER TABLE projects
  ADD COLUMN topic_threshold real NOT NULL DEFAULT 0.40 CHECK (topic_threshold BETWEEN 0 AND 1);