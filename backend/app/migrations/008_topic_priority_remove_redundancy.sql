ALTER TABLE projects ALTER COLUMN weights SET DEFAULT '{"coverage":0.15,"authority":0.075,"freshness":0.10,"velocity":0.075,"topic_fit":0.60}';

WITH old_weights AS (
  SELECT id,
    COALESCE((weights->>'coverage')::numeric, 0.30) AS coverage,
    COALESCE((weights->>'authority')::numeric, 0.15) AS authority,
    COALESCE((weights->>'freshness')::numeric, 0.20) AS freshness,
    COALESCE((weights->>'velocity')::numeric, 0.15) AS velocity
  FROM projects
), totals AS (
  SELECT *, coverage + authority + freshness + velocity AS total
  FROM old_weights
)
UPDATE projects p SET weights = jsonb_build_object(
  'coverage', CASE WHEN t.total = 0 THEN 0.15 ELSE round(t.coverage / t.total * 0.40, 4) END,
  'authority', CASE WHEN t.total = 0 THEN 0.075 ELSE round(t.authority / t.total * 0.40, 4) END,
  'freshness', CASE WHEN t.total = 0 THEN 0.10 ELSE round(t.freshness / t.total * 0.40, 4) END,
  'velocity', CASE WHEN t.total = 0 THEN 0.075 ELSE round(t.velocity / t.total * 0.40, 4) END,
  'topic_fit', 0.60
)
FROM totals t
WHERE p.id = t.id;

UPDATE clusters SET score = NULL, score_breakdown = NULL;