-- migrate:up
CREATE TABLE users (
  id uuid PRIMARY KEY,
  email text UNIQUE,                    -- NULL for anonymous trial users
  anon_token_hash text UNIQUE,          -- NULL for real users
  tier text NOT NULL DEFAULT 'free' CHECK (tier IN ('anon','free')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  storage_bytes_used bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (email IS NOT NULL OR anon_token_hash IS NOT NULL)
);

CREATE TABLE sessions (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,      -- sha256 hex of the opaque cookie token
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL       -- absolute: created_at + 30 days
);

CREATE TABLE magic_links (
  id uuid PRIMARY KEY,
  email text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,      -- created_at + 15 minutes
  used_at timestamptz
);
CREATE INDEX magic_links_email_idx ON magic_links (email, created_at);

CREATE TABLE usage (
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  day date NOT NULL,
  batches int NOT NULL DEFAULT 0,
  photos int NOT NULL DEFAULT 0,
  cards int NOT NULL DEFAULT 0,
  llm_calls int NOT NULL DEFAULT 0,
  llm_cost_cents int NOT NULL DEFAULT 0,
  fetches int NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);

CREATE TABLE batches (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','processing','complete','deferred','failed')),
  photo_count int NOT NULL DEFAULT 0,
  origin_url text,
  notify_on_complete boolean NOT NULL DEFAULT false,
  notified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz
);
CREATE INDEX batches_user_idx ON batches (user_id, created_at DESC);

CREATE TABLE photos (
  id uuid PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','fetching','stored','detecting','done','failed')),
  storage_key text,                     -- NULL until stored
  source_type text NOT NULL DEFAULT 'upload' CHECK (source_type IN ('upload','imgur','reddit')),
  source_url text,
  bytes bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX photos_batch_idx ON photos (batch_id);

CREATE TABLE cards (
  id uuid PRIMARY KEY,
  photo_id uuid NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
  crop_index int NOT NULL,
  crop_storage_key text,
  card_ref_id text REFERENCES card_refs(id),
  finish text,
  finish_needs_confirmation boolean NOT NULL DEFAULT false,
  quantity int NOT NULL DEFAULT 1,
  confidence int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','auto','validation','validated','corrected','skipped','not_card','unreadable','merged')),
  accepted_stage text,
  rotation int NOT NULL DEFAULT 0,
  method_h_id text, method_h_score real,
  method_a_id text, method_a_score real,
  method_b_id text, method_b_score real,
  method_c_id text, method_c_score real,
  candidates jsonb NOT NULL DEFAULT '[]',   -- [{"card_ref_id": "...", "score": null}]
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (photo_id, crop_index)
);
CREATE INDEX cards_photo_idx ON cards (photo_id);

CREATE TABLE corrections (
  id uuid PRIMARY KEY,
  card_id uuid REFERENCES cards(id) ON DELETE SET NULL,
  crop_hash text NOT NULL,              -- sha256 of stored crop webp bytes
  predicted_ref_id text,
  actual_ref_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id uuid PRIMARY KEY,
  type text NOT NULL CHECK (type IN ('detect','identify','fetch_source','ingest_correction')),
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  run_after timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  locked_by text,
  last_error text,
  batch_id uuid REFERENCES batches(id) ON DELETE CASCADE,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim_idx ON jobs (status, run_after);

-- migrate:down
DROP TABLE jobs;
DROP TABLE corrections;
DROP TABLE cards;
DROP TABLE photos;
DROP TABLE batches;
DROP TABLE usage;
DROP TABLE magic_links;
DROP TABLE sessions;
DROP TABLE users;
