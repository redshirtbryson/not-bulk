-- migrate:up
CREATE TABLE exports (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind text NOT NULL DEFAULT 'pdf' CHECK (kind IN ('pdf')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','rendering','ready','failed')),
  storage_key text,                     -- NULL until ready
  card_count int NOT NULL DEFAULT 0,
  bytes bigint NOT NULL DEFAULT 0,
  last_error text,
  expires_at timestamptz,               -- set when ready = now() + retention
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX exports_user_idx ON exports (user_id, created_at DESC);

ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price','export'));

-- migrate:down
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price'));
DROP TABLE exports;
