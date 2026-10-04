-- ============================================================
-- Phase D.5 — Decision dashboard, bets-aware exposure & production readiness (PostgreSQL)
--
-- 三個概念分開（docs/phase-d5-report.md §3）：
--   model opportunity      market_pricing_snapshots / bet_sizing_snapshots（D.2 / D.3，理論）
--   paper strategy decision paper_strategy_*（D.4 execution-v1，normalized units，策略證據）
--   actual user bet        bets（使用者在外部真的下注後手動記錄；真實金額）
-- 本 migration 不修改 paper_strategy_* / market_pricing_snapshots / bet_sizing_snapshots。
--
--   1. bets（擴充，全部 nullable / 有預設值 → 舊列與舊 API 不受影響）
--        provenance（source / bookmaker / 市場身分 / 參考盤口 / 定價 / sizing / decision 列 / paper decision）、
--        origin、strategy_compliance、override、idempotency key、record_status（active / voided / superseded）、
--        betting_day、bankroll basis、結算來源。
--        執行欄位不可改（trigger）；不可 DELETE（改用 void / correction，保留稽核軌跡）。
--   2. bet_events                 bets 的 append-only 稽核紀錄（recorded / settled / voided / superseded …）
--   3. bankroll_accounts          每位使用者一個手動維護的 strategy bankroll（不連銀行 / bookmaker）；
--                                 risk_state_version = 使用者每次異動 bets / ledger 的版本（物化結果是否仍有效）
--      risk_state_claims          每次異動「認領」下一個版本號（PK = account × version）：兩個分頁同時用同一個版本寫入 →
--                                 第二個違反 PK、整個交易 rollback → 不會兩筆都吃掉同一份剩餘額度
--   4. bankroll_ledger            append-only 金額異動（initial_funding / deposit / withdrawal / adjustment / reversal /
--                                 bet_settlement / bet_settlement_reversal）；沒有可直接覆寫的 balance 欄位
--   5. bankroll_day_snapshots     每個 Asia/Taipei betting day 的 day_start_bankroll（凍結；不可改）
--   6. decision_snapshots         Python 物化的使用者決策檢視（user × betting day × 輸入指紋；新輸入 → 新列，舊列保留）
--   7. decision_opportunities     每個 snapshot 的 outcome（台彩 primary + 國際盤 diagnostic）
--
-- 所有 Kelly / exposure / remaining capacity / bankroll 計算只在 Python（pipeline/core/decision/）；
-- Node API 只讀、序列化、比較 Python 物化的上限（不做乘除）。
-- ============================================================

-- 3. bankroll_accounts（先建立，bets 需要 FK） -----------------------------------
CREATE TABLE IF NOT EXISTS bankroll_accounts (
  id                      BIGSERIAL PRIMARY KEY,
  user_id                 INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
  currency                TEXT NOT NULL DEFAULT 'TWD',
  label                   TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  risk_state_version      BIGINT NOT NULL DEFAULT 0,     -- 使用者每次寫入 bets / ledger 都前進一號（與 risk_state_claims 同一交易）
  risk_state_changed_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS risk_state_claims (
  account_id              BIGINT NOT NULL REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  version                 BIGINT NOT NULL,
  kind                    TEXT NOT NULL,                 -- bet_recorded / bet_voided / bet_corrected / bet_settled_manual / ledger_entry
  request_id              TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, version)
);

-- 5. bankroll_day_snapshots ------------------------------------------------------
CREATE TABLE IF NOT EXISTS bankroll_day_snapshots (
  id                      BIGSERIAL PRIMARY KEY,
  account_id              BIGINT NOT NULL REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  betting_day             DATE NOT NULL,                 -- Asia/Taipei 開賽日期（= D.3 / D.4 betting day）
  day_start_bankroll      DOUBLE PRECISION NOT NULL,     -- 當日所有 stake 比例的分母（凍結）
  basis_as_of             TIMESTAMPTZ NOT NULL,          -- 以此時點的 ledger / 未結算 stake 計算
  ledger_balance          DOUBLE PRECISION NOT NULL,     -- basis_as_of 時 ledger 合計
  open_stake_excluded     DOUBLE PRECISION NOT NULL,     -- basis_as_of 時其他 betting day 仍未結算的 stake（不計入 day start）
  ledger_watermark        BIGINT NOT NULL,               -- basis 用到的最大 ledger id
  established_reason      TEXT NOT NULL,                 -- first_qualified_opportunity / first_recorded_bet
  bankroll_version        TEXT NOT NULL,                 -- bankroll-v1
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT bankroll_day_positive_chk CHECK (day_start_bankroll > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bankroll_day ON bankroll_day_snapshots(account_id, betting_day);

-- 6 / 7. decision snapshots（bets 會參照 opportunity → 先建立） ---------------------
CREATE TABLE IF NOT EXISTS decision_snapshots (
  id                        BIGSERIAL PRIMARY KEY,
  user_id                   INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  account_id                BIGINT REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  betting_day               DATE NOT NULL,
  as_of                     TIMESTAMPTZ NOT NULL,        -- 第一次產生此內容的時點
  last_confirmed_at         TIMESTAMPTZ NOT NULL,        -- 之後排程重算得到相同內容的最後時點（唯一可更新欄位）
  decision_version          TEXT NOT NULL,               -- decision-v1
  risk_policy_version       TEXT NOT NULL,               -- risk-v1（不修改）
  execution_policy_version  TEXT NOT NULL,               -- execution-v1（betting day / day-start 語意）
  scope                     TEXT NOT NULL,               -- twsport:twsport（primary）
  input_fingerprint         TEXT NOT NULL,               -- sha256(物化內容，不含時間戳)
  risk_state_version        BIGINT,                      -- 物化時讀到的 bankroll_accounts.risk_state_version
  ledger_watermark          BIGINT,                      -- 物化時讀到的最大 ledger id
  bet_event_watermark       BIGINT,                      -- 物化時讀到的最大 user 端 bet_events id（無 bankroll 的使用者也能判斷是否過期）
  bankroll_day_snapshot_id  BIGINT REFERENCES bankroll_day_snapshots(id) ON DELETE RESTRICT,
  status                    TEXT NOT NULL,               -- ok / bankroll_unavailable / actual_exposure_incomplete / no_games / no_odds …
  summary                   JSONB NOT NULL DEFAULT '{}'::jsonb,
  bankroll                  JSONB NOT NULL DEFAULT '{}'::jsonb,
  actual_exposure           JSONB NOT NULL DEFAULT '{}'::jsonb,
  risk_limits               JSONB NOT NULL DEFAULT '{}'::jsonb,
  games                     JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence                  JSONB NOT NULL DEFAULT '{}'::jsonb,
  actual_performance        JSONB NOT NULL DEFAULT '{}'::jsonb,
  warnings                  JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_snapshot ON decision_snapshots(user_id, betting_day, input_fingerprint);
CREATE INDEX IF NOT EXISTS idx_decision_snapshot_latest ON decision_snapshots(user_id, betting_day, as_of DESC, id DESC);

CREATE TABLE IF NOT EXISTS decision_opportunities (
  id                          BIGSERIAL PRIMARY KEY,
  snapshot_id                 BIGINT NOT NULL REFERENCES decision_snapshots(id) ON DELETE RESTRICT,
  user_id                     INTEGER NOT NULL,
  betting_day                 DATE NOT NULL,
  game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
  scope_kind                  TEXT NOT NULL,             -- taiwan_primary / international_diagnostic
  evidence_label              TEXT NOT NULL,             -- taiwan_sports_lottery_strategy / international_market_diagnostic
  source                      TEXT NOT NULL,
  bookmaker                   TEXT NOT NULL,
  market                      TEXT NOT NULL,
  market_type                 TEXT,
  period                      TEXT,
  outcome_set                 TEXT,
  side                        TEXT NOT NULL,
  line                        DOUBLE PRECISION,
  display_line                DOUBLE PRECISION,
  model_target                TEXT,
  model_threshold             DOUBLE PRECISION,
  comparator                  TEXT,
  settlement_rule             TEXT,
  decimal_odds                DOUBLE PRECISION,
  odds_snapshot_id            INTEGER,                   -- 參考（odds_snapshots；不加 FK：cache 表可清理）
  odds_fetched_at             TIMESTAMPTZ,
  odds_last_seen_at           TIMESTAMPTZ,
  pricing_snapshot_id         BIGINT,
  sizing_snapshot_id          BIGINT,                    -- 同一定價列最新的 D.3 sizing 列（provenance；D.5 數字以 Python 當下重算為準）
  prediction_id               INTEGER,
  artifact_version            TEXT,
  pricing_version             TEXT,
  raw_implied_prob            DOUBLE PRECISION,
  fair_no_vig_prob            DOUBLE PRECISION,
  market_overround            DOUBLE PRECISION,
  model_prob                  DOUBLE PRECISION,
  push_prob                   DOUBLE PRECISION,
  loss_prob                   DOUBLE PRECISION,
  edge_vs_fair                DOUBLE PRECISION,
  ev_per_unit                 DOUBLE PRECISION,
  d3_qualification_status     TEXT,                      -- 台彩-only 組合在 as_of 的 D.3 qualification
  full_kelly_fraction         DOUBLE PRECISION,
  fractional_kelly_fraction   DOUBLE PRECISION,
  single_bet_capped_fraction  DOUBLE PRECISION,
  theoretical_final_fraction  DOUBLE PRECISION,          -- D.3 risk-v1（無實際下注假設）
  actual_game_exposure        DOUBLE PRECISION,          -- 同場已實際下注 / day_start
  remaining_game_fraction     DOUBLE PRECISION,
  remaining_day_fraction      DOUBLE PRECISION,
  game_scale_factor           DOUBLE PRECISION,
  day_scale_factor            DOUBLE PRECISION,
  user_adjusted_fraction      DOUBLE PRECISION,          -- 納入實際下注後的新增額度（bankroll 比例）
  day_start_bankroll          DOUBLE PRECISION,
  max_additional_stake_amount DOUBLE PRECISION,          -- user_adjusted × day_start（Python）
  suggested_stake_amount      DOUBLE PRECISION,          -- 向下取整到貨幣單位（≤ max_additional）
  linked_bet_ids              JSONB NOT NULL DEFAULT '[]'::jsonb,
  linked_actual_fraction      DOUBLE PRECISION,
  paper_decision_id           BIGINT,
  decision_status             TEXT NOT NULL,             -- qualified / already_recorded / no_positive_ev / stale_odds / no_prediction / no_odds /
                                                         -- unsupported / risk_cap_reached / actual_exposure_over_limit / bankroll_unavailable / data_incomplete
  status_group                TEXT NOT NULL,             -- actionable / review / recorded / blocked / inactive / diagnostic
  display_rank                INTEGER,                   -- 僅 UI 排序（不影響 qualification / stake）
  reasons                     JSONB NOT NULL DEFAULT '[]'::jsonb,
  warnings                    JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_decision_opp_snapshot ON decision_opportunities(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_decision_opp_game ON decision_opportunities(game_id);

-- 1. bets 擴充 -------------------------------------------------------------------
ALTER TABLE bets
  ADD COLUMN IF NOT EXISTS account_id                     BIGINT REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS betting_day                    DATE,              -- 記錄時的 Asia/Taipei betting day（舊列 NULL → 由比賽日期導出）
  ADD COLUMN IF NOT EXISTS source                         TEXT,              -- twsport / oddsapi / other；舊列 NULL（legacy_unlinked）
  ADD COLUMN IF NOT EXISTS bookmaker                      TEXT,
  ADD COLUMN IF NOT EXISTS market_type                    TEXT,              -- 以下 canonical 身分：只從定價列複製（不在 Node 計算正負號）
  ADD COLUMN IF NOT EXISTS period                         TEXT,
  ADD COLUMN IF NOT EXISTS outcome_set                    TEXT,
  ADD COLUMN IF NOT EXISTS model_target                   TEXT,
  ADD COLUMN IF NOT EXISTS model_threshold                DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS comparator                     TEXT,
  ADD COLUMN IF NOT EXISTS settlement_rule                TEXT,
  ADD COLUMN IF NOT EXISTS origin                         TEXT,              -- platform_opportunity / paper_decision / manual_unlinked；舊列 NULL
  ADD COLUMN IF NOT EXISTS strategy_compliance            TEXT,              -- compliant / manual_unlinked / user_override / outside_model / missing_context
  ADD COLUMN IF NOT EXISTS reference_odds_snapshot_id     INTEGER REFERENCES odds_snapshots(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS reference_decimal_odds         DOUBLE PRECISION,  -- 平台上觀察到的賠率（actual odds 存在 odds 欄位）
  ADD COLUMN IF NOT EXISTS reference_pricing_snapshot_id  BIGINT,
  ADD COLUMN IF NOT EXISTS reference_sizing_snapshot_id   BIGINT,
  ADD COLUMN IF NOT EXISTS decision_snapshot_id           BIGINT REFERENCES decision_snapshots(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS decision_opportunity_id        BIGINT REFERENCES decision_opportunities(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS paper_decision_id              BIGINT,
  ADD COLUMN IF NOT EXISTS reference_context              JSONB,             -- 記錄當下看到的 EV / 模型機率 / 建議額度等（複製，不重算）
  ADD COLUMN IF NOT EXISTS bankroll_day_snapshot_id       BIGINT REFERENCES bankroll_day_snapshots(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS stake_fraction_at_placement    DOUBLE PRECISION,  -- stake / day_start（Python 填一次）
  ADD COLUMN IF NOT EXISTS risk_check                     JSONB,             -- 寫入前 server-side 驗證結果
  ADD COLUMN IF NOT EXISTS override_reason                TEXT,
  ADD COLUMN IF NOT EXISTS override_confirmed_at          TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS client_request_id              TEXT,              -- idempotency key
  ADD COLUMN IF NOT EXISTS recorded_at                    TIMESTAMPTZ,       -- 伺服器寫入時間（placed_at 為使用者宣告的下注時間）；舊列 NULL
  ADD COLUMN IF NOT EXISTS record_status                  TEXT NOT NULL DEFAULT 'active',   -- active / voided / superseded
  ADD COLUMN IF NOT EXISTS voided_at                      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS void_reason                    TEXT,
  ADD COLUMN IF NOT EXISTS supersedes_bet_id              INTEGER REFERENCES bets(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS superseded_by_bet_id           INTEGER REFERENCES bets(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS settlement_source              TEXT,              -- manual / settle-v1
  ADD COLUMN IF NOT EXISTS settlement_reason              TEXT,
  ADD COLUMN IF NOT EXISTS settled_at                     TIMESTAMPTZ;

DO $$ BEGIN
  ALTER TABLE bets ADD CONSTRAINT bets_record_status_chk CHECK (record_status IN ('active', 'voided', 'superseded'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE bets ADD CONSTRAINT bets_origin_chk CHECK (origin IS NULL OR origin IN ('platform_opportunity', 'paper_decision', 'manual_unlinked'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE bets ADD CONSTRAINT bets_compliance_chk CHECK (strategy_compliance IS NULL OR strategy_compliance IN
    ('compliant', 'manual_unlinked', 'user_override', 'outside_model', 'missing_context'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bets_request ON bets(user_id, client_request_id) WHERE client_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bets_user_day ON bets(user_id, betting_day);
CREATE INDEX IF NOT EXISTS idx_bets_opportunity ON bets(decision_opportunity_id);

-- 2. bet_events -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bet_events (
  id            BIGSERIAL PRIMARY KEY,
  bet_id        INTEGER NOT NULL REFERENCES bets(id) ON DELETE RESTRICT,
  user_id       INTEGER,
  event_type    TEXT NOT NULL,           -- recorded / settled_manual / settled_auto / voided / superseded / correction_recorded / context_filled
  actor         TEXT NOT NULL,           -- user / pipeline
  payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bet_events_bet ON bet_events(bet_id, id);

-- 4. bankroll_ledger --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bankroll_ledger (
  id                  BIGSERIAL PRIMARY KEY,
  account_id          BIGINT NOT NULL REFERENCES bankroll_accounts(id) ON DELETE RESTRICT,
  entry_type          TEXT NOT NULL,
  amount              DOUBLE PRECISION,      -- initial_funding / deposit / withdrawal：正數（方向由 entry_type 決定）；
                                             -- adjustment：有號、≠ 0；bet_settlement / bet_settlement_reversal：有號損益（pipeline）；
                                             -- reversal：NULL（= 被沖銷那筆的相反數，由 Python 計算）
  reverses_entry_id   BIGINT REFERENCES bankroll_ledger(id) ON DELETE RESTRICT,
  bet_id              INTEGER REFERENCES bets(id) ON DELETE RESTRICT,
  settlement_key      TEXT,
  reason              TEXT,
  recorded_by         TEXT NOT NULL,         -- user / pipeline
  client_request_id   TEXT,
  recorded_at         TIMESTAMPTZ NOT NULL,
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
CREATE OR REPLACE FUNCTION d5_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% rows are immutable (D.5 audit trail)', TG_TABLE_NAME;
END $$;

CREATE OR REPLACE FUNCTION bets_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'bets rows are never deleted (bet %); use void / correction', OLD.id;
  END IF;
  -- 執行欄位：一旦記錄就不可靜默修改（更正 = 新列 supersedes 舊列）
  IF ROW(NEW.user_id, NEW.game_id, NEW.market, NEW.selection, NEW.line, NEW.odds, NEW.stake, NEW.placed_at,
         NEW.account_id, NEW.betting_day, NEW.source, NEW.bookmaker, NEW.market_type, NEW.period, NEW.outcome_set,
         NEW.model_target, NEW.model_threshold, NEW.comparator, NEW.settlement_rule, NEW.origin, NEW.strategy_compliance,
         NEW.reference_odds_snapshot_id, NEW.reference_decimal_odds, NEW.reference_pricing_snapshot_id,
         NEW.reference_sizing_snapshot_id, NEW.decision_snapshot_id, NEW.decision_opportunity_id, NEW.paper_decision_id,
         NEW.reference_context, NEW.risk_check, NEW.override_reason, NEW.override_confirmed_at, NEW.client_request_id,
         NEW.recorded_at, NEW.supersedes_bet_id)
     IS DISTINCT FROM
     ROW(OLD.user_id, OLD.game_id, OLD.market, OLD.selection, OLD.line, OLD.odds, OLD.stake, OLD.placed_at,
         OLD.account_id, OLD.betting_day, OLD.source, OLD.bookmaker, OLD.market_type, OLD.period, OLD.outcome_set,
         OLD.model_target, OLD.model_threshold, OLD.comparator, OLD.settlement_rule, OLD.origin, OLD.strategy_compliance,
         OLD.reference_odds_snapshot_id, OLD.reference_decimal_odds, OLD.reference_pricing_snapshot_id,
         OLD.reference_sizing_snapshot_id, OLD.decision_snapshot_id, OLD.decision_opportunity_id, OLD.paper_decision_id,
         OLD.reference_context, OLD.risk_check, OLD.override_reason, OLD.override_confirmed_at, OLD.client_request_id,
         OLD.recorded_at, OLD.supersedes_bet_id) THEN
    RAISE EXCEPTION 'bet % execution fields are immutable; record a correction instead', OLD.id;
  END IF;
  -- 只能補一次（NULL → 值），不能改
  IF OLD.bankroll_day_snapshot_id IS NOT NULL AND NEW.bankroll_day_snapshot_id IS DISTINCT FROM OLD.bankroll_day_snapshot_id
     OR OLD.stake_fraction_at_placement IS NOT NULL AND NEW.stake_fraction_at_placement IS DISTINCT FROM OLD.stake_fraction_at_placement
     OR OLD.superseded_by_bet_id IS NOT NULL AND NEW.superseded_by_bet_id IS DISTINCT FROM OLD.superseded_by_bet_id THEN
    RAISE EXCEPTION 'bet % bankroll basis / supersession can only be filled once', OLD.id;
  END IF;
  -- record_status：active → voided | superseded；之後不可再變
  IF NEW.record_status IS DISTINCT FROM OLD.record_status AND OLD.record_status <> 'active' THEN
    RAISE EXCEPTION 'bet % is % and cannot change record_status', OLD.id, OLD.record_status;
  END IF;
  IF OLD.record_status <> 'active' AND ROW(NEW.result, NEW.payout, NEW.settled_at, NEW.settlement_source, NEW.voided_at,
       NEW.void_reason) IS DISTINCT FROM ROW(OLD.result, OLD.payout, OLD.settled_at, OLD.settlement_source, OLD.voided_at,
       OLD.void_reason) THEN
    RAISE EXCEPTION 'bet % is % ; settlement / void fields are frozen', OLD.id, OLD.record_status;
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION bankroll_account_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'bankroll_accounts rows are never deleted';
  END IF;
  IF ROW(NEW.user_id, NEW.currency, NEW.created_at) IS DISTINCT FROM ROW(OLD.user_id, OLD.currency, OLD.created_at) THEN
    RAISE EXCEPTION 'bankroll account % identity is immutable', OLD.id;
  END IF;
  IF NEW.risk_state_version < OLD.risk_state_version THEN
    RAISE EXCEPTION 'risk_state_version only increases';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION decision_snapshot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'decision_snapshots rows are immutable';
  END IF;
  IF ROW(NEW.user_id, NEW.account_id, NEW.betting_day, NEW.as_of, NEW.decision_version, NEW.risk_policy_version,
         NEW.execution_policy_version, NEW.scope, NEW.input_fingerprint, NEW.risk_state_version, NEW.ledger_watermark,
         NEW.bet_event_watermark, NEW.bankroll_day_snapshot_id, NEW.status, NEW.summary, NEW.bankroll, NEW.actual_exposure, NEW.risk_limits,
         NEW.games, NEW.evidence, NEW.actual_performance, NEW.warnings)
     IS DISTINCT FROM
     ROW(OLD.user_id, OLD.account_id, OLD.betting_day, OLD.as_of, OLD.decision_version, OLD.risk_policy_version,
         OLD.execution_policy_version, OLD.scope, OLD.input_fingerprint, OLD.risk_state_version, OLD.ledger_watermark,
         OLD.bet_event_watermark, OLD.bankroll_day_snapshot_id, OLD.status, OLD.summary, OLD.bankroll, OLD.actual_exposure, OLD.risk_limits,
         OLD.games, OLD.evidence, OLD.actual_performance, OLD.warnings) THEN
    RAISE EXCEPTION 'decision snapshot % is immutable (only last_confirmed_at may advance)', OLD.id;
  END IF;
  IF NEW.last_confirmed_at < OLD.last_confirmed_at THEN
    RAISE EXCEPTION 'last_confirmed_at only advances';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_bets_guard ON bets;
CREATE TRIGGER trg_bets_guard BEFORE UPDATE OR DELETE ON bets FOR EACH ROW EXECUTE FUNCTION bets_guard();
DROP TRIGGER IF EXISTS trg_bet_events_immutable ON bet_events;
CREATE TRIGGER trg_bet_events_immutable BEFORE UPDATE OR DELETE ON bet_events FOR EACH ROW EXECUTE FUNCTION d5_immutable();
DROP TRIGGER IF EXISTS trg_bankroll_account_guard ON bankroll_accounts;
CREATE TRIGGER trg_bankroll_account_guard BEFORE UPDATE OR DELETE ON bankroll_accounts
  FOR EACH ROW EXECUTE FUNCTION bankroll_account_guard();
DROP TRIGGER IF EXISTS trg_risk_claims_immutable ON risk_state_claims;
CREATE TRIGGER trg_risk_claims_immutable BEFORE UPDATE OR DELETE ON risk_state_claims
  FOR EACH ROW EXECUTE FUNCTION d5_immutable();
DROP TRIGGER IF EXISTS trg_bankroll_ledger_immutable ON bankroll_ledger;
CREATE TRIGGER trg_bankroll_ledger_immutable BEFORE UPDATE OR DELETE ON bankroll_ledger
  FOR EACH ROW EXECUTE FUNCTION d5_immutable();
DROP TRIGGER IF EXISTS trg_bankroll_day_immutable ON bankroll_day_snapshots;
CREATE TRIGGER trg_bankroll_day_immutable BEFORE UPDATE OR DELETE ON bankroll_day_snapshots
  FOR EACH ROW EXECUTE FUNCTION d5_immutable();
DROP TRIGGER IF EXISTS trg_decision_snapshot_guard ON decision_snapshots;
CREATE TRIGGER trg_decision_snapshot_guard BEFORE UPDATE OR DELETE ON decision_snapshots
  FOR EACH ROW EXECUTE FUNCTION decision_snapshot_guard();
DROP TRIGGER IF EXISTS trg_decision_opp_immutable ON decision_opportunities;
CREATE TRIGGER trg_decision_opp_immutable BEFORE UPDATE OR DELETE ON decision_opportunities
  FOR EACH ROW EXECUTE FUNCTION d5_immutable();
