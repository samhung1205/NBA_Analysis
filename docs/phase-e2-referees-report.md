# Phase E.2 — 賽前裁判分派收集（referees-v1）報告（2026-10-21）

狀態：0011 已套用到正式 DB（2026-10-21）；程式已 push，Railway 部署狀態見 §5。

## 1. 為什麼先做這一項

- 官方裁判分派每個比賽日約 09:00 ET 公布，賽前可能換人。**賽前版本只能在當天觀測**；賽後 box score 的 officials 是事後名單，不能回填成「當時已知」。每晚一天開始收集，就少一天可用的前瞻資料。
- parser 已在 [data-source-access-2026-10-15.md](data-source-access-2026-10-15.md) 完成並實測；本次補上持久化、比對與排程。
- **只累積資料，不接入 `ml-v2.0`、定價、sizing 或任何策略。** 裁判特徵是否有用，需等累積足夠的 2026-27 前瞻樣本後，另立版本研究。

## 2. 設計

程式：`pipeline/core/jobs/referee_collect.py`、CLI `pipeline/run_referees.py`、migration `migrations/postgres/0011_phase_e2_referees.sql`。

| 項目 | 做法 |
|---|---|
| 排程 | `referee_assignments`：台灣 20:07–10:37 每 30 分（= ET 08:07–21:37，夏令 / 冬令取聯集；維持「所有 Cron 皆為 Asia/Taipei」的既有測試）。16 小時內沒有尚未開賽的 eligible 比賽 → 不發請求 |
| 版本化 | `referee_assignment_reports` 以 (source, report_date_et, content_hash) 唯一。內容相同只推進 `last_seen_at`；換人 → 新列；舊列不改寫 |
| 觀測時間 | `observed_at` = 回應完成時間；`report_date_et` 照頁面記錄。早上仍是昨天的頁面會照實記成昨天的日期 |
| 逐場 | `referee_assignments`：原始對戰文字、`officials` = [{role, name, number}]（crew_chief / referee / umpire / alternate） |
| 比對 | 官方頁用城市名（`L.A. Lakers` / `LA Clippers` 區分洛杉磯兩隊）→ 30 隊對照表 → 同一 ET 比賽日的 games。比不到記 `unknown_team` / `no_game` / `ambiguous`，`game_id` 留 NULL；賽程晚同步時下一輪補上（只改比對結果，不改觀測內容） |
| 失敗 | 403 / 連線失敗 / 頁面改版 / 缺裁判 → 不寫入、不重試、不輪替 IP，心跳 `data_sources.nba_referees` = error。空表（尚未公布）不寫入 |
| 安全 | 新表明確 `ENABLE ROW LEVEL SECURITY` 並撤銷 PUBLIC / anon / authenticated 權限（runbook §3a 契約） |

as-of 查詢規則（供日後研究）：某場比賽在 T 時點已知的裁判 = 該場 `observed_at ≤ T` 的最新一列。

## 3. 驗證

| 檢查 | 結果與範圍 |
|---|---|
| `tests/test_referee_collect.py` | **13 passed**（純邏輯 5 + 暫存 schema DB 8）：30 隊與洛杉磯變體對照、裁判編號解析、內容 hash 與列順序 / 觀測時間無關、比對狀態；無比賽不發請求、儲存與比對、相同內容去重、賽前換人新增版本且 as-of 查得到舊版、賽程晚同步補比對、403 / 斷線 / 改版不寫入且不重試、空表與昨天頁面、新表 RLS |
| `pytest -m 'not db'`（全部） | **642 passed**（排程 job 數測試改為 17） |
| `tests/test_data_api_security_db.py` | **5 passed**（含新表後仍符合 0010 鎖定契約） |
| 正式環境唯讀 dry-run（2026-10-21 14:3x UTC） | 官方頁 `report_date_et = 2026-10-21`、11 場，**11 場全部比對到正式 DB 賽事**；未寫入 |

未執行：正式 migration 0011、Railway 部署、實際排程跨日運行（換人、隔日頁面更新）的觀察。

## 4. 啟用步驟

1. 套用 migration（需使用者授權）：`node --env-file=pipeline/.env scripts/pg-migrate.mjs`，應只套用 0011。
2. Commit 並 push 到 `main`，Railway 自動部署；Deploy logs 應有 17 行「已登錄 job」。
3. 隔天確認：`data_sources.nba_referees` 心跳 ok，`referee_assignment_reports` 有當日一列以上、`referee_assignments` 的 `match_status` 皆為 matched。
4. `run_security_check.py` 應維持 PASS。

## 5. 部署後修正：頁面日期 ≠ 賽程日期（2026-10-24）

- 10/21 套用 0011 並 push 後，Railway 正逢全平台故障（builds paused，[事件頁](https://status.railway.com/incident/QH8L8B3E)），新 job 未部署；10/21 ET 的 11 場以本機手動執行 `run_referees.py` 收進正式 DB（全部賽前、全部比對成功）。10/22、10/23 ET 的賽前分派**沒有收到，無法補回**。
- 10/24 00:50 ET 手動收集時發現：官方頁的 entry-meta 是**頁面當下的 ET 日期**；新一輪公布前，頁面仍列前一個比賽日的對戰卻標成今天，導致 12 場全部 `no_game`。
- 修正：比對範圍改為頁面日期當天與前一天；整份對到前一天時 `state = previous_slate`（正常、不告警）；同一對戰連兩天都有比賽時記 `ambiguous`、不猜。頁面日期仍照實存在 `report_date_et`，`observed_at` 不變，所以開賽後才觀測到的分派在 as-of 查詢中自然被排除（這 12 筆 pregame = 0）。
- 閒置時也更新 `data_sources.nba_referees` 心跳，否則沒有比賽的時段看不出 job 是否在跑。
- 回歸：`test_page_dated_today_but_listing_previous_slate`；`test_referee_collect.py` 14 passed，`pytest -m 'not db'` 646 passed。

## 6. 後續研究（尚未開始）

累積至少半個賽季的前瞻資料後，再評估裁判特徵（例如裁判組合的歷史罰球率 / 節奏偏差）。歷史裁判統計須只用 T 以前已結束比賽的 box score，並與現行 `ml-v2.0` 分開版本化；不得以已看過的 2024-25 / 2025-26 評測季反覆調參。
