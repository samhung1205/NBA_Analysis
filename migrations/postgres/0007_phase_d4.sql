-- ============================================================
-- Phase D.4 — Prospective paper-decision ledger (PostgreSQL)
-- 純新增三張表；不修改既有表、不寫入 bets（bets = 使用者真正手動下注；paper strategy 完全分開）。
--
--   paper_strategy_days       每個 strategy × betting day 的 day_start_bankroll（第一筆 decision 時凍結，不可改）
--   paper_strategy_decisions  每個 strategy × game 唯一一筆 T-60 decision（含 no_bet 與原因；建立後不可改 / 刪）
--   paper_strategy_bets       decision 底下理論執行的 paper bet；結算狀態只能從 pending / ungradable 前進，
--                             結算完成（settled_* / void）後不可改
--
-- strategy_id = execution / risk / sizing / pricing / kelly 版本 + 單一 source:bookmaker
--   （例：execution-v1/risk-v1/sizing-v1/pricing-v1/kelly-push-v1/twsport:twsport）
-- 所有 bankroll / stake 都是 normalized 單位（起始 bankroll = 1.0）；不保存真實金額。
-- ============================================================

CREATE TABLE IF NOT EXISTS paper_strategy_days (
  strategy_id                TEXT NOT NULL,
  betting_day                DATE NOT NULL,                 -- Asia/Taipei 開賽日期
  day_start_bankroll         DOUBLE PRECISION NOT NULL,     -- normalized；當日 stake 基準
  established_at             TIMESTAMPTZ NOT NULL,          -- 第一筆 decision 的 decision_time（as-of 時點）
  established_by_game_id     INTEGER REFERENCES games(id) ON DELETE SET NULL,
  starting_bankroll_units    DOUBLE PRECISION NOT NULL,     -- 策略起始 bankroll（1.0）
  prior_resolved_profit      DOUBLE PRECISION NOT NULL,     -- established_at 以前已結算的累積損益
  prior_unresolved_stake     DOUBLE PRECISION NOT NULL,     -- established_at 時仍未結算的前日 stake（不計入）
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (strategy_id, betting_day)
);

CREATE TABLE IF NOT EXISTS paper_strategy_decisions (
  id                          BIGSERIAL PRIMARY KEY,
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
  betting_day                 DATE NOT NULL,
  scheduled_tipoff            TIMESTAMPTZ NOT NULL,         -- 決策當時已知的開賽時間
  decision_time               TIMESTAMPTZ NOT NULL,         -- T = scheduled_tipoff − 60 分（analysis_as_of）
  evaluated_at                TIMESTAMPTZ NOT NULL,         -- job 實際執行時間（只稽核；不影響資料選取）
  evaluation_lag_seconds      DOUBLE PRECISION NOT NULL,
  decision_status             TEXT NOT NULL,                -- bet / no_bet
  no_bet_reason               TEXT,                         -- no_odds / stale_odds / no_prediction / no_positive_ev / …
  blockers                    JSONB NOT NULL DEFAULT '{}'::jsonb,
  odds_snapshot_ids           JSONB NOT NULL DEFAULT '[]'::jsonb,   -- T 時點每個 series 最新的 snapshot id
  prediction_id               INTEGER REFERENCES predictions(id) ON DELETE RESTRICT,
  prediction_available_at     TIMESTAMPTZ,
  artifact_version            TEXT,
  model_version               TEXT,
  distribution_version        TEXT,
  pricing_fingerprint         TEXT NOT NULL,                -- sha256(重建的 D.2 定價)
  sizing_fingerprint          TEXT NOT NULL,                -- sha256(重建的 D.3 sizing)
  sizing                      JSONB NOT NULL DEFAULT '[]'::jsonb,   -- 每個 outcome 的 D.3 結果摘要
  day_start_bankroll          DOUBLE PRECISION NOT NULL,
  committed_fraction_before   DOUBLE PRECISION NOT NULL,
  remaining_day_fraction_before DOUBLE PRECISION NOT NULL,
  execution_scale_factor      DOUBLE PRECISION NOT NULL,
  total_stake_fraction        DOUBLE PRECISION NOT NULL,
  total_stake_units           DOUBLE PRECISION NOT NULL,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT paper_decision_status_chk CHECK (decision_status IN ('bet', 'no_bet')),
  CONSTRAINT paper_decision_reason_chk CHECK ((decision_status = 'no_bet') = (no_bet_reason IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_decision ON paper_strategy_decisions(strategy_id, game_id);
CREATE INDEX IF NOT EXISTS idx_paper_decision_day ON paper_strategy_decisions(strategy_id, betting_day, decision_time);

CREATE TABLE IF NOT EXISTS paper_strategy_bets (
  id                          BIGSERIAL PRIMARY KEY,
  decision_id                 BIGINT NOT NULL REFERENCES paper_strategy_decisions(id) ON DELETE RESTRICT,
  strategy_id                 TEXT NOT NULL,
  game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
  betting_day                 DATE NOT NULL,
  decision_time               TIMESTAMPTZ NOT NULL,
  scheduled_tipoff            TIMESTAMPTZ NOT NULL,
  odds_snapshot_id            INTEGER NOT NULL REFERENCES odds_snapshots(id) ON DELETE RESTRICT,
  prediction_id               INTEGER REFERENCES predictions(id) ON DELETE RESTRICT,
  source                      TEXT NOT NULL,
  bookmaker                   TEXT NOT NULL,
  market                      TEXT NOT NULL,
  market_type                 TEXT NOT NULL,
  period                      TEXT NOT NULL,
  outcome_set                 TEXT NOT NULL,
  side                        TEXT NOT NULL,
  line                        DOUBLE PRECISION,
  display_line                DOUBLE PRECISION,
  model_target                TEXT NOT NULL,                -- 結算用（下注當時的 canonical 定義；不重查盤口）
  model_threshold             DOUBLE PRECISION NOT NULL,
  comparator                  TEXT NOT NULL,
  settlement_rule             TEXT NOT NULL,
  decimal_odds                DOUBLE PRECISION NOT NULL,
  p_win                       DOUBLE PRECISION NOT NULL,
  p_push                      DOUBLE PRECISION NOT NULL,
  p_loss                      DOUBLE PRECISION NOT NULL,
  ev_per_unit                 DOUBLE PRECISION NOT NULL,
  sizing_final_stake_fraction DOUBLE PRECISION NOT NULL,    -- D.3 risk-v1 static
  execution_scale_factor      DOUBLE PRECISION NOT NULL,    -- execution-v1 剩餘當日額度
  stake_fraction              DOUBLE PRECISION NOT NULL,
  stake_units                 DOUBLE PRECISION NOT NULL,
  expected_profit_units       DOUBLE PRECISION NOT NULL,
  settlement_status           TEXT NOT NULL DEFAULT 'pending',  -- pending / settled_win / settled_loss / settled_push /
                                                                -- settled_draw_win / void / ungradable
  settlement_reason           TEXT,
  settlement_version          TEXT,
  actual_value                DOUBLE PRECISION,
  home_score                  INTEGER,
  away_score                  INTEGER,
  profit_units                DOUBLE PRECISION,
  settled_at                  TIMESTAMPTZ,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT paper_bet_status_chk CHECK (settlement_status IN
    ('pending', 'settled_win', 'settled_loss', 'settled_push', 'settled_draw_win', 'void', 'ungradable'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_bet ON paper_strategy_bets(decision_id, odds_snapshot_id, side);
CREATE INDEX IF NOT EXISTS idx_paper_bet_open ON paper_strategy_bets(strategy_id, settlement_status);
CREATE INDEX IF NOT EXISTS idx_paper_bet_day ON paper_strategy_bets(strategy_id, betting_day);

-- 不可變性 -------------------------------------------------------
CREATE OR REPLACE FUNCTION paper_ledger_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% rows are immutable (paper ledger)', TG_TABLE_NAME;
END $$;

CREATE OR REPLACE FUNCTION paper_bet_settlement_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.settlement_status IN ('settled_win', 'settled_loss', 'settled_push', 'settled_draw_win', 'void') THEN
    RAISE EXCEPTION 'paper bet % already settled (%); settlement is immutable', OLD.id, OLD.settlement_status;
  END IF;
  IF ROW(NEW.decision_id, NEW.strategy_id, NEW.game_id, NEW.odds_snapshot_id, NEW.side, NEW.decimal_odds,
         NEW.model_threshold, NEW.comparator, NEW.settlement_rule, NEW.stake_fraction, NEW.stake_units)
     IS DISTINCT FROM
     ROW(OLD.decision_id, OLD.strategy_id, OLD.game_id, OLD.odds_snapshot_id, OLD.side, OLD.decimal_odds,
         OLD.model_threshold, OLD.comparator, OLD.settlement_rule, OLD.stake_fraction, OLD.stake_units) THEN
    RAISE EXCEPTION 'paper bet % execution fields are immutable', OLD.id;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_paper_days_immutable ON paper_strategy_days;
CREATE TRIGGER trg_paper_days_immutable BEFORE UPDATE OR DELETE ON paper_strategy_days
  FOR EACH ROW EXECUTE FUNCTION paper_ledger_immutable();
DROP TRIGGER IF EXISTS trg_paper_decisions_immutable ON paper_strategy_decisions;
CREATE TRIGGER trg_paper_decisions_immutable BEFORE UPDATE OR DELETE ON paper_strategy_decisions
  FOR EACH ROW EXECUTE FUNCTION paper_ledger_immutable();
DROP TRIGGER IF EXISTS trg_paper_bets_settlement ON paper_strategy_bets;
CREATE TRIGGER trg_paper_bets_settlement BEFORE UPDATE ON paper_strategy_bets
  FOR EACH ROW EXECUTE FUNCTION paper_bet_settlement_guard();
DROP TRIGGER IF EXISTS trg_paper_bets_nodelete ON paper_strategy_bets;
CREATE TRIGGER trg_paper_bets_nodelete BEFORE DELETE ON paper_strategy_bets
  FOR EACH ROW EXECUTE FUNCTION paper_ledger_immutable();
