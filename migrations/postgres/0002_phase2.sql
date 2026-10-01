-- ============================================================
-- NBA 預測平台 — Phase 2 追加 Schema (PostgreSQL)
-- 新增 elo_ratings：逐場記錄每隊 Elo 評分變化，供 walk-forward
-- 回測「重現當時的評分」使用（不可用目前最新評分回推歷史預測，
-- 否則會造成資料洩漏）。
-- ============================================================

CREATE TABLE IF NOT EXISTS elo_ratings (
  id                SERIAL PRIMARY KEY,
  team_id           INTEGER NOT NULL REFERENCES teams(id),
  game_id           INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  season            TEXT NOT NULL,
  game_date_utc     TIMESTAMPTZ NOT NULL,
  opponent_team_id  INTEGER REFERENCES teams(id),
  is_home           SMALLINT DEFAULT 0,
  rating_before     DOUBLE PRECISION NOT NULL,
  rating_after      DOUBLE PRECISION NOT NULL,
  model_version     TEXT NOT NULL,
  computed_at       TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (team_id, game_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_elo_team_date ON elo_ratings(team_id, game_date_utc);
CREATE INDEX IF NOT EXISTS idx_elo_game ON elo_ratings(game_id);
CREATE INDEX IF NOT EXISTS idx_elo_model ON elo_ratings(model_version);
