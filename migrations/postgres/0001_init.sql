-- ============================================================
-- NBA 預測平台 — 初始 Schema (PostgreSQL 方言 / Supabase・Railway)
-- 這是「正式環境」的權威 schema：階段一的 Cloudflare Pages
-- 與階段二的 Python 服務都連同一個 Postgres 實例。
-- 對應 migrations/d1/0001_init.sql（開發期 SQLite 版），欄位語意一致。
-- ============================================================

CREATE TABLE IF NOT EXISTS teams (
  id            SERIAL PRIMARY KEY,
  nba_team_id   INTEGER UNIQUE,
  abbr          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  name_zh       TEXT,
  conference    TEXT,
  division      TEXT,
  logo_url      TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS players (
  id            SERIAL PRIMARY KEY,
  nba_player_id INTEGER UNIQUE,
  name          TEXT NOT NULL,
  team_id       INTEGER REFERENCES teams(id),
  position      TEXT,
  status        TEXT,
  is_starter    SMALLINT DEFAULT 0,   -- 0/1（與 SQLite 方言一致，讓同一份 SQL 可跨方言執行）
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);

CREATE TABLE IF NOT EXISTS games (
  id            SERIAL PRIMARY KEY,
  nba_game_id   TEXT UNIQUE,
  season        TEXT NOT NULL,
  season_stage  TEXT DEFAULT 'regular',
  date_utc      TIMESTAMPTZ NOT NULL,
  home_team_id  INTEGER NOT NULL REFERENCES teams(id),
  away_team_id  INTEGER NOT NULL REFERENCES teams(id),
  arena         TEXT,
  status        TEXT NOT NULL DEFAULT 'scheduled',
  period        INTEGER,
  home_pts      INTEGER,
  away_pts      INTEGER,
  home_q1 INTEGER, home_q2 INTEGER, home_q3 INTEGER, home_q4 INTEGER, home_ot INTEGER,
  away_q1 INTEGER, away_q2 INTEGER, away_q3 INTEGER, away_q4 INTEGER, away_ot INTEGER,
  home_h1 INTEGER, home_h2 INTEGER, away_h1 INTEGER, away_h2 INTEGER,
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(date_utc);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status);
CREATE INDEX IF NOT EXISTS idx_games_teams ON games(home_team_id, away_team_id);

CREATE TABLE IF NOT EXISTS team_game_stats (
  id        SERIAL PRIMARY KEY,
  game_id   INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  team_id   INTEGER NOT NULL REFERENCES teams(id),
  is_home   SMALLINT DEFAULT 0,
  pts       INTEGER,
  fg_pct    DOUBLE PRECISION, fg3_pct DOUBLE PRECISION, ft_pct DOUBLE PRECISION,
  reb       INTEGER, oreb INTEGER, dreb INTEGER,
  ast       INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
  pace      DOUBLE PRECISION, off_rtg DOUBLE PRECISION, def_rtg DOUBLE PRECISION,
  net_rtg   DOUBLE PRECISION,
  ts_pct    DOUBLE PRECISION, efg_pct DOUBLE PRECISION,
  ast_ratio DOUBLE PRECISION, tov_ratio DOUBLE PRECISION,
  UNIQUE (game_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_tgs_team ON team_game_stats(team_id);

CREATE TABLE IF NOT EXISTS player_game_stats (
  id          SERIAL PRIMARY KEY,
  game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  player_id   INTEGER NOT NULL REFERENCES players(id),
  team_id     INTEGER REFERENCES teams(id),
  min         DOUBLE PRECISION,
  pts INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER,
  fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
  plus_minus  DOUBLE PRECISION,
  started     SMALLINT DEFAULT 0,
  UNIQUE (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_pgs_player ON player_game_stats(player_id);

CREATE TABLE IF NOT EXISTS injuries (
  id              SERIAL PRIMARY KEY,
  report_time_utc TIMESTAMPTZ NOT NULL,
  player_id       INTEGER REFERENCES players(id),
  team_id         INTEGER REFERENCES teams(id),
  game_id         INTEGER REFERENCES games(id),
  status          TEXT NOT NULL,
  reason          TEXT,
  source          TEXT DEFAULT 'nba_official',
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_inj_time ON injuries(report_time_utc);
CREATE INDEX IF NOT EXISTS idx_inj_team ON injuries(team_id);
CREATE INDEX IF NOT EXISTS idx_inj_game ON injuries(game_id);

CREATE TABLE IF NOT EXISTS odds_snapshots (
  id          SERIAL PRIMARY KEY,
  fetched_at  TIMESTAMPTZ NOT NULL,
  game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,
  market      TEXT NOT NULL,
  line        DOUBLE PRECISION,
  home_odds   DOUBLE PRECISION,
  away_odds   DOUBLE PRECISION,
  over_odds   DOUBLE PRECISION,
  under_odds  DOUBLE PRECISION,
  raw_json    JSONB
);
CREATE INDEX IF NOT EXISTS idx_odds_game ON odds_snapshots(game_id, market, fetched_at);
CREATE INDEX IF NOT EXISTS idx_odds_src ON odds_snapshots(source);

CREATE TABLE IF NOT EXISTS predictions (
  id             SERIAL PRIMARY KEY,
  game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  model_version  TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL,
  home_win_prob  DOUBLE PRECISION,
  pred_margin    DOUBLE PRECISION,
  pred_total     DOUBLE PRECISION,
  pred_home_h1   DOUBLE PRECISION, pred_away_h1 DOUBLE PRECISION,
  pred_home_h2   DOUBLE PRECISION, pred_away_h2 DOUBLE PRECISION,
  pred_q1_margin DOUBLE PRECISION,
  confidence     DOUBLE PRECISION,
  features_json  JSONB
);
CREATE INDEX IF NOT EXISTS idx_pred_game ON predictions(game_id, created_at);

CREATE TABLE IF NOT EXISTS users (
  id            SERIAL PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bets (
  id         SERIAL PRIMARY KEY,
  user_id    INTEGER REFERENCES users(id),
  game_id    INTEGER NOT NULL REFERENCES games(id),
  market     TEXT NOT NULL,
  selection  TEXT NOT NULL,
  line       DOUBLE PRECISION,
  odds       DOUBLE PRECISION NOT NULL,
  stake      DOUBLE PRECISION NOT NULL,
  result     TEXT DEFAULT 'pending',
  payout     DOUBLE PRECISION,
  note       TEXT,
  placed_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bets_user ON bets(user_id, placed_at);
CREATE INDEX IF NOT EXISTS idx_bets_game ON bets(game_id);

CREATE TABLE IF NOT EXISTS data_sources (
  id              SERIAL PRIMARY KEY,
  source_key      TEXT NOT NULL UNIQUE,
  display_name    TEXT NOT NULL,
  category        TEXT,
  last_success_at TIMESTAMPTZ,
  last_attempt_at TIMESTAMPTZ,
  last_status     TEXT DEFAULT 'unknown',
  last_error      TEXT,
  expected_interval_min INTEGER,
  records_updated INTEGER
);

CREATE TABLE IF NOT EXISTS model_metrics (
  id            SERIAL PRIMARY KEY,
  model_version TEXT NOT NULL,
  season        TEXT,
  evaluated_at  TIMESTAMPTZ NOT NULL,
  n_games       INTEGER,
  accuracy      DOUBLE PRECISION,
  ats_accuracy  DOUBLE PRECISION,
  ou_accuracy   DOUBLE PRECISION,
  h1_accuracy   DOUBLE PRECISION,
  log_loss      DOUBLE PRECISION,
  brier         DOUBLE PRECISION,
  mae_margin    DOUBLE PRECISION,
  mae_total     DOUBLE PRECISION,
  sim_roi       DOUBLE PRECISION,
  notes         TEXT,
  UNIQUE (model_version, season, evaluated_at)
);
