-- ============================================================
-- Phase D.2 — No-vig market pricing, model edge & EV (SQLite / Cloudflare D1 方言)
-- 對應 migrations/postgres/0005_phase_d2.sql，欄位名稱與語意一致（僅供本機開發 / smoke test）。
-- ============================================================

CREATE TABLE IF NOT EXISTS market_pricing_snapshots (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  pricing_version        TEXT NOT NULL,
  no_vig_method          TEXT NOT NULL,
  odds_snapshot_id       INTEGER NOT NULL REFERENCES odds_snapshots(id) ON DELETE CASCADE,
  prediction_id          INTEGER REFERENCES predictions(id) ON DELETE CASCADE,
  game_id                INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  analysis_as_of         TEXT NOT NULL,
  odds_fetched_at        TEXT NOT NULL,
  prediction_created_at  TEXT,
  prediction_kind        TEXT,
  prediction_profile     TEXT,
  model_version          TEXT,
  artifact_version       TEXT,
  distribution_version   TEXT,
  source                 TEXT NOT NULL,
  bookmaker              TEXT,
  market                 TEXT NOT NULL,
  market_type            TEXT,
  period                 TEXT,
  outcome_set            TEXT,
  line                   REAL,
  away_line              REAL,
  status                 TEXT NOT NULL,
  status_reason          TEXT,
  settlement_rule        TEXT,
  total_raw_implied      REAL,
  market_overround       REAL,
  fair_prob_sum          REAL,
  side                   TEXT NOT NULL,
  display_line           REAL,
  model_target           TEXT,
  model_threshold        REAL,
  comparator             TEXT,
  decimal_odds           REAL,
  raw_implied_prob       REAL,
  fair_no_vig_prob       REAL,
  model_prob             REAL,
  push_prob              REAL,
  loss_prob              REAL,
  edge_vs_fair           REAL,
  ev_per_unit            REAL,
  expected_return        REAL,
  ev_percent             REAL,
  warnings               TEXT NOT NULL DEFAULT '[]',
  diagnostics            TEXT NOT NULL DEFAULT '{}',
  computed_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_market_pricing_input
  ON market_pricing_snapshots(odds_snapshot_id, COALESCE(prediction_id, 0), side, pricing_version);
CREATE INDEX IF NOT EXISTS idx_market_pricing_game ON market_pricing_snapshots(game_id, analysis_as_of DESC);
