-- ============================================================
-- NBA 預測平台 — 初始 Schema (SQLite / Cloudflare D1 方言)
-- 對應 migrations/postgres/0001_init.sql，欄位名稱與語意完全一致
-- 階段一開發期使用；正式環境請使用 Postgres 版本
-- ============================================================

CREATE TABLE IF NOT EXISTS teams (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  nba_team_id   INTEGER UNIQUE,
  abbr          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  name_zh       TEXT,
  conference    TEXT,
  division      TEXT,
  logo_url      TEXT,
  created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS players (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  nba_player_id INTEGER UNIQUE,
  name          TEXT NOT NULL,
  team_id       INTEGER REFERENCES teams(id),
  position      TEXT,
  status        TEXT,              -- active / inactive / two_way ...
  is_starter    INTEGER DEFAULT 0, -- 0/1，主力缺陣警示用
  created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);

CREATE TABLE IF NOT EXISTS games (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  nba_game_id   TEXT UNIQUE,
  season        TEXT NOT NULL,          -- e.g. '2025-26'
  season_stage  TEXT DEFAULT 'regular', -- preseason / regular / playoffs
  date_utc      TEXT NOT NULL,          -- ISO8601 UTC，前端轉台灣時間顯示
  home_team_id  INTEGER NOT NULL REFERENCES teams(id),
  away_team_id  INTEGER NOT NULL REFERENCES teams(id),
  arena         TEXT,
  status        TEXT NOT NULL DEFAULT 'scheduled', -- scheduled / live / final / postponed
  period        INTEGER,                -- live 時目前節數
  home_pts      INTEGER,
  away_pts      INTEGER,
  home_q1 INTEGER, home_q2 INTEGER, home_q3 INTEGER, home_q4 INTEGER, home_ot INTEGER,
  away_q1 INTEGER, away_q2 INTEGER, away_q3 INTEGER, away_q4 INTEGER, away_ot INTEGER,
  home_h1 INTEGER, home_h2 INTEGER, away_h1 INTEGER, away_h2 INTEGER,
  updated_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(date_utc);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
CREATE INDEX IF NOT EXISTS idx_games_teams ON games(home_team_id, away_team_id);

CREATE TABLE IF NOT EXISTS team_game_stats (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id   INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  team_id   INTEGER NOT NULL REFERENCES teams(id),
  is_home   INTEGER DEFAULT 0,
  pts       INTEGER,
  fg_pct    REAL, fg3_pct REAL, ft_pct REAL,
  reb       INTEGER, oreb INTEGER, dreb INTEGER,
  ast       INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
  pace      REAL, off_rtg REAL, def_rtg REAL, net_rtg REAL,
  ts_pct    REAL, efg_pct REAL, ast_ratio REAL, tov_ratio REAL,
  UNIQUE (game_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_tgs_team ON team_game_stats(team_id);

CREATE TABLE IF NOT EXISTS player_game_stats (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  player_id   INTEGER NOT NULL REFERENCES players(id),
  team_id     INTEGER REFERENCES teams(id),
  min         REAL,
  pts INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER,
  fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
  plus_minus  REAL,
  started     INTEGER DEFAULT 0,
  UNIQUE (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_pgs_player ON player_game_stats(player_id);

CREATE TABLE IF NOT EXISTS injuries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  report_time_utc TEXT NOT NULL,
  player_id       INTEGER REFERENCES players(id),
  team_id         INTEGER REFERENCES teams(id),
  game_id         INTEGER REFERENCES games(id),
  status          TEXT NOT NULL,   -- Out / Doubtful / Questionable / Probable / Available
  reason          TEXT,
  source          TEXT DEFAULT 'nba_official',
  created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_inj_time ON injuries(report_time_utc);
CREATE INDEX IF NOT EXISTS idx_inj_team ON injuries(team_id);
CREATE INDEX IF NOT EXISTS idx_inj_game ON injuries(game_id);

CREATE TABLE IF NOT EXISTS odds_snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fetched_at  TEXT NOT NULL,
  game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,   -- twsport / oddsapi / pinnacle ...
  market      TEXT NOT NULL,   -- ml / spread / total / h1_ml / h1_spread / h1_total
  line        REAL,            -- 讓分或大小分的盤中線
  home_odds   REAL,
  away_odds   REAL,
  over_odds   REAL,
  under_odds  REAL,
  raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_odds_game ON odds_snapshots(game_id, market, fetched_at);
CREATE INDEX IF NOT EXISTS idx_odds_src ON odds_snapshots(source);

CREATE TABLE IF NOT EXISTS predictions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  model_version  TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  home_win_prob  REAL,
  pred_margin    REAL,   -- 主隊淨勝分（正 = 主隊贏）
  pred_total     REAL,
  pred_home_h1   REAL, pred_away_h1 REAL,
  pred_home_h2   REAL, pred_away_h2 REAL,
  pred_q1_margin REAL,
  confidence     REAL,   -- 0~1
  features_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_game ON predictions(game_id, created_at);

CREATE TABLE IF NOT EXISTS bets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER REFERENCES users(id),
  game_id    INTEGER NOT NULL REFERENCES games(id),
  market     TEXT NOT NULL,   -- ml / spread / total / h1_*
  selection  TEXT NOT NULL,   -- home / away / over / under
  line       REAL,
  odds       REAL NOT NULL,   -- 台彩實際賠率
  stake      REAL NOT NULL,
  result     TEXT DEFAULT 'pending', -- pending / win / lose / push / void
  payout     REAL,
  note       TEXT,
  placed_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id, placed_at);
CREATE INDEX IF NOT EXISTS idx_bets_game ON bets(game_id);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,   -- pbkdf2$<iter>$<salt_b64>$<hash_b64>
  display_name  TEXT,
  created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- 資料來源新鮮度／健康狀態（階段二排程器寫入，系統狀態頁讀取）
CREATE TABLE IF NOT EXISTS data_sources (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key      TEXT NOT NULL UNIQUE,  -- nba_api / nbainjuries / espn / twsport / oddsapi
  display_name    TEXT NOT NULL,
  category        TEXT,                  -- stats / injury / odds / model
  last_success_at TEXT,
  last_attempt_at TEXT,
  last_status     TEXT DEFAULT 'unknown',-- ok / warn / error / unknown
  last_error      TEXT,
  expected_interval_min INTEGER,         -- 預期更新間隔，前端判斷是否過期
  records_updated INTEGER
);

-- 模型回測績效（階段二寫入，績效頁讀取）
CREATE TABLE IF NOT EXISTS model_metrics (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  model_version TEXT NOT NULL,
  season        TEXT,
  evaluated_at  TEXT NOT NULL,
  n_games       INTEGER,
  accuracy      REAL,   -- 勝負命中率
  ats_accuracy  REAL,   -- 讓分命中率
  ou_accuracy   REAL,   -- 大小分命中率
  h1_accuracy   REAL,   -- 上半場命中率
  log_loss      REAL,
  brier         REAL,
  mae_margin    REAL,
  mae_total     REAL,
  sim_roi       REAL,   -- 以台彩實際賠率模擬之 ROI
  notes         TEXT,
  UNIQUE (model_version, season, evaluated_at)
);
