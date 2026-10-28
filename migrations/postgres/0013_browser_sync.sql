-- browser-sync-v1. No credentials or HAR; retain receipt, clear validated capture payload after processing.
CREATE TABLE IF NOT EXISTS browser_sync_jobs (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  client_request_id TEXT NOT NULL,
  betting_day DATE NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('capturing','queued','running','succeeded','partial','failed')),
  lock_key INTEGER NOT NULL DEFAULT 1 CHECK (lock_key = 1),
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  payload TEXT,
  summary TEXT,
  error_code TEXT,
  UNIQUE(user_id, client_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_sync_active ON browser_sync_jobs(lock_key)
  WHERE state IN ('capturing','queued','running');
CREATE INDEX IF NOT EXISTS idx_browser_sync_user ON browser_sync_jobs(user_id, created_at);
ALTER TABLE browser_sync_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON browser_sync_jobs FROM PUBLIC;
DO $$
DECLARE api_role text;
BEGIN
  FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
      EXECUTE format('REVOKE ALL ON browser_sync_jobs FROM %I', api_role);
    END IF;
  END LOOP;
END $$;
