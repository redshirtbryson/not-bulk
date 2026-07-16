-- migrate:up
CREATE TABLE card_refs (
  id text PRIMARY KEY,               -- pokemontcg.io id, e.g. 'sv4-123'
  name text NOT NULL,
  set_id text NOT NULL,
  set_name text NOT NULL,
  number text NOT NULL,              -- collector number as printed, e.g. '123'
  printed_total text,                -- denominator, e.g. '198'
  rarity text,
  image_url text NOT NULL,
  finishes text[] NOT NULL DEFAULT '{}',
  synced_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX card_refs_name_idx ON card_refs (lower(name));
CREATE INDEX card_refs_number_idx ON card_refs (number);

CREATE TABLE ref_hashes (
  id uuid PRIMARY KEY,
  card_ref_id text NOT NULL REFERENCES card_refs(id) ON DELETE CASCADE,
  hash_type text NOT NULL CHECK (hash_type IN ('full','edge','region_art','region_name','region_text')),
  hash_bits bigint NOT NULL,         -- 64-bit hash stored as signed bigint
  source text NOT NULL CHECK (source IN ('reference','augmented','user_validated')),
  usage_count int NOT NULL DEFAULT 0,
  last_matched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ref_hashes_type_idx ON ref_hashes (hash_type);
CREATE INDEX ref_hashes_card_idx ON ref_hashes (card_ref_id);

CREATE TABLE llm_cache (
  crop_sha256 text PRIMARY KEY,
  model text NOT NULL,
  response jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- migrate:down
DROP TABLE llm_cache;
DROP TABLE ref_hashes;
DROP TABLE card_refs;
