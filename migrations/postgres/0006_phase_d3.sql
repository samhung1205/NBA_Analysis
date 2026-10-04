-- ============================================================
-- Phase D.3 — Bet qualification, risk controls & Kelly sizing (PostgreSQL)
-- 純新增一張表；不修改 market_pricing_snapshots（D.2 歷史列不動）、不寫入 bets。
--
-- 為什麼要存：Kelly / exposure scaling 只在 Python（pipeline/core/sizing/）；Node API（Cloudflare Workers）
--   不得用 TypeScript 再寫 Kelly / stake cap / portfolio scaling → Python 計算、寫入；Node 只讀。
--
-- 一列 = 一個 D.2 定價 outcome（market_pricing_snapshot_id）× risk_policy_version × sizing_version × portfolio_key。
--   portfolio_key：同一 betting day（Asia/Taipei 開賽日期）組合成員 + 各自 exposure 前狀態 + policy 的雜湊。
--   game / day exposure cap 依同日其他機會而定 → 組合改變（新盤口、新預測、報價變舊）才會產生新列；
--   同一定價輸入 × 同一 policy × 同一組合 → 重跑冪等（ON CONFLICT DO NOTHING，已存在的列永不改寫）。
--   risk policy 改版（risk-v2…）→ 新列，risk-v1 舊列保留。
-- 所有 *_fraction 都是 bankroll 比例（0.0125 = 1.25%）；不保存任何 bankroll 金額。
-- ============================================================

CREATE TABLE IF NOT EXISTS bet_sizing_snapshots (
  id                          BIGSERIAL PRIMARY KEY,
  sizing_version              TEXT NOT NULL,                -- sizing-v1（qualification / portfolio 演算法）
  risk_policy_version         TEXT NOT NULL,                -- risk-v1（凍結參數，見 docs/phase-d3-report.md）
  kelly_math_version          TEXT NOT NULL,                -- kelly-push-v1
  portfolio_key               TEXT NOT NULL,
  market_pricing_snapshot_id  BIGINT NOT NULL REFERENCES market_pricing_snapshots(id) ON DELETE CASCADE,
  odds_snapshot_id            INTEGER REFERENCES odds_snapshots(id) ON DELETE CASCADE,
  prediction_id               INTEGER REFERENCES predictions(id) ON DELETE CASCADE,
  pricing_version             TEXT,
  game_id                     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  betting_day                 DATE NOT NULL,                -- 開賽時間的 Asia/Taipei 日曆日
  analysis_as_of              TIMESTAMPTZ NOT NULL,         -- sizing 時點 T（所有輸入 ≤ T）
  pricing_analysis_as_of      TIMESTAMPTZ NOT NULL,         -- D.2 定價最早可成立的時點
  source                      TEXT,
  bookmaker                   TEXT,
  market                      TEXT,
  market_type                 TEXT,
  period                      TEXT,
  outcome_set                 TEXT,
  line                        DOUBLE PRECISION,
  side                        TEXT NOT NULL,
  display_line                DOUBLE PRECISION,
  settlement_rule             TEXT,
  decimal_odds                DOUBLE PRECISION,             -- 實際提供的賠率（Kelly 輸入）
  p_win                       DOUBLE PRECISION,             -- = market_pricing_snapshots.model_prob
  p_push                      DOUBLE PRECISION,
  p_loss                      DOUBLE PRECISION,
  ev_per_unit                 DOUBLE PRECISION,
  edge_vs_fair                DOUBLE PRECISION,             -- 診斷用（市場分歧）；不是 Kelly 輸入
  full_kelly_fraction         DOUBLE PRECISION,             -- EV / (b·(1 − p_push))；EV ≤ 0 → 0；hard reject → NULL
  kelly_multiplier            DOUBLE PRECISION NOT NULL,    -- 0.25
  fractional_kelly_fraction   DOUBLE PRECISION,
  max_bet_fraction            DOUBLE PRECISION NOT NULL,    -- 0.02
  single_bet_capped_fraction  DOUBLE PRECISION,
  max_game_fraction           DOUBLE PRECISION NOT NULL,    -- 0.03
  game_exposure_before        DOUBLE PRECISION NOT NULL,
  game_scale_factor           DOUBLE PRECISION NOT NULL,
  game_exposure_after         DOUBLE PRECISION NOT NULL,
  game_adjusted_fraction      DOUBLE PRECISION NOT NULL,
  max_day_fraction            DOUBLE PRECISION NOT NULL,    -- 0.08
  daily_exposure_before       DOUBLE PRECISION NOT NULL,
  daily_scale_factor          DOUBLE PRECISION NOT NULL,
  daily_exposure_after        DOUBLE PRECISION NOT NULL,
  final_stake_fraction        DOUBLE PRECISION NOT NULL,    -- canonical output（bankroll 比例）
  qualification_status        TEXT NOT NULL,                -- eligible / no_positive_ev / unsupported_settlement / market_not_open /
                                                            -- no_prediction / invalid_probability / stale_quote /
                                                            -- mutually_exclusive_positive_kelly / exposure_scaled / unavailable
  mathematically_eligible     BOOLEAN NOT NULL,
  actionable                  BOOLEAN NOT NULL,             -- 不是推薦（推薦屬 D.5）
  reasons                     JSONB NOT NULL DEFAULT '[]'::jsonb,
  warnings                    JSONB NOT NULL DEFAULT '[]'::jsonb,
  odds_fetched_at             TIMESTAMPTZ,
  odds_last_seen_at           TIMESTAMPTZ,
  quote_age_seconds           DOUBLE PRECISION,             -- T − fetched_at（此價格首次觀測至今）
  last_seen_age_seconds       DOUBLE PRECISION,             -- T − 最後一次輪詢確認
  max_quote_age_seconds       DOUBLE PRECISION,             -- risk-v1：2 × 來源輪詢間隔
  computed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()   -- 稽核用（唯一非決定性欄位）
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_bet_sizing_input
  ON bet_sizing_snapshots(market_pricing_snapshot_id, risk_policy_version, sizing_version, portfolio_key);
CREATE INDEX IF NOT EXISTS idx_bet_sizing_day ON bet_sizing_snapshots(betting_day, risk_policy_version, analysis_as_of DESC);
CREATE INDEX IF NOT EXISTS idx_bet_sizing_game ON bet_sizing_snapshots(game_id, analysis_as_of DESC);
