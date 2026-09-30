ALTER TABLE projects ALTER COLUMN topic_threshold SET DEFAULT 0.40;
UPDATE projects SET topic_threshold = 0.40 WHERE topic_threshold BETWEEN 0.449999 AND 0.450001;