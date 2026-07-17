-- migrate:up
CREATE TABLE prices (
  card_ref_id text NOT NULL REFERENCES card_refs(id) ON DELETE CASCADE,
  finish text NOT NULL,                 -- tcgplayer finish key: 'normal' | 'holofoil' | 'reverseHolofoil' | ...
  price_cents integer,                  -- NULL = cached known-miss (no data), NOT $0
  source text NOT NULL,                 -- 'pokemontcg' | 'collectr'
  fetched_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (card_ref_id, finish)
);

-- extend jobs.type to allow 'price' (recreate the CHECK)
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price'));

-- reference-image cache marker: which card_refs have a MinIO-cached ref image
ALTER TABLE card_refs ADD COLUMN ref_cached_key text;   -- NULL until proxied+cached once

-- migrate:down
ALTER TABLE card_refs DROP COLUMN ref_cached_key;
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction'));
DROP TABLE prices;
