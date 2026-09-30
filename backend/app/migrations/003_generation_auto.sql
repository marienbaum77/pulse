-- Режим «авто»: LLM только когда есть что синтезировать (несколько источников или длинный текст).
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_generation_mode_check;
ALTER TABLE projects ADD CONSTRAINT projects_generation_mode_check
  CHECK (generation_mode IN ('llm', 'extractive', 'auto'));
