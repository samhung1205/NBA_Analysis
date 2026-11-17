-- Cold copies preserve complete source rows. The runtime role reads injury
-- entries through the same as-of loader. Old decision and sizing copies are
-- for audit and restoration. No data is moved by this migration.
CREATE TABLE IF NOT EXISTS cold_injury_entries (
  report_id integer PRIMARY KEY REFERENCES injury_reports(id) ON DELETE RESTRICT,
  row_count integer NOT NULL CHECK (row_count > 0),
  entries jsonb NOT NULL CHECK (jsonb_typeof(entries) = 'array' AND jsonb_array_length(entries) = row_count)
);

CREATE TABLE IF NOT EXISTS cold_decisions (
  snapshot_id bigint PRIMARY KEY,
  snapshot jsonb NOT NULL,
  opportunities jsonb NOT NULL CHECK (jsonb_typeof(opportunities) = 'array')
);

CREATE TABLE IF NOT EXISTS cold_sizing_batches (
  batch_key text PRIMARY KEY,
  oldest_day date NOT NULL,
  newest_day date NOT NULL,
  row_count integer NOT NULL CHECK (row_count > 0),
  entries jsonb NOT NULL CHECK (jsonb_typeof(entries) = 'array' AND jsonb_array_length(entries) = row_count)
);

ALTER TABLE cold_injury_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE cold_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cold_sizing_batches ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON cold_injury_entries, cold_decisions, cold_sizing_batches FROM PUBLIC;

DO $$
DECLARE
  api_role text;
BEGIN
  FOR api_role IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
  LOOP
    EXECUTE format('REVOKE ALL ON %I.cold_injury_entries, %I.cold_decisions, %I.cold_sizing_batches FROM %I',
                   current_schema(), current_schema(), current_schema(), api_role);
  END LOOP;
END $$;
