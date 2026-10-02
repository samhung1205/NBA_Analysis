# Phase C.5A — Data Reliability Hardening 報告

日期：2026-10-02　範圍：資料來源、排程、資料完整性（未調 ML、未動前端/API contract、未改 DB schema）

## 1. Root causes

| # | 問題 | 根因 | 證據 |
|---|---|---|---|
| 1 | stats.nba.com 持續逾時 | `nba_stats._headers()` 把 UA 覆寫成 **Chrome 120**，但 nba_api 1.11.4 預設的 `Sec-Ch-Ua` 宣告 **Chrome 145**：UA 與 client-hints 互相矛盾。另外每支 endpoint 都傳 `headers=`，等於繞過套件日後的標頭更新。（本機 IP 仍被 Akamai 對 `/stats/*` 靜默丟包，這點非程式可解，所以才需要 fallback。） | 讀 `nba_api/stats/library/http.py` 預設標頭 vs `config.py` 的 Chrome 120 UA |
| 2 | 只有 stats.nba.com 一個主來源，逾時就整條 pipeline 停 | 賽程/比分/box score 全部走 stats.nba.com，沒有 fallback 鏈，沒有來源追蹤 | `daily.py` 舊版直接呼叫 `fetch_scoreboard_day`，例外向上拋 |
| 3 | 排程「看起來啟動、實際不跑」 | (a) `add_job(..., next_run_time=None)` 在 APScheduler 3.x 是**建立成暫停**；(b) `CronTrigger(hour=12)` 沒帶 timezone，傳入 trigger 物件時**不套用** scheduler 的 `Asia/Taipei`，而是機器本地時區（容器多為 UTC → 台灣 20:00 才跑）。舊版靠啟動時手動跑一次掩蓋。 | 實測 `get_job().next_run_time is None`；`test_regression_next_run_time_none_means_paused` |
| 4 | 傷病 job 永遠找不到剛發布的報告 | `nbainjuries` 的 timestamp 是**美東當地 naive 時間**，舊版傳 UTC（差 4~5 小時）；只探測整點，但 2025-12-22 起報告為 15 分鐘一份 | 讀 `nbainjuries/_util.py`；實測 `Injury-Report_2026-10-01_12_45PM.pdf` 可抓 |
| 4b | 傷病即使抓到也寫 0 筆（**額外發現**） | 官方報告球員名是 `Brown, Jaylen`，球員表是 `Jaylen Brown`，舊版 lower-case 後直接比對 → 永不相符；`NaN` 狀態列還會 `.strip()` 崩潰 | 實抓 10/01 報告確認格式 |
| 4c | 傷病「同一 snapshot 重跑」不冪等 | `insert_injury_if_changed` 拿「全表最新一筆」比較：補跑較舊報告會誤判為狀態變化而重複寫入 | `test_later_report_only_inserts_changes_and_old_rerun_does_not_flip` |
| 5 | 台灣早上漏掉前一個 ET 賽事日 | 把「台灣今天/明天」日期字串直接當 **ET 日期**丟給 ScoreboardV3：台灣 10:00 = ET 前一天 22:00，ET 昨天的比賽（進行中/剛結束）沒被刷新，賽後結算被漏掉 | `test_taipei_morning_still_sees_previous_et_game_day` |
| 5b | 季前賽被當例行賽寫入（**額外發現**） | 舊 daily job 對 10 月賽事一律標 `season_stage='regular'`、賽季用月份推算 | CDN 賽程含 67 場 `001` 季前賽 |

## 2. 修改內容

**新增**
- `pipeline/core/timeutil.py` — UTC / ET / TPE 換算集中地：`refresh_window_utc`、`et_dates_between`、`in_window`（以 `game_time_utc` 判定）、`to_et_naive`、`et_naive_to_utc`。
- `pipeline/core/sources/nba_cdn.py` — cdn.nba.com：整季賽程、當日 scoreboard、基本 box score（含逐節/先發/分鐘）。標頭用 nba_api **stats** 預設（去掉 Host）；nba_api `live` 預設標頭（Chrome 87）實測 403，故不用。
- `pipeline/core/sources/common.py` — 各來源共用的賽事正規化（stage/season 由 gameId 判定，季前賽/明星賽不收錄）。
- `pipeline/core/sources/fallback.py` — `fetch_with_fallback`（逐來源嘗試並保留 attempts）、`CircuitBreaker`（連續失敗 2 次冷卻 10 分，避免被封時每次白等逾時）、`report_attempts`（寫 `data_sources` 心跳）。
- `pipeline/core/jobs/games_sync.py` — 賽程/比分/結算同步：CDN → stats → ESPN；box score CDN → stats；進階數據僅 stats 且失敗不中止。
- `pipeline/core/scheduling.py` — 排程定義（可不啟動排程器驗證 next-run-time）。
- `pipeline/tests/*`、`pipeline/pytest.ini`、`pipeline/requirements-dev.txt`。

**修改**
- `core/sources/nba_stats.py` — 移除 `_headers()` 與所有 `headers=`；新增 `fetch_games_for_et_dates`（fallback 鏈內每日期只重試 2 次、快速失敗）；timeout / retry / 隨機延遲保留。
- `core/sources/espn.py` — 新增正規化與按 ET 日期查詢。
- `core/sources/injuries.py` — ET naive 時間、15/30/60 分鐘探測（舊制整點時段只探測整點）、`Last, First` 名稱正規化。
- `core/jobs/daily.py` — 薄入口 + `ingest_injury_rows` / `fetch_injuries_job`（可注入 check/fetch 以便測試）。
- `core/db.py` — `insert_injury_if_changed` 冪等化（同報告同內容跳過；與「該報告時間之前」最近一筆比較）；新增 `find_game_by_matchup`。
- `scheduler.py` — 改用 `core/scheduling.py`；`--print-next-runs`、`--no-startup-run`。
- `core/config.py`、`.env.example` — 移除 `NBA_API_USER_AGENT`。
- `pipeline/requirements.txt` — 明確加入 `tzdata`（slim 映像的 `zoneinfo` 需要）。
- `README.md` — 修正過時描述（見 §5）。

**排程（台灣時間）**：每日 12:00 賽程/比分；每 5 分鐘依 DB 內 `game_time_utc` 刷新進行中/待結算賽事（沒有就不打外部來源）；傷病 00:00–10:59 每 15 分、11:00–23:59 每 30 分。

**未動**：前端/API contract、Postgres schema/migrations、ML 模型與回測。

## 3. 測試與結果

| 項目 | 結果 |
|---|---|
| Python `pytest`（`cd pipeline && pytest`） | **84 passed**（約 160 秒；`-m "not db"` 57 項純邏輯 < 1 秒） |
| Node `npm run test:api` | **90 通過 / 0 失敗**（對本機 D1 seed 執行，見下方說明） |

Python 測試涵蓋：

- **scheduler**：所有 job 非 paused（含 `run_on_start`）、舊 bug 回歸（`next_run_time=None` → paused）、每個 Cron 為 `Asia/Taipei`、每日 12:00 TPE 的 next-run、傷病 15/30 分鐘 next-run、補跑一次後仍照 trigger。
- **時區邊界**：UTC↔ET↔TPE、台灣早上仍含 ET 前一天、DST 回撥/春進、半開區間、視窗以 `game_time_utc` 過濾（不以日期字串）。
- **傷病**：ET naive（不是 UTC）、15/30/60 分鐘網格、舊制只探測整點、DST 無重複候選、DB 存 UTC、同 snapshot 重跑 0 新增、補跑舊報告不翻轉、無報告/解析失敗只記心跳不拋例外。
- **fallback**：primary 失敗 → fallback、stats 逾時不中止、全部失敗、空結果算成功、斷路器開/半開/復原、ESPN 備援不與官方列重複、官方 ID 認領 `espn:` 暫存列、final 不被過期快取倒退、box CDN 失敗 → stats、進階數據失敗不中止且下次補齊。
- **冪等**：game / box / player / heartbeat / injury 重複寫入；局部 UPSERT 不清掉既有逐節。
- **心跳**：ok / warn（被備援）/ error（全失敗）/ optional 降級 / 復原清 error 且保留 `last_success_at` / 一來源一列 / 備援過期門檻放寬。
- **headers**：不再覆寫 UA、端點呼叫不帶 `headers=`、timeout/retry/延遲保留、CDN 標頭 = nba_api 預設去 Host。

DB 測試在真實 Postgres 內建**獨立暫存 schema**（`c5a_test_*`）套用 migrations，結束後 `DROP SCHEMA`；已確認無殘留、`public` 資料未動（games 6605 / data_sources 8 / injuries 1 前後一致）。

實網唯讀驗證（未寫入正式 DB）：CDN 賽程 1200 場例行賽（過濾 67 場季前賽）、CDN box score 2021-22 ~ 2025-26 與季後賽皆可取、ESPN 日期查詢、nbainjuries 找到並解析 `2026-10-01 12:45 PM ET` 報告。

**Node 測試說明**：`smoke-test.mjs` 依賴 seed 資料；Supabase 在先前階段已換成真實回填資料並清掉 seed 賽事，對它執行會 `games=0`（與本階段無關）。故改對本機 D1 seed 執行並通過；`.dev.vars` 與 Supabase 皆未改動。README 已註明。

## 4. 剩餘風險

1. **stats.nba.com 在本機 IP 仍被擋**（Akamai 丟包，非程式問題）。目前靠 CDN/ESPN 備援；**進階數據（pace/ORtg/DRtg/TS%）仍只有 stats.nba.com 來源**，在被擋的 IP 上會維持 NULL（心跳標 `warn`，不中止）。
2. **未對正式 DB 做端到端排程實跑**：現在是休賽期（CDN 例行賽 10/20 開打），沒有可驗證的真實賽事；寫入邏輯是在暫存 schema 用替身來源驗證。開季第一週請觀察 `/status`。
3. **即時比分為 5 分鐘輪詢**，規格書 §5 寫「比賽中每 60 秒」；刷新邏輯已就位，要加密只需改 interval（每次會下載約 5 MB schedule JSON，gzip 後數百 KB）。
4. **傷病**：(a) 球員在新報告中「消失」（復原）不會寫入「已回歸」；(b) 同名球員整批略過、改名/暱稱差異會算未對應（心跳 `last_error` 顯示筆數）；(c) `injuries` 無 unique 約束，冪等靠 SELECT-then-INSERT，兩個行程同時寫同一報告理論上可能雙寫（排程 `max_instances=1`、peak/offpeak 時段不重疊，風險低）；(d) 美東秋季回撥重複的 01:xx 取第一次；(e) nbainjuries 會在 stdout 印 `Failed validation` 雜訊，無害；(f) `game_id` 仍為 NULL（未與賽事關聯）。
5. 斷路器為**單一行程記憶體**；多實例各自獨立。
6. ESPN 備援依「主客隊 + 開賽 ±12 小時」比對既有賽事；ESPN `dates=` 以 ET 日曆日解讀為本次的假設（已用視窗以 `game_time_utc` 二次過濾、以 event id 去重）。
7. DB 測試約 2.5 分鐘（遠端 Supabase 延遲）；無 DB 時自動 skip。
8. Docker 映像未重建/未實跑（僅 requirements 加 `tzdata`）。

## 5. README 修正

- 「階段二未實作」→ 改為 Phase A~C 已實作。
- 踩坑 3 原本教人「合併 `NBAStatsHTTP.headers` 再覆寫 UA」，與現行做法相反，已更正；新增 CDN 標頭、APScheduler、nbainjuries 時間語意三條踩坑與資料來源/排程簡表。
- 註明 `test:api` 需 seed 資料；更新「建議下一步」與最後更新日期。開發細節不放 README，統一在本報告。

## 6. Phase C.5B 可以開始嗎？

**可以。** 比 README 原先假設更樂觀：CDN `boxscore_<id>.json` 在本機 IP 對 2021-22 ~ 2025-26 全部可取（含逐節、先發、分鐘、plus/minus），所以**球員/球隊基本 box score 回填不再需要被擋的 IP**，可直接沿用 `fetch_box_basic` + `_store_basic`。

C.5B 的前置事項：
- 進階數據（pace/off_rtg/def_rtg）：選 (a) 在未被擋的主機跑 `fetch_box_advanced`，或 (b) 用基本 box 自行估算 possessions（`FGA + 0.44·FTA − OREB + TOV`）推出 pace/ORtg/DRtg，避免依賴 stats.nba.com。建議先做 (b) 讓特徵可用，再以 (a) 校驗。
- 歷史傷病回填（`nbainjuries` 自 2021-22 起）：需寫按日期回補 job；可直接重用 `candidate_report_times` / `ingest_injury_rows`（已冪等、已修名稱對應），回補前先確認對應率。
- 回填大量 box score 時沿用 `fetch_with_fallback` 與斷路器；CDN 為靜態檔，仍請保留禮貌性間隔。
