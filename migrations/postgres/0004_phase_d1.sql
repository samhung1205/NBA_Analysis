-- ============================================================
-- Phase D.1 — Odds Ingestion & Market Normalization (PostgreSQL)
-- 純新增：既有欄位語意不變，舊列（seed）全部欄位仍有效，API / 前端向下相容。
--
--   1. odds_snapshots：補「無損保存」所需欄位（bookmaker、period、canonical market、
--      model threshold、來源事件 id、來源更新時間、市場狀態、去重雜湊、最後確認時間）。
--      一列 = 某一刻 (game, source, bookmaker, market) 的完整兩/三向報價。
--      既有欄位語意不變：line = 讓分盤「主隊」顯示線（負 = 主隊讓分）/ 大小分線 / 獨贏 NULL。
--   2. odds_fetch_runs：每次抓取一列（狀態、延遲、額度、計數、診斷）。
--   3. odds_event_links：來源事件 → games.id 的明確對應層（含 ambiguous / unmatched 紀錄）。
--   4. data_sources：細分狀態 last_outcome（success / partial / parser_changed / quota_low …），
--      last_status 仍只用 ok / warn / error（系統狀態頁既有語意）。
--   5. v_odds_quotes：把每列攤成「每個 outcome 一列」的 canonical quote（不做任何正負號運算，
--      門檻已由 Python canonical 層寫入 model_threshold）。
-- ============================================================

-- 1. odds_snapshots ---------------------------------------------
ALTER TABLE odds_snapshots
  ADD COLUMN IF NOT EXISTS bookmaker          TEXT,              -- twsport：'twsport'；oddsapi：bookmaker key（draftkings…）
  ADD COLUMN IF NOT EXISTS market_type        TEXT,              -- moneyline / spread / total
  ADD COLUMN IF NOT EXISTS period             TEXT,              -- full_game / h1
  ADD COLUMN IF NOT EXISTS outcome_set        TEXT,              -- two_way / three_way（台彩上半場不讓分含和局）
  ADD COLUMN IF NOT EXISTS away_line          DOUBLE PRECISION,  -- 讓分盤「客隊」顯示線（line 為主隊顯示線）
  ADD COLUMN IF NOT EXISTS draw_odds          DOUBLE PRECISION,  -- three_way 的和局賠率
  ADD COLUMN IF NOT EXISTS model_target       TEXT,              -- margin / total / h1_margin / h1_total（C.5E target）
  ADD COLUMN IF NOT EXISTS model_threshold    DOUBLE PRECISION,  -- 主隊/大 ⇔ target > threshold；客隊/小 ⇔ target < threshold
  ADD COLUMN IF NOT EXISTS market_status      TEXT,              -- open / suspended / closed / unknown（NULL = 舊資料，視為 open）
  ADD COLUMN IF NOT EXISTS source_event_id    TEXT,
  ADD COLUMN IF NOT EXISTS source_market_id   TEXT,              -- 台彩 idfomarket / Odds API market key
  ADD COLUMN IF NOT EXISTS source_updated_at  TIMESTAMPTZ,       -- 來源宣稱的更新時間（與 fetched_at 分開）
  ADD COLUMN IF NOT EXISTS last_seen_at       TIMESTAMPTZ,       -- 內容相同的最後一次輪詢（只會往後移）
  ADD COLUMN IF NOT EXISTS content_hash       TEXT,              -- 去重雜湊（見 core/odds/store.py）
  ADD COLUMN IF NOT EXISTS fetch_run_id       BIGINT,
  ADD COLUMN IF NOT EXISTS normalizer_version TEXT;

-- 同一次抓取（同 fetched_at）重跑不會重複；舊 seed 列（content_hash NULL）不受約束
CREATE UNIQUE INDEX IF NOT EXISTS uq_odds_series_fetch
  ON odds_snapshots(game_id, source, bookmaker, market, fetched_at) WHERE content_hash IS NOT NULL;
-- 「某 series 最新一筆」查詢（寫入去重 + API latest）
CREATE INDEX IF NOT EXISTS idx_odds_series_latest
  ON odds_snapshots(game_id, source, bookmaker, market, fetched_at DESC);

-- 2. odds_fetch_runs ---------------------------------------------
CREATE TABLE IF NOT EXISTS odds_fetch_runs (
  id                  BIGSERIAL PRIMARY KEY,
  source              TEXT NOT NULL,                 -- twsport / oddsapi
  started_at          TIMESTAMPTZ NOT NULL,
  fetched_at          TIMESTAMPTZ,                   -- 資料取得時間（寫入 snapshot 的 fetched_at）
  finished_at         TIMESTAMPTZ,
  outcome             TEXT NOT NULL,                 -- success / partial / no_nba_markets / parser_changed / quota_low /
                                                     -- rate_limited / auth_failed / network_failure / blocked / not_configured / error
  n_requests          INTEGER,
  latency_ms          INTEGER,
  n_events            INTEGER,                       -- 來源上的 NBA 事件數
  n_matched           INTEGER,
  n_unmatched         INTEGER,
  n_ambiguous         INTEGER,
  n_rejected          INTEGER,
  n_markets           INTEGER,                       -- 解析成功的 market snapshot 數
  n_markets_invalid   INTEGER,
  n_snapshots_new     INTEGER,
  n_snapshots_unchanged INTEGER,
  n_snapshots_stale   INTEGER,
  quota_remaining     INTEGER,
  quota_used          INTEGER,
  quota_last          INTEGER,
  error               TEXT,
  diagnostics         JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_odds_runs_source_time ON odds_fetch_runs(source, started_at DESC);

-- 3. odds_event_links --------------------------------------------
CREATE TABLE IF NOT EXISTS odds_event_links (
  source            TEXT NOT NULL,
  source_event_id   TEXT NOT NULL,
  game_id           INTEGER REFERENCES games(id) ON DELETE SET NULL,   -- 只有 status='matched' 才非 NULL
  status            TEXT NOT NULL,      -- matched / ambiguous / unmatched / rejected
  method            TEXT,               -- link / teams_time / rescheduled / manual
  reason            TEXT,               -- unknown_team / no_candidate / home_away_reversed / game_postponed …
  home_name_raw     TEXT,
  away_name_raw     TEXT,
  commence_time_utc TIMESTAMPTZ,
  time_delta_min    DOUBLE PRECISION,
  source_stage      TEXT,               -- preseason / regular / cup / unknown（來源宣稱）
  candidates        JSONB NOT NULL DEFAULT '[]'::jsonb,
  manual            BOOLEAN NOT NULL DEFAULT FALSE,   -- 人工指定的對應不會被自動覆寫
  first_seen_at     TIMESTAMPTZ NOT NULL,
  last_seen_at      TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (source, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_odds_links_game ON odds_event_links(game_id);

-- 4. data_sources 細分狀態 ----------------------------------------
ALTER TABLE data_sources
  ADD COLUMN IF NOT EXISTS last_outcome TEXT,
  ADD COLUMN IF NOT EXISTS meta JSONB;

-- 5. canonical per-outcome quotes ---------------------------------
CREATE OR REPLACE VIEW v_odds_quotes AS
  SELECT o.id AS snapshot_id, o.game_id, o.source, o.bookmaker, o.market, o.market_type, o.period,
         s.side, s.price, s.display_line, o.model_target, o.model_threshold, s.comparator,
         o.market_status, o.fetched_at, o.last_seen_at, o.source_updated_at, o.source_event_id
    FROM odds_snapshots o
    CROSS JOIN LATERAL (VALUES
      ('home',  o.home_odds,  CASE WHEN o.market_type = 'spread' THEN o.line END,  'gt'),
      ('away',  o.away_odds,  CASE WHEN o.market_type = 'spread' THEN o.away_line END, 'lt'),
      ('draw',  o.draw_odds,  NULL::double precision,                              'eq'),
      ('over',  o.over_odds,  o.line,                                              'gt'),
      ('under', o.under_odds, o.line,                                              'lt')
    ) AS s(side, price, display_line, comparator)
   WHERE o.content_hash IS NOT NULL AND s.price IS NOT NULL;
