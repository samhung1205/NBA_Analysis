-- ============================================================
-- Phase D.5 — Decision dashboard, bets-aware exposure & production readiness (SQLite / Cloudflare D1 方言)
-- 對應 migrations/postgres/0008_phase_d5.sql，欄位名稱與語意一致（本機開發 / smoke test 用）。
-- bets 只新增欄位（舊列不受影響）；不可 DELETE、執行欄位不可改（trigger）。
-- ============================================================

CREATE TABLE IF NOT EXISTS bankroll_accounts (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id                 INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
  currency                TEXT NOT NULL DEFAULT 'TWD',
  label                   TEXT,
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  risk_state_version      INTEGER NOT NULL DEFAULT 0,
  risk_state_changed_at   TEXT
);

CREATE TABLE IF NOT EXISTS risk_state_claims (
  account_id              INTEGER NOT NULL REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  version                 INTEGER NOT NULL,
  kind                    TEXT NOT NULL,
  request_id              TEXT,
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (account_id, version)
);

CREATE TABLE IF NOT EXISTS bankroll_day_snapshots (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id              INTEGER NOT NULL REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  betting_day             TEXT NOT NULL,
  day_start_bankroll      REAL NOT NULL,
  basis_as_of             TEXT NOT NULL,
  ledger_balance          REAL NOT NULL,
  open_stake_excluded     REAL NOT NULL,
  ledger_watermark        INTEGER NOT NULL,
  established_reason      TEXT NOT NULL,
  bankroll_version        TEXT NOT NULL,
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  CONSTRAINT bankroll_day_positive_chk CHECK (day_start_bankroll > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bankroll_day ON bankroll_day_snapshots(account_id, betting_day);

CREATE TABLE IF NOT EXISTS decision_snapshots (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id                   INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  account_id                INTEGER REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  betting_day               TEXT NOT NULL,
  as_of                     TEXT NOT NULL,
  last_confirmed_at         TEXT NOT NULL,
  decision_version          TEXT NOT NULL,
  risk_policy_version       TEXT NOT NULL,
  execution_policy_version  TEXT NOT NULL,
  scope                     TEXT NOT NULL,
  input_fingerprint         TEXT NOT NULL,
  risk_state_version        INTEGER,
  ledger_watermark          INTEGER,
  bet_event_watermark       INTEGER,
  bankroll_day_snapshot_id  INTEGER REFERENCES bankroll_day_snapshots(id) ON DELETE RESTRICT,
  status                    TEXT NOT NULL,
  summary                   TEXT NOT NULL DEFAULT '{}',
  bankroll                  TEXT NOT NULL DEFAULT '{}',
  actual_exposure           TEXT NOT NULL DEFAULT '{}',
  risk_limits               TEXT NOT NULL DEFAULT '{}',
  games                     TEXT NOT NULL DEFAULT '[]',
  evidence                  TEXT NOT NULL DEFAULT '{}',
  actual_performance        TEXT NOT NULL DEFAULT '{}',
  warnings                  TEXT NOT NULL DEFAULT '[]',
  created_at                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_snapshot ON decision_snapshots(user_id, betting_day, input_fingerprint);
CREATE INDEX IF NOT EXISTS idx_decision_snapshot_latest ON decision_snapshots(user_id, betting_day, as_of, id);

CREATE TABLE IF NOT EXISTS decision_opportunities (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id                 INTEGER NOT NULL REFERENCES decision_snapshots(id) ON DELETE RESTRICT,
  user_id                     INTEGER NOT NULL,
  betting_day                 TEXT NOT NULL,
  game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
  scope_kind                  TEXT NOT NULL,
  evidence_label              TEXT NOT NULL,
  source                      TEXT NOT NULL,
  bookmaker                   TEXT NOT NULL,
  market                      TEXT NOT NULL,
  market_type                 TEXT,
  period                      TEXT,
  outcome_set                 TEXT,
  side                        TEXT NOT NULL,
  line                        REAL,
  display_line                REAL,
  model_target                TEXT,
  model_threshold             REAL,
  comparator                  TEXT,
  settlement_rule             TEXT,
  decimal_odds                REAL,
  odds_snapshot_id            INTEGER,
  odds_fetched_at             TEXT,
  odds_last_seen_at           TEXT,
  pricing_snapshot_id         INTEGER,
  sizing_snapshot_id          INTEGER,
  prediction_id               INTEGER,
  artifact_version            TEXT,
  pricing_version             TEXT,
  raw_implied_prob            REAL,
  fair_no_vig_prob            REAL,
  market_overround            REAL,
  model_prob                  REAL,
  push_prob                   REAL,
  loss_prob                   REAL,
  edge_vs_fair                REAL,
  ev_per_unit                 REAL,
  d3_qualification_status     TEXT,
  full_kelly_fraction         REAL,
  fractional_kelly_fraction   REAL,
  single_bet_capped_fraction  REAL,
  theoretical_final_fraction  REAL,
  actual_game_exposure        REAL,
  remaining_game_fraction     REAL,
  remaining_day_fraction      REAL,
  game_scale_factor           REAL,
  day_scale_factor            REAL,
  user_adjusted_fraction      REAL,
  day_start_bankroll          REAL,
  max_additional_stake_amount REAL,
  suggested_stake_amount      REAL,
  linked_bet_ids              TEXT NOT NULL DEFAULT '[]',
  linked_actual_fraction      REAL,
  paper_decision_id           INTEGER,
  decision_status             TEXT NOT NULL,
  status_group                TEXT NOT NULL,
  display_rank                INTEGER,
  reasons                     TEXT NOT NULL DEFAULT '[]',
  warnings                    TEXT NOT NULL DEFAULT '[]',
  created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_decision_opp_snapshot ON decision_opportunities(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_decision_opp_game ON decision_opportunities(game_id);

-- bets 擴充（SQLite：一次一欄；REFERENCES 欄位預設 NULL）
ALTER TABLE bets ADD COLUMN account_id INTEGER REFERENCES bankroll_accounts(id);
ALTER TABLE bets ADD COLUMN betting_day TEXT;
ALTER TABLE bets ADD COLUMN source TEXT;
ALTER TABLE bets ADD COLUMN bookmaker TEXT;
ALTER TABLE bets ADD COLUMN market_type TEXT;
ALTER TABLE bets ADD COLUMN period TEXT;
ALTER TABLE bets ADD COLUMN outcome_set TEXT;
ALTER TABLE bets ADD COLUMN model_target TEXT;
ALTER TABLE bets ADD COLUMN model_threshold REAL;
ALTER TABLE bets ADD COLUMN comparator TEXT;
ALTER TABLE bets ADD COLUMN settlement_rule TEXT;
ALTER TABLE bets ADD COLUMN origin TEXT CHECK (origin IS NULL OR origin IN ('platform_opportunity', 'paper_decision', 'manual_unlinked'));
ALTER TABLE bets ADD COLUMN strategy_compliance TEXT CHECK (strategy_compliance IS NULL OR strategy_compliance IN
  ('compliant', 'manual_unlinked', 'user_override', 'outside_model', 'missing_context'));
ALTER TABLE bets ADD COLUMN reference_odds_snapshot_id INTEGER REFERENCES odds_snapshots(id);
ALTER TABLE bets ADD COLUMN reference_decimal_odds REAL;
ALTER TABLE bets ADD COLUMN reference_pricing_snapshot_id INTEGER;
ALTER TABLE bets ADD COLUMN reference_sizing_snapshot_id INTEGER;
ALTER TABLE bets ADD COLUMN decision_snapshot_id INTEGER REFERENCES decision_snapshots(id);
ALTER TABLE bets ADD COLUMN decision_opportunity_id INTEGER REFERENCES decision_opportunities(id);
ALTER TABLE bets ADD COLUMN paper_decision_id INTEGER;
ALTER TABLE bets ADD COLUMN reference_context TEXT;
ALTER TABLE bets ADD COLUMN bankroll_day_snapshot_id INTEGER REFERENCES bankroll_day_snapshots(id);
ALTER TABLE bets ADD COLUMN stake_fraction_at_placement REAL;
ALTER TABLE bets ADD COLUMN risk_check TEXT;
ALTER TABLE bets ADD COLUMN override_reason TEXT;
ALTER TABLE bets ADD COLUMN override_confirmed_at TEXT;
ALTER TABLE bets ADD COLUMN client_request_id TEXT;
ALTER TABLE bets ADD COLUMN recorded_at TEXT;
ALTER TABLE bets ADD COLUMN record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'voided', 'superseded'));
ALTER TABLE bets ADD COLUMN voided_at TEXT;
ALTER TABLE bets ADD COLUMN void_reason TEXT;
ALTER TABLE bets ADD COLUMN supersedes_bet_id INTEGER REFERENCES bets(id);
ALTER TABLE bets ADD COLUMN superseded_by_bet_id INTEGER REFERENCES bets(id);
ALTER TABLE bets ADD COLUMN settlement_source TEXT;
ALTER TABLE bets ADD COLUMN settlement_reason TEXT;
ALTER TABLE bets ADD COLUMN settled_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bets_request ON bets(user_id, client_request_id) WHERE client_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bets_user_day ON bets(user_id, betting_day);
CREATE INDEX IF NOT EXISTS idx_bets_opportunity ON bets(decision_opportunity_id);

CREATE TABLE IF NOT EXISTS bet_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  bet_id        INTEGER NOT NULL REFERENCES bets(id) ON DELETE RESTRICT,
  user_id       INTEGER,
  event_type    TEXT NOT NULL,
  actor         TEXT NOT NULL,
  payload       TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_bet_events_bet ON bet_events(bet_id, id);

CREATE TABLE IF NOT EXISTS bankroll_ledger (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id          INTEGER NOT NULL REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  entry_type          TEXT NOT NULL,
  amount              REAL,
  reverses_entry_id   INTEGER REFERENCES bankroll_ledger(id) ON DELETE RESTRICT,
  bet_id              INTEGER REFERENCES bets(id) ON DELETE RESTRICT,
  settlement_key      TEXT,
  reason              TEXT,
  recorded_by         TEXT NOT NULL,
  client_request_id   TEXT,
  recorded_at         TEXT NOT NULL,
  CONSTRAINT ledger_type_chk CHECK (entry_type IN ('initial_funding', 'deposit', 'withdrawal', 'adjustment', 'reversal',
                                                   'bet_settlement', 'bet_settlement_reversal')),
  CONSTRAINT ledger_amount_chk CHECK (
    (entry_type IN ('initial_funding', 'deposit', 'withdrawal') AND amount IS NOT NULL AND amount > 0)
    OR (entry_type = 'adjustment' AND amount IS NOT NULL AND amount <> 0 AND reason IS NOT NULL)
    OR (entry_type = 'reversal' AND amount IS NULL AND reverses_entry_id IS NOT NULL AND reason IS NOT NULL)
    OR (entry_type IN ('bet_settlement', 'bet_settlement_reversal') AND amount IS NOT NULL AND bet_id IS NOT NULL
        AND settlement_key IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_request ON bankroll_ledger(account_id, client_request_id) WHERE client_request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_settlement ON bankroll_ledger(settlement_key) WHERE settlement_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_reversal ON bankroll_ledger(reverses_entry_id) WHERE reverses_entry_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_initial ON bankroll_ledger(account_id) WHERE entry_type = 'initial_funding';
CREATE INDEX IF NOT EXISTS idx_ledger_account ON bankroll_ledger(account_id, id);

-- 不可變性 / 稽核 -------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_bets_nodelete BEFORE DELETE ON bets
BEGIN SELECT RAISE(ABORT, 'bets rows are never deleted; use void / correction'); END;
CREATE TRIGGER IF NOT EXISTS trg_bets_execution_immutable BEFORE UPDATE ON bets
WHEN NEW.user_id IS NOT OLD.user_id OR NEW.game_id IS NOT OLD.game_id OR NEW.market IS NOT OLD.market
  OR NEW.selection IS NOT OLD.selection OR NEW.line IS NOT OLD.line OR NEW.odds IS NOT OLD.odds
  OR NEW.stake IS NOT OLD.stake OR NEW.placed_at IS NOT OLD.placed_at OR NEW.account_id IS NOT OLD.account_id
  OR NEW.betting_day IS NOT OLD.betting_day OR NEW.source IS NOT OLD.source OR NEW.bookmaker IS NOT OLD.bookmaker
  OR NEW.market_type IS NOT OLD.market_type OR NEW.period IS NOT OLD.period OR NEW.outcome_set IS NOT OLD.outcome_set
  OR NEW.model_target IS NOT OLD.model_target OR NEW.model_threshold IS NOT OLD.model_threshold
  OR NEW.comparator IS NOT OLD.comparator OR NEW.settlement_rule IS NOT OLD.settlement_rule
  OR NEW.origin IS NOT OLD.origin OR NEW.strategy_compliance IS NOT OLD.strategy_compliance
  OR NEW.reference_odds_snapshot_id IS NOT OLD.reference_odds_snapshot_id
  OR NEW.reference_decimal_odds IS NOT OLD.reference_decimal_odds
  OR NEW.reference_pricing_snapshot_id IS NOT OLD.reference_pricing_snapshot_id
  OR NEW.reference_sizing_snapshot_id IS NOT OLD.reference_sizing_snapshot_id
  OR NEW.decision_snapshot_id IS NOT OLD.decision_snapshot_id
  OR NEW.decision_opportunity_id IS NOT OLD.decision_opportunity_id
  OR NEW.paper_decision_id IS NOT OLD.paper_decision_id OR NEW.reference_context IS NOT OLD.reference_context
  OR NEW.risk_check IS NOT OLD.risk_check OR NEW.override_reason IS NOT OLD.override_reason
  OR NEW.override_confirmed_at IS NOT OLD.override_confirmed_at OR NEW.client_request_id IS NOT OLD.client_request_id
  OR NEW.recorded_at IS NOT OLD.recorded_at OR NEW.supersedes_bet_id IS NOT OLD.supersedes_bet_id
BEGIN SELECT RAISE(ABORT, 'bet execution fields are immutable; record a correction instead'); END;
CREATE TRIGGER IF NOT EXISTS trg_bets_fill_once BEFORE UPDATE ON bets
WHEN (OLD.bankroll_day_snapshot_id IS NOT NULL AND NEW.bankroll_day_snapshot_id IS NOT OLD.bankroll_day_snapshot_id)
  OR (OLD.stake_fraction_at_placement IS NOT NULL AND NEW.stake_fraction_at_placement IS NOT OLD.stake_fraction_at_placement)
  OR (OLD.superseded_by_bet_id IS NOT NULL AND NEW.superseded_by_bet_id IS NOT OLD.superseded_by_bet_id)
BEGIN SELECT RAISE(ABORT, 'bet bankroll basis / supersession can only be filled once'); END;
CREATE TRIGGER IF NOT EXISTS trg_bets_record_status BEFORE UPDATE ON bets
WHEN OLD.record_status <> 'active' AND (NEW.record_status IS NOT OLD.record_status OR NEW.result IS NOT OLD.result
  OR NEW.payout IS NOT OLD.payout OR NEW.settled_at IS NOT OLD.settled_at OR NEW.settlement_source IS NOT OLD.settlement_source
  OR NEW.voided_at IS NOT OLD.voided_at OR NEW.void_reason IS NOT OLD.void_reason)
BEGIN SELECT RAISE(ABORT, 'voided / superseded bets are frozen'); END;

CREATE TRIGGER IF NOT EXISTS trg_bet_events_noupdate BEFORE UPDATE ON bet_events
BEGIN SELECT RAISE(ABORT, 'bet_events rows are immutable (D.5 audit trail)'); END;
CREATE TRIGGER IF NOT EXISTS trg_bet_events_nodelete BEFORE DELETE ON bet_events
BEGIN SELECT RAISE(ABORT, 'bet_events rows are immutable (D.5 audit trail)'); END;

CREATE TRIGGER IF NOT EXISTS trg_bankroll_account_nodelete BEFORE DELETE ON bankroll_accounts
BEGIN SELECT RAISE(ABORT, 'bankroll_accounts rows are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS trg_bankroll_account_identity BEFORE UPDATE ON bankroll_accounts
WHEN NEW.user_id IS NOT OLD.user_id OR NEW.currency IS NOT OLD.currency OR NEW.created_at IS NOT OLD.created_at
  OR NEW.risk_state_version < OLD.risk_state_version
BEGIN SELECT RAISE(ABORT, 'bankroll account identity is immutable; risk_state_version only increases'); END;

CREATE TRIGGER IF NOT EXISTS trg_risk_claims_noupdate BEFORE UPDATE ON risk_state_claims
BEGIN SELECT RAISE(ABORT, 'risk_state_claims rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_risk_claims_nodelete BEFORE DELETE ON risk_state_claims
BEGIN SELECT RAISE(ABORT, 'risk_state_claims rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_bankroll_ledger_noupdate BEFORE UPDATE ON bankroll_ledger
BEGIN SELECT RAISE(ABORT, 'bankroll_ledger rows are immutable (D.5 audit trail)'); END;
CREATE TRIGGER IF NOT EXISTS trg_bankroll_ledger_nodelete BEFORE DELETE ON bankroll_ledger
BEGIN SELECT RAISE(ABORT, 'bankroll_ledger rows are immutable (D.5 audit trail)'); END;
CREATE TRIGGER IF NOT EXISTS trg_bankroll_day_noupdate BEFORE UPDATE ON bankroll_day_snapshots
BEGIN SELECT RAISE(ABORT, 'bankroll_day_snapshots rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_bankroll_day_nodelete BEFORE DELETE ON bankroll_day_snapshots
BEGIN SELECT RAISE(ABORT, 'bankroll_day_snapshots rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_decision_snapshot_nodelete BEFORE DELETE ON decision_snapshots
BEGIN SELECT RAISE(ABORT, 'decision_snapshots rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_snapshot_noupdate BEFORE UPDATE ON decision_snapshots
WHEN NEW.input_fingerprint IS NOT OLD.input_fingerprint OR NEW.status IS NOT OLD.status OR NEW.as_of IS NOT OLD.as_of
  OR NEW.summary IS NOT OLD.summary OR NEW.games IS NOT OLD.games OR NEW.bankroll IS NOT OLD.bankroll
  OR NEW.actual_exposure IS NOT OLD.actual_exposure OR NEW.risk_state_version IS NOT OLD.risk_state_version
  OR NEW.ledger_watermark IS NOT OLD.ledger_watermark OR NEW.bet_event_watermark IS NOT OLD.bet_event_watermark
  OR NEW.user_id IS NOT OLD.user_id OR NEW.betting_day IS NOT OLD.betting_day OR NEW.last_confirmed_at < OLD.last_confirmed_at
BEGIN SELECT RAISE(ABORT, 'decision snapshot is immutable (only last_confirmed_at may advance)'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_opp_noupdate BEFORE UPDATE ON decision_opportunities
BEGIN SELECT RAISE(ABORT, 'decision_opportunities rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_decision_opp_nodelete BEFORE DELETE ON decision_opportunities
BEGIN SELECT RAISE(ABORT, 'decision_opportunities rows are immutable'); END;
