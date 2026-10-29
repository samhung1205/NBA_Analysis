# NBA 對戰預測平台（NBA Matchup Prediction Platform）

每天自動分析 NBA 賽事，輸出**全場勝負、讓分、大小分、上半場**的模型機率，並與**台灣運彩（台彩）**盤口比對，計算去水後的 edge、期望值（EV）與風控後的理論注碼，輔助個人投注決策。

> ⚠️ **免責聲明**：本平台僅為個人研究與紀錄工具，**不構成任何投注建議**。模型歷史表現不代表未來結果，目前**沒有任何真實投注獲利證據**。平台**不會自動下注**，所有下注由使用者自行在台彩完成。台彩含抽水（返還率約 85%），長期期望值通常為負。

- 程式碼倉庫：https://github.com/samhung1205/NBA_Analysis
- 文件更新：2026-10-15

---

## 目錄

1. [平台能做什麼](#1-平台能做什麼)
2. [系統架構](#2-系統架構)
3. [開始使用：帳號與登入](#3-開始使用帳號與登入)
4. [頁面操作說明](#4-頁面操作說明)
5. [每日使用流程](#5-每日使用流程)
6. [匯入台彩盤口（HAR）](#6-匯入台彩盤口har)
7. [名詞解釋](#7-名詞解釋)
8. [系統狀態與維運](#8-系統狀態與維運)
9. [本機開發與測試](#9-本機開發與測試)
10. [部署](#10-部署)
11. [指令速查](#11-指令速查)
12. [專案結構](#12-專案結構)
13. [目前狀態與已知限制](#13-目前狀態與已知限制)
14. [延伸文件](#14-延伸文件)

---

## 1. 平台能做什麼

| 功能 | 說明 |
|---|---|
| 賽事預測 | 模型 `ml-v2.0` 預測勝率、分差、總分、上半場分差/總分，並提供任意盤口線的過盤機率（含 push） |
| 盤口比較 | 台彩 vs 國際盤（The Odds API）vs 模型，逐盤口計算去水公允機率（fair probability）、edge 與 EV |
| 傷病追蹤 | 自動抓取 NBA 官方傷病報告，標示主力缺陣 |
| 決策中心 | 依凍結的風控規則 `risk-v1`，結合你**已經下注**的金額，算出今天每個機會還能下多少 |
| 下注紀錄 | 手動記錄在台彩實際下的注，可作廢 / 更正（保留完整稽核軌跡），比賽結束自動結算 |
| 資金管理 | 手動維護的 strategy bankroll（存入 / 提出 / 調整），每日凍結 day-start 金額供風控使用 |
| 績效追蹤 | 模型回測指標、個人實際損益曲線、前瞻 paper strategy 紀錄 |
| 系統監控 | 各資料來源最後更新時間、新鮮度與失敗告警 |
| Telegram 推播（選用） | 新的 ACTIONABLE 機會、每日賽事摘要、系統異常告警推送到你的 Telegram |

## 2. 系統架構

```
 NBA CDN / ESPN / 官方傷病 PDF ─┐
 台彩 Chrome 同步 / HAR 備援 / The Odds API ─┤
                                 ▼
          Railway 排程器（Python，19 個 jobs，24 小時常駐）
          擷取 → 特徵 → 預測 → 定價 → 注碼 → paper → 決策物化 → 推播
                                 │ 讀寫
                                 ▼
                     Supabase Postgres（共用資料庫）
                                 ▲ 讀寫
                                 │
          Cloudflare Pages（Hono 網站 + API + 登入）◀── 你的瀏覽器
```

- **網站（Stage 1）**：`src/`，Hono + TypeScript，部署在 Cloudflare Pages。網站只負責顯示與記錄，不做任何機率或風控運算。
- **資料與模型引擎（Stage 2）**：`pipeline/`，Python。所有預測、去水、Kelly、風控、結算都在這裡計算並寫入資料庫。
- **資料庫**：正式環境用 Supabase Postgres；本機開發可用 Cloudflare D1（SQLite）。
- **Supabase 資料存取安全**：後端直接連 Postgres；公開 Data API 不供本專案使用。RLS 與 API 權限修正、唯讀檢查方式見 [維運手冊 §3a](docs/production-runbook.md#3a-supabase-data-api-安全邊界2026-10-21)。

## 3. 開始使用：帳號與登入

1. 開啟網站，進入 `/login`。
2. **註冊**：正式環境在已有使用者後**預設關閉註冊**。第一次建立帳號時，需由管理者暫時設定 `ALLOW_REGISTRATION=true`，註冊完成後移除該變數並重新部署（見 [production-runbook.md](docs/production-runbook.md) §3）。
3. 新註冊密碼至少 **12 字元**。
4. 登入後才能使用：**今日決策中心**、**資金**、**投注紀錄**、個人損益。賽事、盤口、傷病、系統狀態為公開頁面（不含個人資料）。

> 本機開發的 seed 測試帳號 `demo@example.com` / `nba12345678` 是公開密碼，**只能用在本機 D1**，絕不可出現在正式資料庫。

## 4. 頁面操作說明

上方導覽列可切換各頁面。所有時間皆以**台灣時間（Asia/Taipei）**顯示。

### 4.1 賽事總覽 `/`

平台首頁，預設顯示**明日**賽事（台灣時間），這是最主要的使用情境。

- 上方切換 **今日 / 明日 / 後天**，或用日期選擇器挑任意日期。
- 每張比賽卡片顯示：對戰組合、開賽時間、模型勝率、預測分差 / 總分、上半場預測、台彩 vs 國際盤 vs 模型的比較。
- 「模型差距（診斷）」= 模型機率 − 台彩去水公允機率，只看市場分歧，不是推薦；差距較大時只以粗體與邊框標示，不用綠色或火焰。
- 能不能下、能下多少只看決策中心（已扣除你實際下注的額度）；卡片底部「能不能下、下多少 → 決策中心」直接帶到同一天。
- 點「查看詳情」進入單場頁面；詳情頁的理論注碼尚未扣除已下注額度，國際盤列只標「僅診斷」。

### 4.2 單場詳情 `/games/:id`

| 區塊 | 用途 |
|---|---|
| 盤口定價 | 每個 bookmaker × 玩法 × 選項：原始隱含機率、去水公允機率、模型機率、push 機率、edge、EV |
| 理論注碼 | risk-v1 調整後的理論下注比例（risk-adjusted stake）與不合格原因 |
| 特徵拆解 | 「為什麼這樣預測」：各特徵對預測的貢獻 |
| 盤口變動圖 | 盤口線與賠率的時間序列折線圖 |
| 逐節 / 半場比分 | 已開打或已結束比賽的分節成績 |
| 傷病、球隊數據、近況、H2H | 賽前參考資訊 |

### 4.3 今日決策中心 `/decision`（需登入）

把「台彩盤口 × 模型 × 風控 × 你的資金與已下注金額」整合成一張決策表。**平台不會自動下注**：你在台彩下注後，回到這裡按「**記錄下注**」。

機會分組：

| 標籤 | 意義 |
|---|---|
| **ACTIONABLE** 可操作 | 正 EV、盤口新鮮、仍在風控額度內 |
| **REVIEW** 需檢視 | 有條件需要你確認（例如資料品質警示） |
| **RECORDED** 已記錄 | 這個比賽 × 玩法你已記錄過下注（不會再加碼） |
| **BLOCKED** 受阻 | 額度用完、盤口過期、互斥選項等原因無法操作 |
| **NO EDGE** 無正 EV | 模型認為沒有正期望值 |
| **DIAGNOSTIC** 國際盤診斷 | 國際盤只作參考對照，不可操作 |

- 右側 **Evidence** 顯示目前的證據狀態（例如是否已有前瞻樣本）；**System readiness** 顯示各資料來源健康度。
- 記錄下注時填入**實際賠率、實際金額、下注時間**。金額不會預填，可按「帶入建議上限」；晚一點才記錄時請把下注時間改成實際時間。若超出額度或重複記錄，系統會要求你明確確認並填寫原因（不會自動幫你改金額）。
- 每個台彩機會都有記錄入口：可操作且額度有效的是「記錄下注」，其他狀態（無正 EV、受阻、額度重算中）是「我已下注」，送出後由伺服器依最新狀態判斷合規，不符合策略時會先要求確認。
- 填到一半被打斷（切去台彩 App、分頁被系統回收）時，草稿只存在這台裝置；回來後頁首與手機底部列會出現「繼續記錄」。重送使用同一個請求編號，不會重複建立注單。
- 網址會帶 `?date=`，重新整理後仍停在同一個 betting day；背景每分鐘更新與切回分頁時更新，都會保留展開的明細與捲動位置。
- 手機上導覽在畫面底部；決策中心另有底部列，顯示 Python 物化的當日剩餘額度與「下一個可操作」。
- 頁面資料若顯示「過期（stale）」，代表排程器超過 10 分鐘沒有更新，請先檢查系統狀態。

### 4.4 資金 `/bankroll`（需登入）

手動維護的**策略資金**，不連結銀行或台彩帳戶。

- 第一次使用先建立 **初始資金（initial funding）**；之後可新增 **存入 / 提出 / 手動調整 / 沖銷**。
- 所有異動都是 append-only（只增不改），錯誤請用「沖銷」修正。
- 每個 betting day 開始時會凍結 **day-start bankroll**，當天的風控額度都以它為分母。
- 頁面也會顯示每日資金快照與你的實際投注績效。
- 提出、手動調整、沖銷送出前會先顯示金額讓你確認；成功訊息會保留到下一次操作。

### 4.5 投注紀錄 `/bets`（需登入）

- 可直接新增紀錄：比賽、玩法、選項、盤口線、**台彩實際賠率**、金額、下注時間、來源、備註。賠率務必填實際值，績效才準確。
- 方向選項會依玩法過濾（讓分 / 獨贏只有主客隊，大小分只有大小），需要盤線的玩法才顯示盤線欄。預設選第一場未開賽的比賽；已開賽的比賽仍可選（補記早先的注），但會標示。
- 手動改結算結果、作廢、更正都會先開確認視窗並顯示是哪一筆；更正在視窗內填新的賠率 / 金額 / 原因。
- 比賽結束後由排程**自動結算**；必要時也可手動調整結果。
- **作廢（void）**：保留紀錄並標記作廢；**更正（correction）**：建立新紀錄取代舊紀錄，舊紀錄標為 superseded。兩者都需要填寫原因，紀錄永不刪除。
- 更正另需明確確認：現有摘要未提供替代注單的重新驗證額度，因此更正後標示 `missing_context`，不沿用原本的「合規」。實際 exposure 由 Python 下一輪重新計算。
- 勾選「顯示已作廢 / 已更正」可查看歷史版本。

### 4.6 傷病中心 `/injuries`

依球隊分組的官方傷病申報，**主力缺陣**的球隊排在前面。可切換今日 / 明日或任選日期。NBA 官方於賽前一日當地 17:00 前申報，賽前會多次更新。

球員姓名與「主力」標籤保持同一行；手機或窄卡片可在各隊名單內左右捲動，完整姓名不截斷。

### 4.7 回測 / 績效 `/performance`

- **模型回測績效**：歷史準確率、ATS、大小分、上半場命中率、Log Loss / Brier。
- **個人實際投注損益**：以你記錄的台彩實際賠率計算的損益曲線。

2026-10-29 已完成本機的「本季實際預測」與獨立影子實驗區塊，需套用 0014 並部署後才會在線上顯示。影子校正不改正式模型或風控；啟用方式與驗證邊界見 [影子實驗說明](docs/model-shadow-2026-10-29.md)。近期 68 場檢查見 [模型檢查報告](docs/model-review-2026-10-29.md)。

### 4.8 系統狀態 `/status`

各資料來源（賽程、比分、傷病、盤口、定價、決策物化等）的最後更新時間與健康度，每分鐘自動刷新。休賽期或當天沒有比賽時，許多項目顯示「等待中 / idle」屬正常。

狀態頁分開顯示真正的「資料過期」、執行警告、台彩需匯入盤口、備援待命、歷史回填及未使用來源。備援只有在其他新鮮來源較晚成功時才判讀為待命，原始警告仍保留；不代表備援已重新測試成功。裁判來源在台灣 10:37 至 20:07 之間沒有輪詢，會顯示等待排程及下次時間；若先前應執行的時段已漏跑，仍會告警。歷史回填警告另外計數，不混入即時來源整體狀態。

2026-10-28 的排版修正、六筆提示診斷與免 HAR 可行性評估見 [本次修正紀錄](docs/ui-status-fixes-2026-10-28.md)。

## 5. 每日使用流程

以「明天有比賽」為例：

1. **前一天中午後**：開 `/` 看明日賽事與模型預測（排程每天 12:00 同步賽程、12:20 產生 early 預測）。
2. **開賽前數小時**：看 `/injuries` 確認主力缺陣；傷病有更新時模型會自動重算。
3. **開賽前約 1 小時**：模型產生 final 預測。在台彩頁面[匯出 HAR 並匯入](#6-匯入台彩盤口har)，取得最新台彩盤口。
4. **幾分鐘內**：開 `/decision`，看 ACTIONABLE 機會與剩餘額度。
5. **自行決定**是否在台彩下注；下注後回 `/decision` 按「記錄下注」（或到 `/bets` 新增）。
6. **比賽結束後**：系統自動結算，到 `/performance`、`/bankroll` 查看結果。
7. **每天一次**：看 `/status`（或執行 `run_production_check.py`）確認系統健康。

> 沒有匯入台彩 HAR 時，定價、注碼與決策中心會是空的或過期——這是安全的預期行為，不是錯誤。

## 6. 匯入台彩盤口（HAR）

可先用已部署的[免費 Chrome 同步助手](docs/browser-sync-guide.md)，省去 HAR。以下保留手動 HAR 備援：台彩網站以 Cloudflare 防護，本專案**不繞過**，可用一般瀏覽器手動匯出：

1. 用一般 Chrome 打開台彩 NBA 頁面 → 開啟 DevTools → **Network** 分頁 → 等 NBA 盤口載入。
2. 在單場詳情開啟全場與上半場玩法，等回應完成後選 **Export HAR (sanitized)**，存到 `pipeline/captures/`（已被 git 忽略）。需保留回應內容，不需要匯出 Cookie / Authorization。
3. 完全離線檢查（不讀 DB / `.env`；逐場列出玩法與 T-60 擷取時間窗）：
   ```bash
   cd pipeline
   .venv/bin/python run_twsport_har.py captures/<檔名>.har --inspect
   ```
4. 對照預定 DB 做 dry-run（會讀 DB，不寫入）：
   ```bash
   .venv/bin/python run_twsport_har.py captures/<檔名>.har
   ```
5. 確認目標 DB 與對戰比對後匯入，並刪除 HAR：
   ```bash
   .venv/bin/python run_twsport_har.py captures/<檔名>.har --import --delete-after
   ```

注意：盤口時間以 HAR 實際擷取時間為準；超過 2 小時預設拒絕匯入，但 **T-60 策略還要求擷取在開賽前 120～60 分鐘，且盡快匯入、建議在 T-60 前完成**。`--inspect` 成功只代表可解析，不代表 actionable。詳見 [資料來源處理方案](docs/data-source-access-2026-10-15.md) 與 [production-runbook.md](docs/production-runbook.md) §7。

2026-10-28 已依免費需求實作 **Chrome 同步助手**：一般桌面 Chrome 正常開啟台彩 NBA → 決策中心按「同步台彩盤口」→ Python 驗證匯入並更新決策，不必匯出 HAR、不需付費資料 API。一次性安裝、設定與限制見[同步指南](docs/browser-sync-guide.md)。**0013、正式網站與 Railway 已完成部署啟用**；使用者需在桌面 Chrome 載入助手，第一次真實台彩同步仍待實測。HAR 維持備援，未採用付費 JBot。

## 7. 名詞解釋

| 名詞 | 說明 |
|---|---|
| 隱含機率（implied probability） | `1 / 賠率`，包含莊家抽水 |
| 抽水（overround / vig） | 同一盤口所有選項隱含機率總和超過 100% 的部分 |
| 去水公允機率（fair / no-vig probability） | 把抽水按比例移除後的市場機率 |
| Edge | `模型機率 − 去水公允機率`，只作診斷，不決定下注 |
| EV（期望值） | 每下 1 元的期望獲利：`P(贏)×(賠率−1) − P(輸)`，push 退本金 |
| Push | 整數盤口剛好打平，退回本金 |
| Kelly | 依 EV 與賠率計算的理論下注比例；本平台使用 **¼ Kelly** |
| risk-v1 | 凍結的風控規則：¼ Kelly、單筆 ≤ 2%、同場合計 ≤ 3%、單日合計 ≤ 8%（以 day-start bankroll 計） |
| Exposure | 已下注（或理論上將下注）金額佔資金的比例 |
| Betting day | 以開賽的台灣日期劃分的下注日 |
| T-60 | 開賽前 60 分鐘，paper strategy 的決策時點 |
| Paper strategy / paper ledger | 平台依規則在 T-60 自動做的「紙上下注」紀錄，用來前瞻驗證策略，與你的實際下注分開 |
| Strategy bankroll | 你手動維護的策略資金，風控額度的計算基礎 |
| Stale | 資料過期（例如盤口太久沒更新），不可操作 |

## 8. 系統狀態與維運

在 `pipeline/` 下執行（皆為唯讀，不會印出任何密鑰）：

```bash
cd pipeline && .venv/bin/python run_production_check.py
```

| 狀態 | 意義 | 處理 |
|---|---|---|
| `PASS` | 正常 | 無 |
| `WAITING` | 沒有比賽、尚無盤口等，或排程閒置略過 | 無 |
| `BLOCKED` twsport | 台彩自動擷取已停用 / 被擋（預期） | 要下注時手動匯入 HAR |
| `WARN` | Odds API 額度偏低、非關鍵資料過期 | 檢查 Railway 日誌 |
| `FAIL` DB unreachable | Supabase 被暫停或連線字串錯誤 | 到 Supabase Dashboard Restore |
| `FAIL` artifact / 心跳過期 | 模型無法載入或排程器停止 | 依 runbook §8 回滾或重新部署 |

完整說明見 [production-runbook.md](docs/production-runbook.md) §4–§5。

### 8.1 Telegram 推播（選用）

設定後排程器會主動通知你，不用一直開網站：

| 推播 | 時間（台灣） | 內容 |
|---|---|---|
| ACTIONABLE 機會 | 決策中心每分鐘更新後 | 新出現的台彩可操作機會：玩法、選項、賠率、EV、理論金額與剩餘額度。同一機會只通知一次 |
| 每日賽事摘要 | 每天 12:40 | 明日賽事、模型勝率、預測分差與總分 |
| 系統異常告警 | 每 15 分鐘檢查 | 健康檢查出現 FAIL（排程停擺、資料庫連不上、模型無法載入等）與恢復通知；持續異常每 6 小時提醒 |

設定步驟：

1. 在 Telegram 找 **@BotFather** → `/newbot` 建立 bot，取得 token。
2. 傳任一訊息給你的 bot，再開 `https://api.telegram.org/bot<token>/getUpdates`，找到 `"chat":{"id": ...}` 就是 chat id。
3. 在 Railway 設定 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，並對資料庫執行 `npm run db:migrate:pg`（套用 0009）。
4. 本機測試：`cd pipeline && .venv/bin/python run_notify.py --test`，收到測試訊息即完成。

注意：

- 推播內容都是決策中心已經算好的結果，**不是投注建議**，下注前仍請到網站確認盤口是否仍有效。
- 開季初期大多數機會會因資料品質旗標（例如 `season_opener`）被歸在 **REVIEW**，預設不推播；想一起收到請設定 `TELEGRAM_NOTIFY_INCLUDE_REVIEW=true`。
- 排程器本身完全停機時無法自己發出告警。
- 細節見 [production-runbook.md](docs/production-runbook.md) §4c。

## 9. 本機開發與測試

### 9.1 環境需求

- Node.js（本機測試建議 24；`test:decision` 使用內建 `node:sqlite`）、Python 3.11（`pipeline/`）
- 環境變數範本：[.env.example](.env.example)。網站用 `.dev.vars`、Python 用 `pipeline/.env`，**都不可提交進 git**。

### 9.2 網站（使用本機 D1 測試資料）

```bash
npm install
npm run build              # 先 build
npm run db:reset:local     # 建立本機 D1 schema 並灌入 seed 測試資料
npm run dev:sandbox        # 啟動 http://localhost:3000
curl http://localhost:3000/healthz
```

- `.dev.vars` 若設有 `DATABASE_URL`，網站會改連 Postgres。測試請使用獨立暫存工作目錄的 Wrangler 設定與 state，排除正式連線設定，並確認 `/api/system/status` 的 `db_driver = d1` 後才灌 seed 或跑 smoke；保留原本的密鑰檔。
- `ecosystem.config.cjs` 是早期沙盒的 pm2 設定，可忽略。

### 9.3 Python pipeline

```bash
cd pipeline
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -m "not db"          # 純邏輯測試
pytest                      # 含 DB 整合測試（使用暫存 schema，需 DATABASE_URL，約 17 分鐘）
```

macOS 跑研究用評估（XGBoost）需 OpenMP：`brew install libomp`，見 [dev-pitfalls.md](docs/dev-pitfalls.md) §9。

### 9.4 測試

| 指令 | 內容 | 最近結果（2026-10-15） |
|---|---|---|
| `npm run test:api` | API smoke test（需服務已啟動，且使用本機 D1 seed） | 162 passed |
| `npm run test:auth` | 登入 / 權限防護 | 14 passed |
| `npm run test:status` | 來源角色、排程休息、過期與警告分類（不連 DB） | 18 passed（2026-10-28） |
| `npm run test:twsport-sync` | Chrome 同步權限、佇列、併發、白名單、collector 與前端重送恢復 | 29 passed（fixture／mock、斷線恢復、壓縮、進度與結果區分；2026-10-29） |
| `npm run test:decision` | D.5 更正／出金回歸（記憶體 SQLite + Hono，不連網） | 19 passed |
| `pytest`（pipeline） | Python 純邏輯 + DB 整合 | 上次完整稽核 741 passed；Phase E.1 後純邏輯 618 passed、推播 DB 7 passed |

`test:api` 依賴 seed 測試資料，對正式資料庫執行會出現「games=0」類失敗，屬預期。

## 10. 部署

| 元件 | 平台 | 部署方式 |
|---|---|---|
| 網站 | Cloudflare Pages（專案 `webapp`） | `npm run deploy:prod`；secrets：`DATABASE_URL`、`SESSION_SECRET` |
| 排程器 | Railway（服務 `nba-scheduler`，Root `/pipeline`） | GitHub `main` 自動部署；volume 掛 `/data/model-artifacts`；**只能 1 個 replica** |
| 資料庫 | Supabase Postgres | 使用 **Session pooler（port 5432）**；migration：`npm run db:migrate:pg` |

完整步驟、環境變數與部署後檢查見 [production-runbook.md](docs/production-runbook.md)；平台選型評估見 [DEPLOY.md](DEPLOY.md)。

## 11. 指令速查

網站（專案根目錄）：

| 指令 | 用途 |
|---|---|
| `npm run build` / `npm run dev:sandbox` | 建置 / 本機啟動 |
| `npm run deploy:prod` | 部署到 Cloudflare Pages |
| `npm run db:reset:local` | 重置本機 D1 並灌入 seed |
| `npm run db:migrate:pg` | 對 `DATABASE_URL` 套用 Postgres migrations |
| `npm run test:api` / `npm run test:auth` | 測試 |

Pipeline（`cd pipeline`，以 `.venv/bin/python` 執行）：

| 指令 | 用途 |
|---|---|
| `run_production_check.py [--post-deploy] [--json]` | 系統健康檢查（唯讀） |
| `run_data_audit.py` | 帳號與資料稽核（唯讀） |
| `run_security_check.py` | Supabase RLS、Data API 有效權限與角色拒絕測試（唯讀） |
| `scheduler.py --print-next-runs` | 各排程下次執行時間 |
| `run_twsport_har.py <file> [--import --delete-after]` | 台彩 HAR 驗證 / 匯入 |
| `run_twsport_har.py <file> --inspect` | 完全離線解析、缺少玩法及逐場 T-60 時點檢查 |
| `run_source_check.py [--game-id <已知歷史場次>]` | 免費 NBA CDN / 官方裁判頁唯讀檢查，不讀 DB |
| `run_referees.py [--dry-run] [--force]` | 抓官方裁判分派並比對賽事；`--dry-run` 不寫入 |
| `run_predict.py --kind early\|final\|refresh [--dry-run]` | 手動產生預測 |
| `run_retrain.py [--list \| --rollback [--to <版本>]]` | 重訓 / 版本清單 / 回滾模型 |
| `run_odds.py --source oddsapi [--dry-run]` | 手動抓國際盤 |
| `run_pricing.py --dry-run` / `run_sizing.py --dry-run` | 檢視定價 / 注碼（不寫入） |
| `run_decision.py --dry-run --summary` | 檢視決策中心計算（不寫入） |
| `run_paper.py --report` | 前瞻 paper strategy 績效 |
| `run_notify.py --test` / `run_notify.py --job actionable\|daily\|system --dry-run` | Telegram 測試訊息 / 預覽會推播的內容（不送出） |

各指令的詳細用法見 [stage2-phase-summary.md](docs/stage2-phase-summary.md)。

## 12. 專案結構

```
├── src/                  網站（Hono）
│   ├── routes/           API：api.ts、decision.ts（bets / bankroll / 決策）、auth.ts
│   ├── db/               DB adapter（Postgres / D1 自動切換）與查詢
│   ├── lib/              驗證、時間換算、顯示用格式化
│   └── pages/            頁面殼子（資料由前端 JS 呼叫 API 取得）
├── public/static/js/     各頁面前端腳本
├── pipeline/             Python 資料 / 模型 / 決策引擎
│   ├── core/sources/     NBA CDN、stats.nba.com、ESPN、傷病擷取
│   ├── core/models/      特徵工程、Elo、模型
│   ├── core/production/  推論、重訓、artifact、預測分佈、readiness
│   ├── core/odds/        盤口擷取與正規化
│   ├── core/pricing/     去水、edge、EV
│   ├── core/sizing/      Kelly、risk-v1
│   ├── core/execution/   paper strategy、結算
│   ├── core/decision/    決策中心、實際下注 exposure、bankroll
│   ├── core/notify/      Telegram 推播（Phase E.1）
│   ├── core/scheduling.py  19 個排程 job 定義
│   ├── scheduler.py      排程器入口
│   └── run_*.py          各項手動指令
├── migrations/postgres/  正式 schema（0001–0011，權威版）
├── migrations/d1/        本機 D1 schema
├── seed/                 本機測試資料樣板
├── scripts/              migration、seed、測試腳本
└── docs/                 維運手冊與開發報告
```

## 13. 目前狀態與已知限制

**現況（2026-10-21）**

- Phase A ~ D.5、E.1 已完成並部署：正式資料庫 migrations 0001–0010 已套用，Railway 排程器與 Cloudflare 網站皆為 `e30c824` 之後的版本（2026-10-15 確認）。
- 2026-27 例行賽已開打（台灣 10/21 03:00）。10/21 唯讀檢查：3 場已結束、ml-v2 預測 21 筆、國際盤快照 634 筆、每日摘要已送出 2 則。
- **台彩盤口仍為 0 筆**（尚未匯入 HAR），所以 paper decision 全是 `no_bet / no_odds`、決策中心沒有可操作機會；完整的台彩流程仍待第一次 HAR 匯入後驗證。
- D.5 更正 / 出金修正已隨網站部署（[phase-d5-fixes-2026-10-15.md](docs/phase-d5-fixes-2026-10-15.md)）；Supabase Data API 已鎖定（0010，見 runbook §3a）。

**Phase E 進度**

- **E.1 Telegram 推播**：已上線（[phase-e1-telegram-report.md](docs/phase-e1-telegram-report.md)）。
- **E.2 賽前裁判分派收集**：本機完成，**尚未部署**；需套用 `0011_phase_e2_referees.sql`。只累積資料，不接入模型（[phase-e2-referees-report.md](docs/phase-e2-referees-report.md)）。
- 尚未開始：旅行距離特徵（可做）、line movement（需累積數月盤口）、五人 lineup / on-off（大型研究）。來源可行性見 [data-source-access-2026-10-15.md](docs/data-source-access-2026-10-15.md)。

**使用限制**

- 免費 Chrome 同步已正式部署；須先安裝助手，首次真實台彩擷取仍待使用者實測。HAR 保留備援，國際盤只作診斷參考。
- 模型預測分佈中心 μ = 0 是評測後的修正，2026-27 賽季才是真正的前瞻驗證期。
- 沒有歷史投注獲利證據；歷史模型準確率不等於能贏盤口。
- Supabase 免費專案閒置會被暫停，排程器長時間停機後需手動 Restore。
- 登入使用 PBKDF2（100,000 次），在 Cloudflare 免費方案可能偶發 Error 1102，必要時升級 Workers Paid。

## 14. 延伸文件

| 文件 | 內容 |
|---|---|
| [docs/README.md](docs/README.md) | 全部文件索引 |
| [docs/production-runbook.md](docs/production-runbook.md) | 正式環境操作手冊 |
| [docs/project-audit-2026-10-15.md](docs/project-audit-2026-10-15.md) | 最新全專案稽核 |
| [AGENTS.md](AGENTS.md) | 跨模型共用的專案工作指令 |
| [docs/agent-instructions-rationale.md](docs/agent-instructions-rationale.md) | OpenAI／Anthropic 官方指南評估與來源 |
| [docs/phase-d5-fixes-2026-10-15.md](docs/phase-d5-fixes-2026-10-15.md) | D.5 更正與出金缺口修正、驗收與後續推進 |
| [docs/stage1-website-report.md](docs/stage1-website-report.md) | 階段一網站開發報告 |
| [docs/stage2-phase-summary.md](docs/stage2-phase-summary.md) | 階段二各 Phase 摘要與指令 |
| [docs/phase-e1-telegram-report.md](docs/phase-e1-telegram-report.md) | Phase E.1 Telegram 推播設計與驗證 |
| [docs/dev-pitfalls.md](docs/dev-pitfalls.md) | 開發踩坑紀錄 |
| [NBA預測平台規格書_兩階段.md](NBA預測平台規格書_兩階段.md) | 原始規格書 |
