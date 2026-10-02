CREATE TABLE translation_cache (
  target_language text NOT NULL,
  source_hash text NOT NULL,
  translated_text text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (target_language, source_hash)
);
CREATE INDEX translation_cache_updated ON translation_cache(updated_at);
