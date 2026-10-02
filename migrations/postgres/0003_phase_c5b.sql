-- ============================================================
-- Phase C.5B — Historical Data Completeness & Feature Foundation (PostgreSQL)
-- 純新增：不改動既有欄位語意，前端 / API contract 不受影響。
--   1. team_game_stats：補基本 box score 的投籃/罰球計數（算 eFG / TS / 控球數的原料）
--   2. team_game_derived：由基本 box score 可重現計算的進階指標（與官方 pace/off_rtg 分開存放，
--      官方欄位語意不變；以 formula_version 標記公式版本，可整批重算）
--   3. injury_reports / injury_report_entries：官方傷病報告「完整快照」（as-of 還原的權威來源）
--      舊的 injuries 表維持「API 讀取的最新狀態」用途，不動 schema 語意。
--   4. source_fetch_log：回填的來源抓取紀錄（可續傳、可定位缺漏；missing 也記錄，避免重複探測）
--   5. injuries：唯一性約束（冪等 + 併發保護）與 API 查詢用索引
-- ============================================================

-- 1. team_game_stats raw counts ---------------------------------
ALTER TABLE team_game_stats
  ADD COLUMN IF NOT EXISTS fgm INTEGER,
  ADD COLUMN IF NOT EXISTS fga INTEGER,
  ADD COLUMN IF NOT EXISTS fg3m INTEGER,
  ADD COLUMN IF NOT EXISTS fg3a INTEGER,
  ADD COLUMN IF NOT EXISTS ftm INTEGER,
  ADD COLUMN IF NOT EXISTS fta INTEGER,
  ADD COLUMN IF NOT EXISTS team_min DOUBLE PRECISION;   -- 球隊總上場分鐘（240 + 25×OT 節數）

-- 2. derived advanced metrics -----------------------------------
CREATE TABLE IF NOT EXISTS team_game_derived (
  game_id         INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  team_id         INTEGER NOT NULL REFERENCES teams(id),
  formula_version TEXT NOT NULL,
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  poss            DOUBLE PRECISION,   -- 估計控球數（雙方平均，同場兩隊相同）
  pace            DOUBLE PRECISION,   -- 每 48 分鐘控球數
  ortg            DOUBLE PRECISION,   -- 100 × PTS / poss
  drtg            DOUBLE PRECISION,   -- 100 × OppPTS / poss
  net_rtg         DOUBLE PRECISION,
  efg_pct         DOUBLE PRECISION,   -- 比率 0~1
  ts_pct          DOUBLE PRECISION,
  tov_pct         DOUBLE PRECISION,   -- TOV / (FGA + 0.44 FTA + TOV)
  orb_pct         DOUBLE PRECISION,   -- OREB / (OREB + 對手 DREB)
  drb_pct         DOUBLE PRECISION,
  ftr             DOUBLE PRECISION,   -- FTA / FGA
  fg3a_rate       DOUBLE PRECISION,   -- 3PA / FGA
  ast_pct         DOUBLE PRECISION,   -- AST / FGM
  PRIMARY KEY (game_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_tgd_team ON team_game_derived(team_id);

-- 3. 官方傷病報告完整快照 ----------------------------------------
CREATE TABLE IF NOT EXISTS injury_reports (
  id              SERIAL PRIMARY KEY,
  source          TEXT NOT NULL DEFAULT 'nba_official',
  report_time_utc TIMESTAMPTZ NOT NULL,          -- 報告檔名時間（美東）換算成 UTC
  ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  n_rows          INTEGER NOT NULL DEFAULT 0,    -- 報告原始列數（含 NOT YET SUBMITTED）
  n_entries       INTEGER NOT NULL DEFAULT 0,    -- 有狀態的球員列數
  n_unmatched     INTEGER NOT NULL DEFAULT 0,    -- 無法對應到 players 的列數
  -- {"2024-02-15": {"MIL": {"n": 3, "nys": false, "implied": false}, ...}, ...}
  -- 每個「賽事日期（ET）× 球隊」是否被這份報告涵蓋：n=列出的球員數；nys=NOT YET SUBMITTED；
  -- implied=比賽在報告中但該隊沒有任何一列（推定已申報、無人列入）
  coverage        JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (source, report_time_utc)
);
CREATE INDEX IF NOT EXISTS idx_inj_reports_time ON injury_reports(report_time_utc);

CREATE TABLE IF NOT EXISTS injury_report_entries (
  id          BIGSERIAL PRIMARY KEY,
  report_id   INTEGER NOT NULL REFERENCES injury_reports(id) ON DELETE CASCADE,
  game_date   DATE NOT NULL,                     -- 報告列出的賽事日期（美東日曆日）
  team_id     INTEGER REFERENCES teams(id),
  player_id   INTEGER REFERENCES players(id),    -- NULL = 姓名無法對應（保留原始姓名供稽核）
  player_name TEXT NOT NULL,                     -- 報告原文 'Last, First'
  status      TEXT NOT NULL,                     -- Out / Doubtful / Questionable / Probable / Available
  reason      TEXT,
  UNIQUE (report_id, game_date, team_id, player_name)
);
-- 不另建 report_id / player_id 索引：UNIQUE(report_id, …) 的前導欄位已涵蓋 report_id 查詢，
-- 球員查詢一律載入記憶體索引（injury_asof.load_index）；90 萬列下每個多餘索引約 10 MB（免費方案 500 MB）。

-- 4. 來源抓取紀錄 -------------------------------------------------
CREATE TABLE IF NOT EXISTS source_fetch_log (
  kind        TEXT NOT NULL,                     -- 'box_basic' / 'injury_report'
  key         TEXT NOT NULL,                     -- nba_game_id 或 'YYYY-MM-DDTHH:MM ET'
  status      TEXT NOT NULL,                     -- ok / missing / error / not_final
  attempts    INTEGER NOT NULL DEFAULT 1,
  last_error  TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (kind, key)
);

-- 5. injuries：冪等 + 併發保護 ------------------------------------
-- 同一份報告（source + report_time）對同一球員最多一列；重複/並行寫入以 ON CONFLICT DO NOTHING 吸收。
-- 既有資料若有重複，保留 id 最小的一列。
DELETE FROM injuries a USING injuries b
 WHERE a.id > b.id AND a.source = b.source AND a.report_time_utc = b.report_time_utc
   AND a.player_id IS NOT DISTINCT FROM b.player_id;
CREATE UNIQUE INDEX IF NOT EXISTS uq_injuries_report_player
  ON injuries(source, report_time_utc, player_id);
-- API「每位球員最新一筆」的子查詢用
CREATE INDEX IF NOT EXISTS idx_inj_player_time ON injuries(player_id, report_time_utc DESC);
