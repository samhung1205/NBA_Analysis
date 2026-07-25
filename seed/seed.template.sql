-- ============================================================
-- 開發期測試資料 —— 樣板 (規格書 §3.1-3)
-- 用途：驗證 UI / API，資料本身是假的，但「讀取路徑走真實 API + DB」。
-- 階段二寫入真實資料後，前端無需修改任何程式碼。
--
-- ⚠️ 這是「樣板」，不可直接執行。請用產生器渲染成純 SQL：
--      node scripts/render-seed.mjs            → 產生 seed/seed.generated.sql
--      npm run db:seed:local                   → 渲染並灌入 D1 (開發期)
--      DATABASE_URL=... npm run db:seed:pg     → 渲染並灌入 Postgres (正式)
--
-- 時間 token（由產生器替換為固定的 ISO8601 UTC 字串，故兩種資料庫方言通用）：
--   {{NOW:-30m}}       → 現在往前 30 分鐘（支援 m / h / d）
--   {{TPE:+1 08:00}}   → 台灣時間「明天 08:00」對應的 UTC 時刻
--   {{TPE:+0 10:00}}   → 台灣時間「今天 10:00」
--   {{TPE:-3 09:00}}   → 台灣時間「3 天前 09:00」
--
-- 使用台灣時間錨點的原因：規格書要求「隔日(台灣時間)賽事列表」，
-- 若改用 UTC 午夜錨定，賽事會落在錯誤的台灣日期而導致總覽頁看似無資料。
-- ============================================================

DELETE FROM model_metrics;
DELETE FROM data_sources;
DELETE FROM bets;
DELETE FROM predictions;
DELETE FROM odds_snapshots;
DELETE FROM injuries;
DELETE FROM player_game_stats;
DELETE FROM team_game_stats;
DELETE FROM games;
DELETE FROM players;
DELETE FROM teams;
DELETE FROM users;

-- ---------------------------- 球隊 ----------------------------
INSERT INTO teams (nba_team_id, abbr, name, name_zh, conference, division) VALUES
  (1610612747, 'LAL', 'Los Angeles Lakers', '洛杉磯湖人', 'West', 'Pacific'),
  (1610612744, 'GSW', 'Golden State Warriors', '金州勇士', 'West', 'Pacific'),
  (1610612738, 'BOS', 'Boston Celtics', '波士頓塞爾提克', 'East', 'Atlantic'),
  (1610612749, 'MIL', 'Milwaukee Bucks', '密爾瓦基公鹿', 'East', 'Central'),
  (1610612743, 'DEN', 'Denver Nuggets', '丹佛金塊', 'West', 'Northwest'),
  (1610612755, 'PHI', 'Philadelphia 76ers', '費城七六人', 'East', 'Atlantic'),
  (1610612752, 'NYK', 'New York Knicks', '紐約尼克', 'East', 'Atlantic'),
  (1610612760, 'OKC', 'Oklahoma City Thunder', '奧克拉荷馬雷霆', 'West', 'Northwest'),
  (1610612748, 'MIA', 'Miami Heat', '邁阿密熱火', 'East', 'Southeast'),
  (1610612742, 'DAL', 'Dallas Mavericks', '達拉斯獨行俠', 'West', 'Southwest');

-- ---------------------------- 球員 ----------------------------
INSERT INTO players (nba_player_id, name, team_id, position, status, is_starter) VALUES
  (2544,    'LeBron James',          (SELECT id FROM teams WHERE abbr='LAL'), 'F',  'active', 1),
  (203076,  'Anthony Davis',         (SELECT id FROM teams WHERE abbr='LAL'), 'F-C','active', 1),
  (1630559, 'Austin Reaves',         (SELECT id FROM teams WHERE abbr='LAL'), 'G',  'active', 1),
  (201939,  'Stephen Curry',         (SELECT id FROM teams WHERE abbr='GSW'), 'G',  'active', 1),
  (202691,  'Klay Thompson',         (SELECT id FROM teams WHERE abbr='GSW'), 'G',  'active', 0),
  (1628369, 'Jayson Tatum',          (SELECT id FROM teams WHERE abbr='BOS'), 'F',  'active', 1),
  (1627759, 'Jaylen Brown',          (SELECT id FROM teams WHERE abbr='BOS'), 'G-F','active', 1),
  (203507,  'Giannis Antetokounmpo', (SELECT id FROM teams WHERE abbr='MIL'), 'F',  'active', 1),
  (203081,  'Damian Lillard',        (SELECT id FROM teams WHERE abbr='MIL'), 'G',  'active', 1),
  (203999,  'Nikola Jokic',          (SELECT id FROM teams WHERE abbr='DEN'), 'C',  'active', 1),
  (1627750, 'Jamal Murray',          (SELECT id FROM teams WHERE abbr='DEN'), 'G',  'active', 1),
  (203954,  'Joel Embiid',           (SELECT id FROM teams WHERE abbr='PHI'), 'C',  'active', 1),
  (1628983, 'Shai Gilgeous-Alexander',(SELECT id FROM teams WHERE abbr='OKC'),'G', 'active', 1),
  (1629627, 'Jalen Brunson',         (SELECT id FROM teams WHERE abbr='NYK'), 'G',  'active', 1),
  (1628389, 'Bam Adebayo',           (SELECT id FROM teams WHERE abbr='MIA'), 'C',  'active', 1),
  (1629029, 'Luka Doncic',           (SELECT id FROM teams WHERE abbr='DAL'), 'G-F','active', 1);

-- ============================================================
-- 比賽：以 UTC now 為基準動態產生
--   明日(台灣) 賽事 → UTC now + 1 天前後（台灣早上時段）
-- ============================================================

-- 明日（台灣）3 場：台灣時間隔日 08:00 / 09:30 / 11:00
INSERT INTO games (nba_game_id, season, season_stage, date_utc, home_team_id, away_team_id, arena, status) VALUES
  ('0022500101', '2025-26', 'regular',
   '{{TPE:+1 08:00}}',
   (SELECT id FROM teams WHERE abbr='LAL'), (SELECT id FROM teams WHERE abbr='GSW'),
   'Crypto.com Arena', 'scheduled'),
  ('0022500102', '2025-26', 'regular',
   '{{TPE:+1 09:30}}',
   (SELECT id FROM teams WHERE abbr='BOS'), (SELECT id FROM teams WHERE abbr='MIL'),
   'TD Garden', 'scheduled'),
  ('0022500103', '2025-26', 'regular',
   '{{TPE:+1 11:00}}',
   (SELECT id FROM teams WHERE abbr='DEN'), (SELECT id FROM teams WHERE abbr='OKC'),
   'Ball Arena', 'scheduled');

-- 今日（台灣）2 場：一場進行中、一場已結束
INSERT INTO games (nba_game_id, season, season_stage, date_utc, home_team_id, away_team_id, arena, status,
                   period, home_pts, away_pts,
                   home_q1, home_q2, home_q3, home_q4, away_q1, away_q2, away_q3, away_q4,
                   home_h1, home_h2, away_h1, away_h2) VALUES
  ('0022500098', '2025-26', 'regular',
   '{{TPE:+0 10:00}}',
   (SELECT id FROM teams WHERE abbr='NYK'), (SELECT id FROM teams WHERE abbr='PHI'),
   'Madison Square Garden', 'live',
   3, 82, 78, 28, 25, 29, NULL, 24, 27, 27, NULL, 53, NULL, 51, NULL),
  ('0022500099', '2025-26', 'regular',
   '{{TPE:+0 08:30}}',
   (SELECT id FROM teams WHERE abbr='MIA'), (SELECT id FROM teams WHERE abbr='DAL'),
   'Kaseya Center', 'final',
   4, 112, 105, 30, 26, 28, 28, 24, 29, 27, 25, 56, 56, 53, 52);

-- 歷史已完成比賽（供近況 / H2H / 回填驗證）
INSERT INTO games (nba_game_id, season, date_utc, home_team_id, away_team_id, status,
                   home_pts, away_pts, home_q1, home_q2, home_q3, home_q4,
                   away_q1, away_q2, away_q3, away_q4, home_h1, home_h2, away_h1, away_h2)
VALUES
  ('0022500001','2025-26', '{{TPE:-3 09:00}}', (SELECT id FROM teams WHERE abbr='GSW'),(SELECT id FROM teams WHERE abbr='LAL'),'final',118,110,30,29,31,28,28,26,29,27,59,59,54,56),
  ('0022500002','2025-26', '{{TPE:-6 09:00}}', (SELECT id FROM teams WHERE abbr='LAL'),(SELECT id FROM teams WHERE abbr='GSW'),'final',105,113,25,27,26,27,29,28,30,26,52,53,57,56),
  ('0022500003','2025-26', '{{TPE:-9 09:00}}', (SELECT id FROM teams WHERE abbr='LAL'),(SELECT id FROM teams WHERE abbr='DEN'),'final',121,116,32,28,30,31,29,30,28,29,60,61,59,57),
  ('0022500004','2025-26', '{{TPE:-12 09:00}}',(SELECT id FROM teams WHERE abbr='PHI'),(SELECT id FROM teams WHERE abbr='LAL'),'final',99,108,24,25,26,24,28,27,26,27,49,50,55,53),
  ('0022500005','2025-26', '{{TPE:-4 09:00}}', (SELECT id FROM teams WHERE abbr='GSW'),(SELECT id FROM teams WHERE abbr='OKC'),'final',108,120,26,25,29,28,32,30,29,29,51,57,62,58),
  ('0022500006','2025-26', '{{TPE:-7 09:00}}', (SELECT id FROM teams WHERE abbr='MIL'),(SELECT id FROM teams WHERE abbr='GSW'),'final',115,109,29,30,28,28,27,26,29,27,59,56,53,56),
  ('0022500007','2025-26', '{{TPE:-2 09:00}}', (SELECT id FROM teams WHERE abbr='BOS'),(SELECT id FROM teams WHERE abbr='NYK'),'final',124,111,33,31,30,30,28,27,29,27,64,60,55,56),
  ('0022500008','2025-26', '{{TPE:-5 09:00}}', (SELECT id FROM teams WHERE abbr='MIL'),(SELECT id FROM teams WHERE abbr='BOS'),'final',107,119,26,27,27,27,30,31,29,29,53,54,61,58),
  ('0022500009','2025-26', '{{TPE:-8 09:00}}', (SELECT id FROM teams WHERE abbr='BOS'),(SELECT id FROM teams WHERE abbr='MIL'),'final',117,112,30,28,30,29,28,29,27,28,58,59,57,55),
  ('0022500010','2025-26', '{{TPE:-3 09:00}}', (SELECT id FROM teams WHERE abbr='OKC'),(SELECT id FROM teams WHERE abbr='DEN'),'final',122,118,32,30,31,29,29,31,28,30,62,60,60,58),
  ('0022500011','2025-26', '{{TPE:-6 09:00}}', (SELECT id FROM teams WHERE abbr='DEN'),(SELECT id FROM teams WHERE abbr='OKC'),'final',114,109,28,29,28,29,27,28,26,28,57,57,55,54),
  ('0022500012','2025-26', '{{TPE:-10 09:00}}',(SELECT id FROM teams WHERE abbr='DEN'),(SELECT id FROM teams WHERE abbr='MIA'),'final',110,102,27,28,27,28,25,26,26,25,55,55,51,51);

-- ---------------------- 球隊單場數據 ----------------------
-- 已結束的 MIA vs DAL 一場，用於驗證單場詳情頁的球隊數據區塊
INSERT INTO team_game_stats (game_id, team_id, is_home, pts, fg_pct, fg3_pct, ft_pct, reb, oreb, dreb, ast, stl, blk, tov, pf, pace, off_rtg, def_rtg, net_rtg)
SELECT g.id, g.home_team_id, 1, 112, 0.478, 0.372, 0.812, 44, 9, 35, 26, 8, 5, 12, 18, 99.4, 116.2, 108.9, 7.3
FROM games g WHERE g.nba_game_id = '0022500099';
INSERT INTO team_game_stats (game_id, team_id, is_home, pts, fg_pct, fg3_pct, ft_pct, reb, oreb, dreb, ast, stl, blk, tov, pf, pace, off_rtg, def_rtg, net_rtg)
SELECT g.id, g.away_team_id, 0, 105, 0.451, 0.341, 0.788, 41, 8, 33, 23, 6, 3, 14, 20, 99.4, 108.9, 116.2, -7.3
FROM games g WHERE g.nba_game_id = '0022500099';

-- ---------------------- 球員單場數據 ----------------------
INSERT INTO player_game_stats (game_id, player_id, team_id, min, pts, reb, ast, stl, blk, tov, fgm, fga, fg3m, fg3a, ftm, fta, plus_minus, started)
SELECT g.id, p.id, p.team_id, 34.5, 24, 9, 6, 1, 1, 3, 9, 17, 2, 5, 4, 5, 8.0, 1
FROM games g, players p WHERE g.nba_game_id='0022500099' AND p.name='Bam Adebayo';
INSERT INTO player_game_stats (game_id, player_id, team_id, min, pts, reb, ast, stl, blk, tov, fgm, fga, fg3m, fg3a, ftm, fta, plus_minus, started)
SELECT g.id, p.id, p.team_id, 37.2, 31, 8, 9, 2, 0, 4, 11, 23, 4, 11, 5, 6, -6.0, 1
FROM games g, players p WHERE g.nba_game_id='0022500099' AND p.name='Luka Doncic';

-- ------------------------- 傷病報告 -------------------------
-- 使用台灣時間錨點，確保申報明確落在預期的台灣日期：
--   · 明日賽事的申報 → 台灣「今日」下午（模擬官方賽前一日 17:00 前申報）
--   · 今日賽事的申報 → 台灣「今日」上午（賽前更新）
-- 這樣傷病中心切到「今日」或「明日」都有資料可驗證。
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 16:30}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500101'), 'Out', 'Left ankle sprain', 'nba_official'
FROM players p WHERE p.name = 'Anthony Davis';
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 16:45}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500101'), 'Questionable', 'Right knee soreness', 'nba_official'
FROM players p WHERE p.name = 'LeBron James';
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 16:20}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500101'), 'Probable', 'Illness', 'nba_official'
FROM players p WHERE p.name = 'Stephen Curry';
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 15:10}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500102'), 'Out', 'Right calf strain', 'nba_official'
FROM players p WHERE p.name = 'Damian Lillard';
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 15:15}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500102'), 'Available', 'Rest management', 'nba_official'
FROM players p WHERE p.name = 'Jayson Tatum';
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 14:40}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500103'), 'Doubtful', 'Lower back tightness', 'nba_official'
FROM players p WHERE p.name = 'Jamal Murray';
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 09:20}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500098'), 'Questionable', 'Left knee management', 'nba_official'
FROM players p WHERE p.name = 'Joel Embiid';
-- 今日進行中賽事的主力缺陣（驗證主力缺陣警示）
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 09:05}}', p.id, p.team_id,
       (SELECT id FROM games WHERE nba_game_id='0022500098'), 'Out', 'Right shoulder sprain', 'nba_official'
FROM players p WHERE p.name = 'Jalen Brunson';
-- 未指定比賽的申報（球隊層級傷病，供傷病中心顯示）
INSERT INTO injuries (report_time_utc, player_id, team_id, game_id, status, reason, source)
SELECT '{{TPE:+0 11:30}}', p.id, p.team_id,
       NULL, 'Questionable', 'Left hamstring tightness', 'nba_official'
FROM players p WHERE p.name = 'Klay Thompson';

-- -------------------------- 預測 --------------------------
-- 明日 3 場，每場 1 筆最新預測（含 features_json 供詳情頁「為什麼這樣預測」）
INSERT INTO predictions (game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                         pred_home_h1, pred_away_h1, pred_home_h2, pred_away_h2, confidence, features_json)
SELECT id, 'elo-v0.1-seed', '{{NOW:-1h}}',
       0.517, 1.4, 228.5, 56.2, 55.1, 58.7, 58.0, 0.54,
       '{"elo_home":1548,"elo_away":1541,"rest_days_home":2,"rest_days_away":1,"b2b_away":true,"form_home_last5":0.60,"form_away_last5":0.40,"injury_impact_home":-4.8,"injury_impact_away":-0.5,"h2h_last5_home_wins":2,"home_court_adv":2.5,"pace_est":99.8,"contributions":[{"label":"主場優勢","value":2.5},{"label":"傷病影響(AD 缺陣)","value":-4.3},{"label":"客隊背靠背","value":1.8},{"label":"近5場戰績","value":1.2},{"label":"Elo 差距","value":0.3},{"label":"休息天數差","value":0.9}]}'
FROM games WHERE nba_game_id='0022500101';

INSERT INTO predictions (game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                         pred_home_h1, pred_away_h1, pred_home_h2, pred_away_h2, confidence, features_json)
SELECT id, 'elo-v0.1-seed', '{{NOW:-1h}}',
       0.712, 6.8, 223.0, 57.4, 53.9, 58.5, 55.2, 0.73,
       '{"elo_home":1642,"elo_away":1573,"rest_days_home":2,"rest_days_away":2,"b2b_away":false,"form_home_last5":0.80,"form_away_last5":0.40,"injury_impact_home":-0.2,"injury_impact_away":-6.1,"h2h_last5_home_wins":3,"home_court_adv":2.5,"pace_est":98.1,"contributions":[{"label":"Elo 差距","value":3.4},{"label":"傷病影響(Lillard 缺陣)","value":3.1},{"label":"主場優勢","value":2.5},{"label":"近5場戰績","value":1.6},{"label":"H2H 優勢","value":0.7},{"label":"賽程密集度","value":-0.4}]}'
FROM games WHERE nba_game_id='0022500102';

INSERT INTO predictions (game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                         pred_home_h1, pred_away_h1, pred_home_h2, pred_away_h2, confidence, features_json)
SELECT id, 'elo-v0.1-seed', '{{NOW:-1h}}',
       0.462, -1.9, 235.5, 58.1, 59.0, 58.8, 59.5, 0.51,
       '{"elo_home":1655,"elo_away":1688,"rest_days_home":1,"rest_days_away":3,"b2b_home":true,"form_home_last5":0.60,"form_away_last5":0.80,"injury_impact_home":-3.9,"injury_impact_away":0.0,"h2h_last5_home_wins":2,"home_court_adv":2.5,"pace_est":101.2,"contributions":[{"label":"Elo 差距","value":-1.7},{"label":"傷病影響(Murray 存疑)","value":-2.6},{"label":"主隊背靠背","value":-2.1},{"label":"主場優勢","value":2.5},{"label":"近5場戰績","value":-0.9},{"label":"休息天數差","value":-1.4}]}'
FROM games WHERE nba_game_id='0022500103';

-- 進行中的比賽也給一筆賽前預測
INSERT INTO predictions (game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                         pred_home_h1, pred_away_h1, pred_home_h2, pred_away_h2, confidence, features_json)
SELECT id, 'elo-v0.1-seed', '{{NOW:-8h}}',
       0.588, 3.2, 218.5, 55.0, 53.4, 55.8, 54.3, 0.61,
       '{"elo_home":1601,"elo_away":1566,"rest_days_home":2,"rest_days_away":1,"contributions":[{"label":"主場優勢","value":2.5},{"label":"Elo 差距","value":1.8},{"label":"傷病影響(Embiid 存疑)","value":1.4},{"label":"客隊背靠背","value":0.6}]}'
FROM games WHERE nba_game_id='0022500098';

-- ------------------------ 盤口快照 ------------------------
-- 台彩盤口（多個時間點，供折線圖驗證）+ 國際盤對照
-- LAL vs GSW（明日）
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-6h}}', id, 'twsport', 'ml',     NULL, 1.83, 1.95, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-3h}}', id, 'twsport', 'ml',     NULL, 1.86, 1.92, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'twsport','ml',   NULL, 1.90, 1.88, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-6h}}', id, 'twsport', 'spread', -1.5, 1.87, 1.87, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-3h}}', id, 'twsport', 'spread', -1.0, 1.87, 1.87, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'twsport','spread',-0.5, 1.87, 1.87, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-6h}}', id, 'twsport', 'total',  230.5, NULL, NULL, 1.85, 1.89 FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-3h}}', id, 'twsport', 'total',  229.5, NULL, NULL, 1.85, 1.89 FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'twsport','total', 228.0, NULL, NULL, 1.86, 1.88 FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'twsport','h1_spread', -0.5, 1.86, 1.88, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'twsport','h1_total', 112.5, NULL, NULL, 1.85, 1.89 FROM games WHERE nba_game_id='0022500101';
-- 國際盤（The Odds API）對照：返還率較高
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'oddsapi','ml',    NULL, 1.95, 1.93, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'oddsapi','spread', -1.0, 1.92, 1.94, NULL, NULL FROM games WHERE nba_game_id='0022500101';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-30m}}', id, 'oddsapi','total', 228.5, NULL, NULL, 1.93, 1.94 FROM games WHERE nba_game_id='0022500101';

-- BOS vs MIL（明日）：模型高信心 + 台彩盤口偏低 → 產生較大 edge
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-4h}}', id, 'twsport','ml',   NULL, 1.55, 2.35, NULL, NULL FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'twsport','ml', NULL, 1.58, 2.28, NULL, NULL FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-4h}}', id, 'twsport','spread', -4.5, 1.87, 1.87, NULL, NULL FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'twsport','spread', -3.5, 1.87, 1.87, NULL, NULL FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'twsport','total', 227.5, NULL, NULL, 1.85, 1.89 FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'twsport','h1_spread', -2.0, 1.86, 1.88, NULL, NULL FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'twsport','h1_total', 111.5, NULL, NULL, 1.86, 1.88 FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'oddsapi','ml',   NULL, 1.62, 2.42, NULL, NULL FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'oddsapi','spread', -4.0, 1.93, 1.93, NULL, NULL FROM games WHERE nba_game_id='0022500102';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-20m}}', id, 'oddsapi','total', 226.5, NULL, NULL, 1.92, 1.95 FROM games WHERE nba_game_id='0022500102';

-- DEN vs OKC（明日）
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-15m}}', id, 'twsport','ml',   NULL, 2.05, 1.75, NULL, NULL FROM games WHERE nba_game_id='0022500103';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-15m}}', id, 'twsport','spread', 2.5, 1.87, 1.87, NULL, NULL FROM games WHERE nba_game_id='0022500103';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-15m}}', id, 'twsport','total', 231.0, NULL, NULL, 1.85, 1.89 FROM games WHERE nba_game_id='0022500103';
INSERT INTO odds_snapshots (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds)
SELECT '{{NOW:-15m}}', id, 'oddsapi','total', 232.0, NULL, NULL, 1.92, 1.95 FROM games WHERE nba_game_id='0022500103';

-- ---------------------- 資料來源狀態 ----------------------
-- 階段一先寫入 seed 值；階段二排程器會持續更新（§3.3-11）
INSERT INTO data_sources (source_key, display_name, category, last_success_at, last_attempt_at, last_status, last_error, expected_interval_min, records_updated) VALUES
  ('nba_api',      'NBA Stats API (stats.nba.com)', 'stats',  '{{NOW:-25m}}', '{{NOW:-25m}}', 'ok',    NULL, 60,  312),
  ('nbainjuries',  'NBA 官方傷病報告',              'injury', '{{NOW:-12m}}', '{{NOW:-12m}}', 'ok',    NULL, 30,  7),
  ('espn',         'ESPN 隱藏 API（備援）',         'stats',  '{{NOW:-40m}}', '{{NOW:-40m}}', 'ok',    NULL, 60,  18),
  ('balldontlie',  'balldontlie API（第二備援）',   'stats',  '{{NOW:-6h}}',    '{{NOW:-6h}}',    'ok',    NULL, 240, 30),
  ('twsport',      '台灣運彩官網（爬蟲）',          'odds',   '{{NOW:-18m}}', '{{NOW:-18m}}', 'ok',    NULL, 30,  22),
  ('oddsapi',      'The Odds API（國際盤）',        'odds',   '{{NOW:-95m}}', '{{NOW:-95m}}', 'warn', '免費層額度剩餘 148/500，已降頻至每 2 小時', 120, 9),
  ('bbref',        'Basketball Reference（歷史回填）','stats', '{{NOW:-3d}}',     '{{NOW:-3d}}',      'ok',    NULL, 10080, 5820),
  ('model_predict','預測引擎（Elo / XGBoost）',      'model',  '{{NOW:-60m}}', '{{NOW:-60m}}', 'ok',    NULL, 120, 4);

-- ---------------------- 模型回測績效 ----------------------
INSERT INTO model_metrics (model_version, season, evaluated_at, n_games, accuracy, ats_accuracy, ou_accuracy, h1_accuracy, log_loss, brier, mae_margin, mae_total, sim_roi, notes) VALUES
  ('elo-v0.1-seed', '2024-25', '{{NOW:-1d}}', 1230, 0.641, 0.512, 0.503, 0.497, 0.6312, 0.2184, 9.84, 14.21, -0.0182,
   'Baseline Elo，walk-forward 回測。ROI 以台彩實際賠率計算，尚未達到獲利門檻，符合預期（階段二 Phase B 驗收線為 accuracy ≥ 63%）。'),
  ('elo-v0.1-seed', '2023-24', '{{NOW:-1d}}', 1230, 0.633, 0.505, 0.498, 0.491, 0.6398, 0.2211, 10.02, 14.58, -0.0264,
   'Baseline Elo 前一季回測結果，作為穩定性對照。');

-- ------------------------- 測試帳號 -------------------------
-- 帳號：demo@example.com　密碼：nba12345678
-- 雜湊由 `node seed/make-password-hash.mjs "<密碼>"` 產生（與 src/lib/auth.ts 同格式）
-- ⚠️ 正式環境請刪除此測試帳號，改用 /login 頁面自行註冊
INSERT INTO users (email, password_hash, display_name) VALUES
  ('demo@example.com',
   'pbkdf2$210000$aM/7vl/sgW2Sllr61lDirQ==$mhclR2wcId1f3yv27DPMpO4/NwcRiRBN5kQr/Y5k8z4=',
   '開發測試帳號');
