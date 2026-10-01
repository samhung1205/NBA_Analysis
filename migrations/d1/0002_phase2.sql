-- ============================================================
-- NBA 預測平台 — Phase 2 追加 Schema (SQLite / Cloudflare D1 方言)
-- 對應 migrations/postgres/0002_phase2.sql，欄位名稱與語意完全一致
-- ============================================================

CREATE TABLE IF NOT EXISTS elo_ratings (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id           INTEGER NOT NULL REFERENCES teams(id),
  game_id           INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  season            TEXT NOT NULL,
  game_date_utc     TEXT NOT NULL,
  opponent_team_id  INTEGER REFERENCES teams(id),
  is_home           INTEGER DEFAULT 0,
  rating_before     REAL NOT NULL,
  rating_after      REAL NOT NULL,
  model_version     TEXT NOT NULL,
  computed_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (team_id, game_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_elo_team_date ON elo_ratings(team_id, game_date_utc);
CREATE INDEX IF NOT EXISTS idx_elo_game ON elo_ratings(game_id);
CREATE INDEX IF NOT EXISTS idx_elo_model ON elo_ratings(model_version);
