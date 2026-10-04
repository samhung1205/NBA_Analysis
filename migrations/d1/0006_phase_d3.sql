-- ============================================================
-- Phase D.3 — Bet qualification, risk controls & Kelly sizing (SQLite / Cloudflare D1 方言)
-- 對應 migrations/postgres/0006_phase_d3.sql，欄位名稱與語意一致（僅供本機開發 / smoke test）。
-- ============================================================

CREATE TABLE IF NOT EXISTS bet_sizing_snapshots (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  sizing_version              TEXT NOT NULL,
  risk_policy_version         TEXT NOT NULL,
  kelly_math_version          TEXT NOT NULL,
  portfolio_key               TEXT NOT NULL,
  market_pricing_snapshot_id  INTEGER NOT NULL REFERENCES market_pricing_snapshots(id) ON DELETE CASCADE,
  odds_snapshot_id            INTEGER REFERENCES odds_snapshots(id) ON DELETE CASCADE,
  prediction_id               INTEGER REFERENCES predictions(id) ON DELETE CASCADE,
  pricing_version             TEXT,
  game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  betting_day                 TEXT NOT NULL,
  analysis_as_of              TEXT NOT NULL,
  pricing_analysis_as_of      TEXT NOT NULL,
  source                      TEXT,
  bookmaker                   TEXT,
  market                      TEXT,
  market_type                 TEXT,
  period                      TEXT,
  outcome_set                 TEXT,
  line                        REAL,
  side                        TEXT NOT NULL,
  display_line                REAL,
  settlement_rule             TEXT,
  decimal_odds                REAL,
  p_win                       REAL,
  p_push                      REAL,
  p_loss                      REAL,
  ev_per_unit                 REAL,
  edge_vs_fair                REAL,
  full_kelly_fraction         REAL,
  kelly_multiplier            REAL NOT NULL,
  fractional_kelly_fraction   REAL,
  max_bet_fraction            REAL NOT NULL,
  single_bet_capped_fraction  REAL,
  max_game_fraction           REAL NOT NULL,
  game_exposure_before        REAL NOT NULL,
  game_scale_factor           REAL NOT NULL,
  game_exposure_after         REAL NOT NULL,
  game_adjusted_fraction      REAL NOT NULL,
  max_day_fraction            REAL NOT NULL,
  daily_exposure_before       REAL NOT NULL,
  daily_scale_factor          REAL NOT NULL,
  daily_exposure_after        REAL NOT NULL,
  final_stake_fraction        REAL NOT NULL,
  qualification_status        TEXT NOT NULL,
  mathematically_eligible     INTEGER NOT NULL,
  actionable                  INTEGER NOT NULL,
  reasons                     TEXT NOT NULL DEFAULT '[]',
  warnings                    TEXT NOT NULL DEFAULT '[]',
  odds_fetched_at             TEXT,
  odds_last_seen_at           TEXT,
  quote_age_seconds           REAL,
  last_seen_age_seconds       REAL,
  max_quote_age_seconds       REAL,
  computed_at                 TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_bet_sizing_input
  ON bet_sizing_snapshots(market_pricing_snapshot_id, risk_policy_version, sizing_version, portfolio_key);
CREATE INDEX IF NOT EXISTS idx_bet_sizing_day ON bet_sizing_snapshots(betting_day, risk_policy_version, analysis_as_of DESC);
CREATE INDEX IF NOT EXISTS idx_bet_sizing_game ON bet_sizing_snapshots(game_id, analysis_as_of DESC);
