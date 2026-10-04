-- ============================================================
-- Phase D.2 — No-vig market pricing, model edge & EV (PostgreSQL)
-- 純新增一張表，既有表 / 欄位 / API 欄位語意不變。
--
-- 為什麼要存（而不是 API 即時計算）：
--   model_prob 需要 C.5E artifact（分佈狀態、artifact 版本、push 語意），只有 Python pipeline 能載入；
--   Cloudflare Workers（Node API）不能跑 Python，也**不得**用 TypeScript 再實作一份 normal CDF / 去水 / EV。
--   → 由 Python pricing engine（pipeline/core/pricing/）計算，Node API 只讀這張表。
--
-- 一列 = 一個 odds snapshot × 一筆 prediction × 一個 outcome × pricing_version。
--   同一組輸入（snapshot、prediction、不可變的 artifact 版本）→ 同樣的輸出：唯一索引保證重跑冪等。
--   analysis_as_of = max(odds.fetched_at, prediction 可得時間)：此分析最早可成立的時點（決定性，與何時重算無關）。
--   prediction_id 為 NULL = 當時沒有有效預測（market_only：只有 raw implied / 去水）。
--   這張表是 API 的計算結果快取 + 稽核紀錄；D.4 歷史重建以 odds_snapshots + predictions + artifact 用同一函式重算。
-- ============================================================

CREATE TABLE IF NOT EXISTS market_pricing_snapshots (
  id                     BIGSERIAL PRIMARY KEY,
  pricing_version        TEXT NOT NULL,                -- pricing-v1
  no_vig_method          TEXT NOT NULL,                -- proportional-v1
  odds_snapshot_id       INTEGER NOT NULL REFERENCES odds_snapshots(id) ON DELETE CASCADE,
  prediction_id          INTEGER REFERENCES predictions(id) ON DELETE CASCADE,
  game_id                INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  analysis_as_of         TIMESTAMPTZ NOT NULL,
  odds_fetched_at        TIMESTAMPTZ NOT NULL,
  prediction_created_at  TIMESTAMPTZ,
  prediction_kind        TEXT,                         -- early / final / refresh
  prediction_profile     TEXT,                         -- early / final
  model_version          TEXT,
  artifact_version       TEXT,
  distribution_version   TEXT,
  -- 市場（每個 outcome 列重複保存，便於查詢）
  source                 TEXT NOT NULL,
  bookmaker              TEXT,
  market                 TEXT NOT NULL,                -- ml / spread / total / h1_ml / h1_spread / h1_total
  market_type            TEXT,
  period                 TEXT,
  outcome_set            TEXT,
  line                   DOUBLE PRECISION,             -- 讓分：主隊顯示線；大小：總分線
  away_line              DOUBLE PRECISION,
  status                 TEXT NOT NULL,                -- priced / market_only / unsupported_settlement / unsupported_market / rejected
  status_reason          TEXT,
  settlement_rule        TEXT,                         -- moneyline_ot_included / half_line_no_push / integer_line_push_refund / three_way_draw_outcome / …
  total_raw_implied      DOUBLE PRECISION,             -- Σ 1/odds
  market_overround       DOUBLE PRECISION,             -- Σ 1/odds − 1
  fair_prob_sum          DOUBLE PRECISION,             -- Σ fair（= 1）
  -- outcome
  side                   TEXT NOT NULL,                -- home / away / draw / over / under
  display_line           DOUBLE PRECISION,
  model_target           TEXT,                         -- margin / total / h1_margin / h1_total
  model_threshold        DOUBLE PRECISION,
  comparator             TEXT,                         -- gt / lt / eq
  decimal_odds           DOUBLE PRECISION,
  raw_implied_prob       DOUBLE PRECISION,             -- 1 / decimal_odds
  fair_no_vig_prob       DOUBLE PRECISION,             -- raw / Σ raw
  model_prob             DOUBLE PRECISION,             -- C.5E：P(win)
  push_prob              DOUBLE PRECISION,             -- C.5E：P(push)（退還本金）
  loss_prob              DOUBLE PRECISION,
  edge_vs_fair           DOUBLE PRECISION,             -- model_prob − fair_no_vig_prob（唯一的 edge）
  ev_per_unit            DOUBLE PRECISION,             -- model_prob·(odds−1) − loss_prob（實際賠率）
  expected_return        DOUBLE PRECISION,             -- 1 + ev_per_unit
  ev_percent             DOUBLE PRECISION,             -- 100 × ev_per_unit
  warnings               JSONB NOT NULL DEFAULT '[]'::jsonb,
  diagnostics            JSONB NOT NULL DEFAULT '{}'::jsonb,
  computed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()   -- 稽核用（唯一非決定性欄位）
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_market_pricing_input
  ON market_pricing_snapshots(odds_snapshot_id, COALESCE(prediction_id, 0), side, pricing_version);
CREATE INDEX IF NOT EXISTS idx_market_pricing_game ON market_pricing_snapshots(game_id, analysis_as_of DESC);
