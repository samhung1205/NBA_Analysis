-- ============================================================
-- Phase E.2 — 賽前裁判分派收集（referees-v1；只累積資料，不接入 ml-v2.0 / 任何策略）
-- ------------------------------------------------------------
-- 來源：https://official.nba.com/referee-assignments/（每個比賽日約 09:00 ET 公布，賽前可能換人）。
-- 只有 Python 排程器讀寫；網站不使用（D1 不需要對應 migration）。
--
-- referee_assignment_reports  每次觀測到的「整頁內容」一列；內容（content_hash）沒變只更新 last_seen_at，
--                             變了才新增一列，舊列永不改寫 → 可做 as-of 查詢（observed_at ≤ T 的最新一列）。
--   observed_at   回應完成的實際時間（不是頁面日期、不是 09:00 ET）
--   report_date_et 頁面標示的 ET 比賽日
-- referee_assignments          該 report 的逐場分派；game_id 為比對到的 games.id（比對不到 → NULL + match_status）。
--   officials     [{role, name, number}]，role ∈ crew_chief / referee / umpire / alternate
-- 賽後 box score 的 officials 是事後名單，不得寫進這兩張表冒充賽前快照。
-- ============================================================

CREATE TABLE IF NOT EXISTS referee_assignment_reports (
  id              BIGSERIAL PRIMARY KEY,
  source          TEXT NOT NULL DEFAULT 'nba_official_referees',
  report_date_et  DATE NOT NULL,
  content_hash    TEXT NOT NULL,
  n_assignments   INTEGER NOT NULL,
  observed_at     TIMESTAMPTZ NOT NULL,
  last_seen_at    TIMESTAMPTZ NOT NULL,
  parser_version  TEXT NOT NULL,
  UNIQUE (source, report_date_et, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_ref_reports_day ON referee_assignment_reports(report_date_et, observed_at);

CREATE TABLE IF NOT EXISTS referee_assignments (
  id              BIGSERIAL PRIMARY KEY,
  report_id       BIGINT NOT NULL REFERENCES referee_assignment_reports(id) ON DELETE CASCADE,
  report_date_et  DATE NOT NULL,
  game_text       TEXT NOT NULL,
  away_label      TEXT NOT NULL,
  home_label      TEXT NOT NULL,
  game_id         INTEGER REFERENCES games(id),
  match_status    TEXT NOT NULL CHECK (match_status IN ('matched', 'unknown_team', 'no_game', 'ambiguous')),
  officials       JSONB NOT NULL,
  UNIQUE (report_id, game_text)
);
CREATE INDEX IF NOT EXISTS idx_ref_assign_game ON referee_assignments(game_id);

-- 0010 契約：新表必須明確啟用 RLS 並撤銷 API 角色（後端以 BYPASSRLS 帳號直連，不受影響）。
ALTER TABLE referee_assignment_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE referee_assignments ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON referee_assignment_reports, referee_assignments FROM PUBLIC;
REVOKE ALL ON SEQUENCE referee_assignment_reports_id_seq, referee_assignments_id_seq FROM PUBLIC;
DO $$
DECLARE api_role text;
BEGIN
  FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
      EXECUTE format('REVOKE ALL ON referee_assignment_reports, referee_assignments FROM %I', api_role);
      EXECUTE format('REVOKE ALL ON SEQUENCE referee_assignment_reports_id_seq, referee_assignments_id_seq FROM %I',
                     api_role);
    END IF;
  END LOOP;
END $$;
