-- Browser capture inbox: one active import globally; payload is cleared after processing/expiry.
CREATE TABLE IF NOT EXISTS browser_sync_jobs (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  client_request_id TEXT NOT NULL,
  betting_day TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('capturing','queued','running','succeeded','partial','failed')),
  lock_key INTEGER NOT NULL DEFAULT 1 CHECK (lock_key = 1),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  received_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  payload TEXT,
  summary TEXT,
  error_code TEXT,
  UNIQUE(user_id, client_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_sync_active ON browser_sync_jobs(lock_key)
  WHERE state IN ('capturing','queued','running');
CREATE INDEX IF NOT EXISTS idx_browser_sync_user ON browser_sync_jobs(user_id, created_at);
