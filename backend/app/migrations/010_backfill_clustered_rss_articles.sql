INSERT INTO jobs(kind, payload, dedupe_key)
SELECT
  'enrich_articles',
  jsonb_build_object('project_id', i.project_id, 'source_id', i.source_id, 'include_clustered', true),
  'article-backfill:' || i.project_id || ':' || i.source_id
FROM items i
JOIN sources s ON s.id = i.source_id AND s.type = 'rss'
JOIN clusters c ON c.id = i.cluster_id AND c.state IN ('open','drafted')
WHERE i.status = 'clustered'
  AND i.article_content_status = 'pending'
  AND i.url <> ''
  AND char_length(btrim(i.text)) < 800
  AND i.fetched_at >= now() - interval '14 days'
GROUP BY i.project_id, i.source_id
ON CONFLICT (dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL DO NOTHING;