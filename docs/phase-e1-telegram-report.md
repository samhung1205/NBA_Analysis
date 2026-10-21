# Phase E.1 — Telegram 推播（notify-v1）報告（2026-10-15）

狀態：**本機已實作與驗證，尚未部署**；正式資料庫尚未套用 `0009_phase_e1_notifications.sql`。

> 資料來源判斷後續更新：[免費來源實測與封鎖處理](data-source-access-2026-10-15.md)。球員基本資料、旅行與裁判不必全部依賴 stats.nba.com；CDN 五人 lineup 重建仍待獨立驗證。下表保留當時選擇推播的背景。

## 1. 範圍與取捨

規格書 Phase E 有三項選做：球員 / lineup / 旅行 / 裁判、line movement、Telegram/LINE 推播。本次只做推播，原因：

| 項目 | 現況判斷 |
|---|---|
| 推播 | 只讀已物化結果，不改模型 / 風控，可立即完成 |
| Line movement | 正式 DB 盤口 0 筆，必須先累積 2026-27 真實台彩序列 |
| 球員 / lineup / 裁判 / 旅行 | On/Off、lineup 需要 stats.nba.com（使用者 IP 被擋）；2024-25 / 2025-26 已是看過的測試季，新模型需以 2026-27 前瞻驗證，屬研究題目 |

使用者選擇：**Telegram**（LINE Notify 已停止服務，Messaging API 免費額度有限）；內容為 **ACTIONABLE 機會、每日賽事摘要、系統異常告警**（不含 HAR 提醒）。

## 2. 設計

程式：`pipeline/core/notify/`（`telegram.py` 傳送、`messages.py` 純格式、`job.py` 三個 job），CLI `pipeline/run_notify.py`，migration `migrations/postgres/0009_phase_e1_notifications.sql`。

| Job | 排程（台灣） | 資料來源 | 去重 |
|---|---|---|---|
| `notify_actionable` | 每分鐘 :50 秒 | 使用者最新的 `decision_snapshots`（需 ≤ 10 分鐘內確認過）→ `decision_opportunities` 中台彩、尚未開賽、`status_group = actionable` | 每個 game × 玩法 × 選項 × 盤口線 × bookmaker × 賠率一列；金額 / EV 變動不重送，賠率或盤口線改變視為新報價 |
| `notify_daily_summary` | 12:40 | 台灣明日 eligible（regular / playin / playoffs）未結束賽事 + 每場最新預測（與網站相同取法） | `daily_summary:<日期>` |
| `notify_system_alerts` | :04 / :19 / :34 / :49 | `readiness.run_checks(post_deploy=True)` 的 FAIL 集合 | 集合改變才送（新增 / 恢復）；不變每 6 小時提醒；以 `area/name` 比對，detail 文字變動不算新告警 |

邊界（沿用 AGENTS.md 與 D.5 契約）：

- 不計算任何機率、EV、Kelly、exposure、額度；全部來自 Python 已物化的表。
- 只推台彩；國際盤（diagnostic）永不推播。平台不會自動下注；每則附免責提醒，不使用「推薦」。
- 物化過期（> `MATERIALIZATION_STALE_AFTER` 10 分鐘）的決策中心不推播，避免把失效額度當有效。
- **REVIEW**（qualified 但有資料品質旗標）預設不推；`TELEGRAM_NOTIFY_INCLUDE_REVIEW=true` 時一併推播並逐筆標示 `[REVIEW 需檢視]` 與旗標。測試中發現合成資料的 qualified 機會全在 REVIEW；開季初期（`season_opener`、`low_sample`、`injury_unknown`）真實環境也可能如此，因此提供此選項，但不改變 decision-v1 的分組。
- 單一 chat 對應單一網站使用者：資料庫只有一位使用者時自動選取，否則需 `TELEGRAM_NOTIFY_USER_ID`。

可靠性：

- `notification_events.dedup_key` UNIQUE；先 `INSERT … ON CONFLICT DO NOTHING` 領取再送，誤開兩個 replica 也不會重送。
- 送出失敗保留 `failed` 與錯誤摘要，最多重試 3 次；送出途中行程終止留下的 `pending` 10 分鐘後可重新領取。
- 系統告警在資料庫本身連不上時改用行程記憶體去重並直接送出；重啟後前 15 分鐘不告警（心跳尚未恢復屬預期）。
- token 只出現在請求 URL；例外訊息只保留類別 / HTTP 狀態 / Telegram description，並遮蔽 token；不寫入 log 或 DB。
- 未設定 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 時三個 job 立即返回，不連 DB、不寫心跳。設定後心跳寫入 `data_sources.notify_telegram`（狀態頁分類「推播」）。
- `run_production_check.py` 新增 `notify / telegram`：未設定 = WAITING；只設一個或 0009 未套用 = WARN。0009 不列入必要 migrations。

已知限制：排程器整個停機時無法自我告警（需外部監控）；LINE 未實作；HAR 匯入提醒未做（使用者未選）。

## 3. 驗證

| 檢查 | 結果與範圍 |
|---|---|
| `tests/test_notify.py` | **11 passed**：訊息拆分、送出格式、token 不外洩（網路例外 / HTTP 401）、ACTIONABLE / REVIEW / 摘要格式與 HTML escape、去重 key、readiness 分類、未設定不碰 DB、系統告警新增 → 不變 → 6 小時提醒 → 部分恢復 → 全部恢復（DB 不可用路徑）、送出失敗下次重送、啟動寬限 |
| `tests/test_notify_db.py` | **7 passed**（暫存 schema，約 2 分鐘）：真實 D.2 → D.3 → D.5 物化後推播一次且不重送、dry-run 知道已送過、預設只推 ACTIONABLE、失敗重試上限、過期 snapshot 不推、多使用者需指定、每日摘要一天一則 / 無賽事不送、系統告警狀態在重啟後由 DB 延續 |
| `pytest -m 'not db'`（全部） | **618 passed**（原 607 + 新增 11；排程 job 數測試改為 16） |
| `scheduler.py --print-next-runs` | 三個新 job 皆有下一次觸發時間 |
| 正式 DB 唯讀 dry-run | `--job system`：目前 0 個 FAIL → 不會告警；`--job daily --date 2026-10-21`：正式 DB 尚無該日賽事（同步視窗未到，符合稽核說明） |

未執行：真實 Telegram 送出（需使用者建立 bot 與提供 token）、正式 migration、Railway 部署、完整 141 項 DB suite（本次只改新增模組、readiness 一個檢查與排程清單；相關 DB 測試已跑）。

## 4. 啟用步驟

1. 部署前：`npm run db:migrate:pg` 套用 0009（需使用者授權）。
2. 依 [production-runbook.md](production-runbook.md) §4c 在 Railway 設定 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（選填 `TELEGRAM_NOTIFY_USER_ID`、`TELEGRAM_NOTIFY_INCLUDE_REVIEW`、`NOTIFY_SITE_URL`）。
3. 本機 `run_notify.py --test` 確認收到訊息；部署後 `run_production_check.py` 的 `notify / telegram` 應為 PASS。
4. 開季後觀察第一批 ACTIONABLE / REVIEW 數量，再決定是否開啟 REVIEW 推播。
