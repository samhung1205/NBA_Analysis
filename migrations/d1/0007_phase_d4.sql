-- ============================================================
-- Phase D.4 — Prospective paper-decision ledger (SQLite / Cloudflare D1 方言)
-- 對應 migrations/postgres/0007_phase_d4.sql，欄位名稱與語意一致（僅供本機開發 / smoke test；Node 端目前不讀）。
-- 不修改既有表、不寫入 bets。
-- ============================================================

CREATE TABLE IF NOT EXISTS paper_strategy_days (
  strategy_id                TEXT NOT NULL,
  betting_day                TEXT NOT NULL,                 -- Asia/Taipei 開賽日期
  day_start_bankroll         REAL NOT NULL,     -- normalized；當日 stake 基準
  established_at             TEXT NOT NULL,          -- 第一筆 decision 的 decision_time（as-of 時點）
  established_by_game_id     INTEGER REFERENCES games(id) ON DELETE SET NULL,
  starting_bankroll_units    REAL NOT NULL,     -- 策略起始 bankroll（1.0）
  prior_resolved_profit      REAL NOT NULL,     -- established_at 以前已結算的累積損益
  prior_unresolved_stake     REAL NOT NULL,     -- established_at 時仍未結算的前日 stake（不計入）
  created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (strategy_id, betting_day)
);

CREATE TABLE IF NOT EXISTS paper_strategy_decisions (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id                 TEXT NOT NULL,
  execution_policy_version    TEXT NOT NULL,                -- execution-v1
  risk_policy_version         TEXT NOT NULL,                -- risk-v1
  sizing_version              TEXT NOT NULL,
  pricing_version             TEXT NOT NULL,
  kelly_math_version          TEXT NOT NULL,
  strategy_scope              TEXT NOT NULL,                -- primary / diagnostic
  evidence_label              TEXT NOT NULL,                -- taiwan_sports_lottery_strategy / international_market_diagnostic
  source                      TEXT NOT NULL,
  bookmaker                   TEXT NOT NULL,
  game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
  betting_day                 TEXT NOT NULL,
  scheduled_tipoff            TEXT NOT NULL,         -- 決策當時已知的開賽時間
  decision_time               TEXT NOT NULL,         -- T = scheduled_tipoff − 60 分（analysis_as_of）
  evaluated_at                TEXT NOT NULL,         -- job 實際執行時間（只稽核；不影響資料選取）
  evaluation_lag_seconds      REAL NOT NULL,
  decision_status             TEXT NOT NULL,                -- bet / no_bet
  no_bet_reason               TEXT,                         -- no_odds / stale_odds / no_prediction / no_positive_ev / …
  blockers                    TEXT NOT NULL DEFAULT '{}',
  odds_snapshot_ids           TEXT NOT NULL DEFAULT '[]',   -- T 時點每個 series 最新的 snapshot id
  prediction_id               INTEGER REFERENCES predictions(id) ON DELETE RESTRICT,
  prediction_available_at     TEXT,
  artifact_version            TEXT,
  model_version               TEXT,
  distribution_version        TEXT,
  pricing_fingerprint         TEXT NOT NULL,                -- sha256(重建的 D.2 定價)
  sizing_fingerprint          TEXT NOT NULL,                -- sha256(重建的 D.3 sizing)
  sizing                      TEXT NOT NULL DEFAULT '[]',   -- 每個 outcome 的 D.3 結果摘要
  day_start_bankroll          REAL NOT NULL,
  committed_fraction_before   REAL NOT NULL,
  remaining_day_fraction_before REAL NOT NULL,
  execution_scale_factor      REAL NOT NULL,
  total_stake_fraction        REAL NOT NULL,
  total_stake_units           REAL NOT NULL,
  created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
  CONSTRAINT paper_decision_status_chk CHECK (decision_status IN ('bet', 'no_bet')),
  CONSTRAINT paper_decision_reason_chk CHECK ((decision_status = 'no_bet') = (no_bet_reason IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_decision ON paper_strategy_decisions(strategy_id, game_id);
CREATE INDEX IF NOT EXISTS idx_paper_decision_day ON paper_strategy_decisions(strategy_id, betting_day, decision_time);

CREATE TABLE IF NOT EXISTS paper_strategy_bets (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id                 INTEGER NOT NULL REFERENCES paper_strategy_decisions(id) ON DELETE RESTRICT,
  strategy_id                 TEXT NOT NULL,
  game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
  betting_day                 TEXT NOT NULL,
  decision_time               TEXT NOT NULL,
  scheduled_tipoff            TEXT NOT NULL,
  odds_snapshot_id            INTEGER NOT NULL REFERENCES odds_snapshots(id) ON DELETE RESTRICT,
  prediction_id               INTEGER REFERENCES predictions(id) ON DELETE RESTRICT,
  source                      TEXT NOT NULL,
  bookmaker                   TEXT NOT NULL,
  market                      TEXT NOT NULL,
  market_type                 TEXT NOT NULL,
  period                      TEXT NOT NULL,
  outcome_set                 TEXT NOT NULL,
  side                        TEXT NOT NULL,
  line                        REAL,
  display_line                REAL,
  model_target                TEXT NOT NULL,                -- 結算用（下注當時的 canonical 定義；不重查盤口）
  model_threshold             REAL NOT NULL,
  comparator                  TEXT NOT NULL,
  settlement_rule             TEXT NOT NULL,
  decimal_odds                REAL NOT NULL,
  p_win                       REAL NOT NULL,
  p_push                      REAL NOT NULL,
  p_loss                      REAL NOT NULL,
  ev_per_unit                 REAL NOT NULL,
  sizing_final_stake_fraction REAL NOT NULL,    -- D.3 risk-v1 static
  execution_scale_factor      REAL NOT NULL,    -- execution-v1 剩餘當日額度
  stake_fraction              REAL NOT NULL,
  stake_units                 REAL NOT NULL,
  expected_profit_units       REAL NOT NULL,
  settlement_status           TEXT NOT NULL DEFAULT 'pending',  -- pending / settled_win / settled_loss / settled_push /
                                                                -- settled_draw_win / void / ungradable
  settlement_reason           TEXT,
  settlement_version          TEXT,
  actual_value                REAL,
  home_score                  INTEGER,
  away_score                  INTEGER,
  profit_units                REAL,
  settled_at                  TEXT,
  created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
  CONSTRAINT paper_bet_status_chk CHECK (settlement_status IN
    ('pending', 'settled_win', 'settled_loss', 'settled_push', 'settled_draw_win', 'void', 'ungradable'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_bet ON paper_strategy_bets(decision_id, odds_snapshot_id, side);
CREATE INDEX IF NOT EXISTS idx_paper_bet_open ON paper_strategy_bets(strategy_id, settlement_status);
CREATE INDEX IF NOT EXISTS idx_paper_bet_day ON paper_strategy_bets(strategy_id, betting_day);

-- 不可變性 -------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_paper_days_noupdate BEFORE UPDATE ON paper_strategy_days
BEGIN SELECT RAISE(ABORT, 'paper_strategy_days rows are immutable (paper ledger)'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_days_nodelete BEFORE DELETE ON paper_strategy_days
BEGIN SELECT RAISE(ABORT, 'paper_strategy_days rows are immutable (paper ledger)'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_decisions_noupdate BEFORE UPDATE ON paper_strategy_decisions
BEGIN SELECT RAISE(ABORT, 'paper_strategy_decisions rows are immutable (paper ledger)'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_decisions_nodelete BEFORE DELETE ON paper_strategy_decisions
BEGIN SELECT RAISE(ABORT, 'paper_strategy_decisions rows are immutable (paper ledger)'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_bets_settled BEFORE UPDATE ON paper_strategy_bets
WHEN OLD.settlement_status IN ('settled_win', 'settled_loss', 'settled_push', 'settled_draw_win', 'void')
  OR NEW.decision_id IS NOT OLD.decision_id OR NEW.odds_snapshot_id IS NOT OLD.odds_snapshot_id
  OR NEW.side IS NOT OLD.side OR NEW.decimal_odds IS NOT OLD.decimal_odds OR NEW.stake_units IS NOT OLD.stake_units
  OR NEW.stake_fraction IS NOT OLD.stake_fraction OR NEW.model_threshold IS NOT OLD.model_threshold
BEGIN SELECT RAISE(ABORT, 'paper bet settlement / execution fields are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_paper_bets_nodelete BEFORE DELETE ON paper_strategy_bets
BEGIN SELECT RAISE(ABORT, 'paper_strategy_bets rows are immutable (paper ledger)'); END;
