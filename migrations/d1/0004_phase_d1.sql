-- ============================================================
-- Phase D.1 — Odds Ingestion & Market Normalization (SQLite / Cloudflare D1 方言)
-- 對應 migrations/postgres/0004_phase_d1.sql，欄位名稱與語意一致（僅供本機開發 / smoke test）。
-- ============================================================

ALTER TABLE odds_snapshots ADD COLUMN bookmaker TEXT;
ALTER TABLE odds_snapshots ADD COLUMN market_type TEXT;
ALTER TABLE odds_snapshots ADD COLUMN period TEXT;
ALTER TABLE odds_snapshots ADD COLUMN outcome_set TEXT;
ALTER TABLE odds_snapshots ADD COLUMN away_line REAL;
ALTER TABLE odds_snapshots ADD COLUMN draw_odds REAL;
ALTER TABLE odds_snapshots ADD COLUMN model_target TEXT;
ALTER TABLE odds_snapshots ADD COLUMN model_threshold REAL;
ALTER TABLE odds_snapshots ADD COLUMN market_status TEXT;
ALTER TABLE odds_snapshots ADD COLUMN source_event_id TEXT;
ALTER TABLE odds_snapshots ADD COLUMN source_market_id TEXT;
ALTER TABLE odds_snapshots ADD COLUMN source_updated_at TEXT;
ALTER TABLE odds_snapshots ADD COLUMN last_seen_at TEXT;
ALTER TABLE odds_snapshots ADD COLUMN content_hash TEXT;
ALTER TABLE odds_snapshots ADD COLUMN fetch_run_id INTEGER;
ALTER TABLE odds_snapshots ADD COLUMN normalizer_version TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_odds_series_fetch
  ON odds_snapshots(game_id, source, bookmaker, market, fetched_at) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_odds_series_latest
  ON odds_snapshots(game_id, source, bookmaker, market, fetched_at DESC);

CREATE TABLE IF NOT EXISTS odds_fetch_runs (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  source              TEXT NOT NULL,
  started_at          TEXT NOT NULL,
  fetched_at          TEXT,
  finished_at         TEXT,
  outcome             TEXT NOT NULL,
  n_requests          INTEGER,
  latency_ms          INTEGER,
  n_events            INTEGER,
  n_matched           INTEGER,
  n_unmatched         INTEGER,
  n_ambiguous         INTEGER,
  n_rejected          INTEGER,
  n_markets           INTEGER,
  n_markets_invalid   INTEGER,
  n_snapshots_new     INTEGER,
  n_snapshots_unchanged INTEGER,
  n_snapshots_stale   INTEGER,
  quota_remaining     INTEGER,
  quota_used          INTEGER,
  quota_last          INTEGER,
  error               TEXT,
  diagnostics         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_odds_runs_source_time ON odds_fetch_runs(source, started_at DESC);

CREATE TABLE IF NOT EXISTS odds_event_links (
  source            TEXT NOT NULL,
  source_event_id   TEXT NOT NULL,
  game_id           INTEGER REFERENCES games(id) ON DELETE SET NULL,
  status            TEXT NOT NULL,
  method            TEXT,
  reason            TEXT,
  home_name_raw     TEXT,
  away_name_raw     TEXT,
  commence_time_utc TEXT,
  time_delta_min    REAL,
  source_stage      TEXT,
  candidates        TEXT NOT NULL DEFAULT '[]',
  manual            INTEGER NOT NULL DEFAULT 0,
  first_seen_at     TEXT NOT NULL,
  last_seen_at      TEXT NOT NULL,
  PRIMARY KEY (source, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_odds_links_game ON odds_event_links(game_id);

ALTER TABLE data_sources ADD COLUMN last_outcome TEXT;
ALTER TABLE data_sources ADD COLUMN meta TEXT;

CREATE VIEW IF NOT EXISTS v_odds_quotes AS
  SELECT o.id AS snapshot_id, o.game_id, o.source, o.bookmaker, o.market, o.market_type, o.period,
         'home' AS side, o.home_odds AS price, CASE WHEN o.market_type = 'spread' THEN o.line END AS display_line,
         o.model_target, o.model_threshold, 'gt' AS comparator,
         o.market_status, o.fetched_at, o.last_seen_at, o.source_updated_at, o.source_event_id
    FROM odds_snapshots o WHERE o.content_hash IS NOT NULL AND o.home_odds IS NOT NULL
  UNION ALL
  SELECT o.id, o.game_id, o.source, o.bookmaker, o.market, o.market_type, o.period,
         'away', o.away_odds, CASE WHEN o.market_type = 'spread' THEN o.away_line END,
         o.model_target, o.model_threshold, 'lt',
         o.market_status, o.fetched_at, o.last_seen_at, o.source_updated_at, o.source_event_id
    FROM odds_snapshots o WHERE o.content_hash IS NOT NULL AND o.away_odds IS NOT NULL
  UNION ALL
  SELECT o.id, o.game_id, o.source, o.bookmaker, o.market, o.market_type, o.period,
         'draw', o.draw_odds, NULL, o.model_target, o.model_threshold, 'eq',
         o.market_status, o.fetched_at, o.last_seen_at, o.source_updated_at, o.source_event_id
    FROM odds_snapshots o WHERE o.content_hash IS NOT NULL AND o.draw_odds IS NOT NULL
  UNION ALL
  SELECT o.id, o.game_id, o.source, o.bookmaker, o.market, o.market_type, o.period,
         'over', o.over_odds, o.line, o.model_target, o.model_threshold, 'gt',
         o.market_status, o.fetched_at, o.last_seen_at, o.source_updated_at, o.source_event_id
    FROM odds_snapshots o WHERE o.content_hash IS NOT NULL AND o.over_odds IS NOT NULL
  UNION ALL
  SELECT o.id, o.game_id, o.source, o.bookmaker, o.market, o.market_type, o.period,
         'under', o.under_odds, o.line, o.model_target, o.model_threshold, 'lt',
         o.market_status, o.fetched_at, o.last_seen_at, o.source_updated_at, o.source_event_id
    FROM odds_snapshots o WHERE o.content_hash IS NOT NULL AND o.under_odds IS NOT NULL;
