-- ============================================================
-- 球員現役名單同步（roster-sync-v1）
-- ------------------------------------------------------------
-- 問題：players.team_id 原本只由 refresh_player_current_team() 依「最近一場出賽」回填，
-- 休賽季轉隊 / 新秀 / 被釋出的球員在新球隊出賽前一直掛在舊球隊（例：Klay Thompson DAL → MIA）。
--
-- roster_synced_at：最近一次以官方現役名單（stats.nba.com playerindex，ESPN 備援）設定 team_id 的時間。
--   refresh_player_current_team() 只在「最近出賽晚於這個時間」時才改寫 team_id，
--   名單同步後不會被舊賽季的最後一場 box score 蓋回去。
-- 只有 Python 排程器 / CLI 讀寫；網站不使用（D1 不需要對應 migration）。
-- ============================================================

ALTER TABLE players ADD COLUMN IF NOT EXISTS roster_synced_at TIMESTAMPTZ;
