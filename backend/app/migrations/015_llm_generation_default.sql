ALTER TABLE projects ALTER COLUMN generation_mode SET DEFAULT 'llm';

-- Previous versions defaulted projects to extractive mode; move existing projects
-- to the requested LLM synthesis default. Extractive remains an explicit option.
UPDATE projects SET generation_mode = 'llm' WHERE generation_mode <> 'llm';
