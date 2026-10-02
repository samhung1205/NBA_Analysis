# NBA 對戰預測平台 — 階段一（網站殼子）

## 專案概述

- **名稱**：NBA 對戰預測平台
- **目標**：每日分析隔天 NBA 對戰，輸出「全場勝負、讓分、大小分、上/下半場表現」預測與信心度，並與台灣運彩盤口比對，輔助個人投注決策。
- **程式碼倉庫**：https://github.com/samhung1205/NBA_Analysis
- **本階段範圍**：規格書 v2.0 **階段一** — 用 Hono + Cloudflare Pages 完成可登入、能讀寫資料庫、UI 齊全的網站骨架。資料為 seed 測試資料，但**讀取路徑全部走真實 API + 資料庫**。
- **階段二**（進行中，見文末「階段二（pipeline/）狀態」）：Python 資料擷取、排程與 ML 預測引擎；Phase A~C 已實作，Phase D（盤口）起尚未。

## 目前完成的功能

### 前端頁面（§3.4）
| 頁面 | 路徑 | 內容 |
|---|---|---|
| 賽事總覽 | `/` | 每場卡片：對戰、台灣時間、模型勝率、預測分差/總分、上半場預測、台彩 vs 國際盤 vs 模型、Edge 標示；可切換 今日/明日/後天 或任選日期 |
| 單場詳情 | `/games/:id` | 盤口價值分析（含去抽水公允機率、¼Kelly）、逐節/半場表格、**特徵拆解（為什麼這樣預測）**、盤口變動折線圖、本場傷病、球隊數據、雙方近況、H2H |
| 傷病中心 | `/injuries` | 依球隊分組的官方傷病申報 + **主力缺陣警示**（警示球隊排序在前）；可切換今日/明日 |
| 回測 / 績效 | `/performance` | 模型歷史準確率、ATS、大小分、上半場命中率、Log Loss/Brier、模擬 ROI；個人實際投注損益曲線 |
| 投注紀錄 | `/bets` | 個人下單 CRUD、手動結算、命中率/損益/ROI 統計 |
| 系統狀態 | `/status` | 各資料來源最後更新時間、新鮮度、失敗告警；每分鐘自動刷新 |
| 登入 / 註冊 | `/login` | email + password |

所有頁面皆常駐顯示**免責聲明**與台彩抽水提醒（規格書 §6）。

### API 路由（§3.3）
| # | 端點 | 說明 |
|---|---|---|
| 5 | `GET /api/games/tomorrow` | 隔日（台灣時間）賽事 + 最新 predictions/odds + edge 分析 |
| — | `GET /api/games/today` | 今日賽事 |
| — | `GET /api/games?date=YYYY-MM-DD` | 任意台灣日期（格式錯誤回 400） |
| 6 | `GET /api/games/:id` | 單場詳情：box score、H2H、近況、特徵拆解、盤口歷史、傷病 |
| 7 | `GET /api/injuries/today?date=` | 當日各隊傷病報告（含主力缺陣警示） |
| 8 | `GET /api/predictions/:gameId` | 該場最新預測 |
| 9 | `GET /api/odds/:gameId` | 盤口快照歷史 + 已分組時間序列（供折線圖） |
| 10 | `GET /api/bets`、`POST /api/bets`、`PATCH /api/bets/:id`、`DELETE /api/bets/:id` | 個人下單紀錄 CRUD（**需登入**） |
| 11 | `GET /api/system/status` | 各資料來源最後更新時間與健康度 |
| — | `GET /api/metrics` | 模型回測績效 |
| — | `GET /api/teams` | 球隊清單 |
| — | `POST /api/auth/register`、`/login`、`/logout`、`GET /api/auth/me` | 驗證機制（§3.2） |
| — | `GET /healthz` | 健康檢查（階段二排程器可用） |

### 驗收測試
```bash
npm run test:api        # 90 項檢查，需服務已啟動
```
> ⚠️ 這組檢查依賴 **seed 測試資料**（明日 3 場、今日進行中等）。正式 Supabase 已回填真實賽事、seed 賽事已清除，
> 對它執行會有「games=0」類失敗，屬預期。請對本機 D1 seed 執行（`npm run db:reset:local`，並暫時不要讓 `.dev.vars`
> 的 `DATABASE_URL` 生效，例如啟動時改用 `--binding SESSION_SECRET=...`）。
涵蓋：6 個頁面渲染、全部 API 契約、edge/Kelly 計算、抽水 > 0、授權保護（401）、
bets 寫入→讀回→結算→刪除、ROI 以台彩實際賠率計算、404/400 錯誤處理。
**目前結果：90 通過 / 0 失敗。**

## 資料架構

- **Schema**（12 張表）：`teams` `players` `games` `team_game_stats` `player_game_stats`
  `injuries` `odds_snapshots` `predictions` `bets` `users` + 額外新增
  `data_sources`（資料來源健康度，供系統狀態頁）、`model_metrics`（回測績效，供績效頁）
- **Migration**：`migrations/postgres/0001_init.sql`（正式，權威版）與 `migrations/d1/0001_init.sql`（開發期）欄位語意一致
- **半場欄位**：`games.home_h1/home_h2/away_h1/away_h2` 與 `predictions.pred_home_h1...` 已就緒，供階段二半場模型使用
- **時區**：DB 一律存 UTC ISO8601，台灣時間換算集中在 `src/lib/time.ts`

### ⚠️ 資料庫選型（規格書 §0 銜接關鍵）

規格書要求「階段一、階段二都連得到的獨立 Postgres」。本專案實作了 **DB adapter 層**（`src/db/index.ts`），依環境變數自動切換驅動：

| 環境 | 條件 | 驅動 |
|---|---|---|
| **正式**（階段一 + 階段二共用） | 已設 `DATABASE_URL` | **Postgres**（Supabase / Railway） |
| 沙盒開發期 | 未設 `DATABASE_URL`，有 D1 binding | D1 (local SQLite) |

業務程式碼只依賴統一介面，SQL 一律用 `?` 佔位符（Postgres adapter 自動轉 `$1..$n`）。
**切換資料庫不需修改任何一行程式碼。**

> ⚠️ D1 僅供沙盒開發便利之用 —— 階段二的 Python 服務**無法**連線 D1。
> 正式部署前務必依下方步驟切到 Postgres。

### 切換到正式 Postgres（階段二開工前必做）✅ 已完成並驗證

```bash
# 1. 在 Supabase 建專案，取得連線字串
#    ⚠️ 用 Session pooler（pooler host + port 5432），不要用 Transaction pooler（port 6543）
#       ——見下方「⚠️ 連線模式踩坑記錄」

# 2. 建立 schema
DATABASE_URL="postgresql://..." npm run db:migrate:pg

# 3. （選用）灌入測試資料
DATABASE_URL="postgresql://..." npm run db:seed:pg

# 4. 設定 Cloudflare secret
npx wrangler pages secret put DATABASE_URL
npx wrangler pages secret put SESSION_SECRET   # openssl rand -base64 32
```

**狀態**：Supabase Postgres 已建立、migration 已套用、90 項 smoke test 已在真實 Postgres + 真並行流量下全數通過。`.dev.vars` 存有本機開發用連線字串（已 gitignore，不在 repo 中）。

#### ⚠️ 連線模式踩坑記錄（2026-08-01）

規格書原先建議 Workers 用 Supabase 的 **Transaction pooler（port 6543）**。實測發現這個組合在 Cloudflare Workers runtime 下會**間歇性卡死請求**：

```
GET /api/games/tomorrow  → 200 OK (2s)
GET /api/games/today     → 500，僅 10ms 就被判定「Worker's code had hung」
```

根因是兩層問題疊加：
1. **`src/db/index.ts` 原本把 pg 連線做成跨請求共用的模組級單例** —— Cloudflare Workers 禁止一個請求沿用另一個請求開啟的 socket I/O，跨請求重用連線會被 runtime 判定為掛起。已修正為**每個請求建立獨立連線**（`max: 1`，不快取）。
2. **Transaction pooler 本身與 `postgres.js` 的連線管線化（pipelining）假設衝突**，即使修正上述單例問題後仍會間歇卡死；換成 **Session pooler（同一 pooler host，port 5432）** 後在多輪並行壓測（8 端點 × 5 輪同時發送）與完整 90 項 smoke test 下皆穩定通過。

現行修正的代價是每次請求多一次 TCP+TLS 連線開銷（約 150–300ms），對個人使用的低流量網站可接受。**若未來要正式上線給多人使用**，建議改用 [Cloudflare Hyperdrive](https://developers.cloudflare.com/hyperdrive/)（官方為 Workers + 外部 Postgres 設計的邊緣連線代理），可同時解決連線延遲與這個限制，但需要另外用 `wrangler hyperdrive create` 建立資源。

### Seed 測試資料
單一份樣板 `seed/seed.template.sql`，由 `scripts/render-seed.mjs` 將時間 token
（`{{NOW:-30m}}`、`{{TPE:+1 08:00}}`）展開為固定 ISO8601 UTC 字串後產生純標準 SQL，
**同一份檔案可同時餵給 D1 與 Postgres**，不需維護兩份或跨方言翻譯。

內容：10 支球隊、16 名球員、17 場比賽（明日 3 場含預測與盤口、今日 1 場進行中 + 1 場已結束、12 場歷史）、
4 筆預測（含 `features_json` 的 contributions）、28 筆盤口快照（台彩多時間點 + 國際盤對照）、
9 筆傷病申報、8 個資料來源狀態、2 筆回測績效。

**測試帳號**：`demo@example.com` / `nba12345678`（⚠️ 正式環境請刪除）

## 使用者指南

1. 進入 `/`，預設顯示**明日（台灣時間）**賽事 —— 這是本平台的主要用途
2. 每張卡片可看模型勝率、預測分差/總分、上半場預測，以及台彩 vs 國際盤 vs 模型的 Edge 標示（🔥 = edge 較大）
3. 點「查看詳情」看特徵拆解（為什麼這樣預測）、盤口變動折線圖
4. 到 `/login` 註冊/登入後，可在 `/bets` 記錄實際投注（**賠率請填台彩實際賠率**），並在 `/performance` 看損益曲線
5. `/status` 確認各資料來源新鮮度；階段二排程上線後此頁會顯示真實狀態

## 本機開發

```bash
npm run build              # 必須先 build
npm run db:migrate:local   # 建立 local D1 schema
npm run db:seed:local      # 渲染並灌入測試資料
pm2 start ecosystem.config.cjs
curl http://localhost:3000/healthz
npm run test:api           # 驗收測試

npm run db:reset:local     # 重置本機資料庫
```

## 部署

- **平台**：Cloudflare Pages
- **狀態**：Supabase Postgres 已就緒並通過驗收；尚未執行 `wrangler pages deploy`
- **技術棧**：Hono + TypeScript + TailwindCSS(CDN) + Chart.js(CDN) + Cloudflare Pages
- **最後更新**：2026-10-02

## 階段二進度

- ✅ **Phase A** 資料基礎：fetcher、排程器、回填（C.5A 另做了來源 fallback / 排程 / 傷病硬化）
- ✅ **Phase B** Baseline：特徵工程、Elo walk-forward 回測
- ✅ **Phase C.5B** 歷史資料完整度：五季 box score / 衍生進階指標 / 歷史傷病快照 / 賽前特徵底座（見下方「Phase C.5B」）
- ✅ **Phase C.5C** 時間尺度特徵 + walk-forward 重新評估：勝負與分差/總分/上半場**全部**優於 Elo / baseline（評估完成，尚未上線；見下方「Phase C.5C」）
- 🟡 **Phase C** ML：線上仍是 ml-v1.0（只有勝負達標）；C.5C 的新模型留待 C.5D 上線

### 尚未實作

- **Phase D** 盤口與價值：台灣運彩 Playwright 爬蟲、The Odds API 整合、Edge/Kelly 精算、ROI 回測
- **Phase E** 強化（選做）：球員層級模型、line movement 特徵、Telegram/LINE 推播

### 階段一已為階段二預留的接口

| 階段二只要寫入… | 階段一就會自動顯示於… |
|---|---|
| `games`（含逐節/半場欄位） | 賽事總覽、詳情頁逐節表 |
| `predictions`（含 `features_json.contributions`） | 總覽卡片、詳情頁特徵拆解 |
| `odds_snapshots`（`source='twsport'` / `'oddsapi'`） | 盤口比較表、Edge 標示、變動折線圖 |
| `injuries`（最新狀態；球員從報告消失 → 補 `Available`） | 傷病中心、主力缺陣警示、詳情頁 |
| `data_sources` | 系統狀態頁（含 warn/error 告警） |
| `model_metrics` | 回測績效頁 |

**階段二不需修改階段一的任何 API 或前端程式碼。**

### 建議下一步

1. 部署 Cloudflare Pages 並驗證線上環境（Supabase Postgres 已就緒）
2. 將 `pipeline/scheduler.py` 部署到未被 stats.nba.com 封鎖的主機（Railway/Fly；Dockerfile 已含 Java）
3. Phase C.5D（C.5C 模型上線：production inference）→ 再進 Phase D

## 待與使用者確認的事項

1. **Edge 精算範圍**：目前讓分/大小分的 edge 以「模型值 vs 盤口線的落差」呈現（`line_gap`），
   標記為 `note: 'edge 需階段二模型輸出分佈後精算'`。真正的機率型 edge 需階段二模型輸出
   分差/總分的**機率分佈**（而非單點預測）才能計算 —— 屬 Phase C/D 範圍。獨贏(ML)的 edge 已為真實機率計算。
2. **球隊中文名**：目前 seed 用常見譯名，若你有偏好的譯名（如「塞爾提克」vs「凱爾特人」）可調整
3. **正式部署與 Hyperdrive**：目前修正（每請求獨立連線）已驗證穩定可用；若之後要正式對外開放給多人使用，
   建議評估改用 Cloudflare Hyperdrive 以消除連線延遲，見上方「連線模式踩坑記錄」

## 階段二（pipeline/）狀態與已知問題

- **程式**：`pipeline/`（Python）— nba_api / ESPN / nbainjuries fetcher、Elo walk-forward 回測、APScheduler、Dockerfile；`migrations/postgres/0002_phase2.sql` 新增 `elo_ratings`。
- **回填**：C.5B 起歷史 box score 走 `python -m core.jobs.box_backfill`（NBA CDN，不需 stats.nba.com）；舊的 `python run_backfill.py`（stats.nba.com，含官方進階數據）保留但在被封鎖的 IP 上不可用；`python run_backfill_espn.py`（ESPN 備援，僅賽程/比分/逐節，足以做 Elo 回測與半場模型）。皆為 UPSERT，可中斷重跑；ESPN 暫存的 `espn:<id>` 賽事會在 NBA 官方回填時自動認領改寫。回測：`python run_backtest.py --eval-seasons 2`。

### 踩坑記錄（2026-08 ~ 10）

1. **`stats.nba.com/stats/*` 對部分 IP 會被 Akamai 靜默丟棄請求**：DNS/TCP/TLS 皆正常（~30ms），但 HTTP 請求零回應位元組（curl `ttfb=0`，IPv4/IPv6 皆同），而站台根路徑、`www.nba.com` 正常。非程式問題，換 IP（雲端主機/VPN/熱點）或改用 ESPN 備援。排查方式：`curl -4 -m 15 -w "%{time_connect} %{time_appconnect} %{time_starttransfer}\n" -o /dev/null -H "Referer: https://www.nba.com/" "https://stats.nba.com/stats/scoreboardv3?GameDate=2025-02-01&LeagueID=00"`。
2. **ESPN**：不可偽造瀏覽器 UA（`Mozilla/5.0` 會 403），用 requests 預設 UA；scoreboard 只接受單日 `dates=YYYYMMDD`。
3. **不要**對 `nba_api` 傳 `headers=` 或覆寫 User-Agent：`headers=` 會整個取代預設標頭，而舊版自訂的 Chrome 120 UA 與套件預設的
   `Sec-Ch-Ua`（Chrome 145）互相矛盾。一律沿用 `NBAStatsHTTP.headers`，也不要自行重建 `requests.Session`。
   `nba_api.live` 端點的預設標頭（Chrome 87）已過期，打 `cdn.nba.com` 會 403；CDN 請求改用 stats 預設標頭（去掉 Host），見 `core/sources/nba_cdn.py`。
4. **Supabase 免費專案閒置會被暫停**：暫停後 pooler 回 `tenant/user not found`、直連主機 DNS 消失，需到 Supabase Dashboard 手動 Restore。
5. 背景執行 Python 請加 `-u`，否則輸出被緩衝，容易誤判為卡住。
6. APScheduler：`add_job(..., next_run_time=None)` 是「建立成暫停」，且 `CronTrigger` 不指定 `timezone` 會用機器本地時區。
   排程現集中在 `core/scheduling.py`（每個 Cron 皆帶 `Asia/Taipei`，有 next-run-time 測試）。
7. `nbainjuries` 的 timestamp 是**美東當地 naive 時間**（非 UTC），2025-12-22 起為 15 分鐘一份；球員名稱格式為 `Last, First`。

### 資料來源與排程（C.5A 後）

| 資料 | 優先序（失敗自動往下） |
|---|---|
| 賽程 / 即時比分 / 逐節 | NBA CDN → stats.nba.com `ScoreboardV3` → ESPN |
| 基本 box score | NBA CDN `boxscore_<id>` → stats.nba.com `BoxScoreTraditionalV3` |
| 進階數據（pace/ORtg/DRtg） | 僅 stats.nba.com；失敗不中止，下次補 |
| 傷病 | `nbainjuries`（官方 PDF） |

每次嘗試與 fallback 狀態寫入 `data_sources`（`nba_cdn` / `nba_api` / `espn` / `nbainjuries`），系統狀態頁可見：被備援取代的來源標 `warn`、勝出的備援註記「備援 <primary>」、全部失敗標 `error`。
排程（台灣時間）：每日 12:00 賽程/比分、每 5 分鐘依 `game_time_utc` 刷新進行中/待結算賽事（無則不打外部來源）、傷病台灣 00:00–10:59 每 15 分 / 11:00–23:59 每 30 分。
`python scheduler.py --print-next-runs` 可查看各 job 下次觸發時間。測試：`cd pipeline && pip install -r requirements-dev.txt && pytest`（DB 測試使用暫存 schema，`-m "not db"` 可只跑純邏輯）。
詳見 [docs/phase-c5a-report.md](docs/phase-c5a-report.md)。

### Phase C 現況（2026-10）

`cd pipeline && python run_train.py`（評測加 `--no-write`）。walk-forward（以賽季為單位），評測 2024-25 + 2025-26 共 2,631 場，
特徵皆為賽前可得：Elo、休息/背靠背、近況、交手、賽程（目前沒有 box score / 傷病，見下方路線圖）。

| 勝負（主隊勝） | accuracy | log loss | Brier |
|---|---|---|---|
| Elo baseline | 0.6697 | 0.6221 | 0.2150 |
| **ml-v1.0（邏輯迴歸）** | **0.6724** | **0.6046** | **0.2088** |

兩個評測賽季單獨看也都優於 Elo。**勝負模型用邏輯迴歸而非 XGBoost**：在驗證賽季（2022-23、2023-24）上所有 XGBoost 設定
（含校準）都輸給 5 特徵邏輯迴歸（log loss 0.638 vs 0.628）；特徵（elo_diff、b2b_diff、margin10_diff、rest_diff、form10_diff）
與 C 值只用評測賽季「之前」的賽季選出，評測賽季只跑一次。accuracy 的優勢（+0.27 pt，約 7 場）不顯著，
log loss / Brier 的進步才是穩的。

**尚未達標**：分差/總分/上半場（XGBoost）MAE 與 baseline 持平（margin 11.39 vs 11.34、total 15.26 vs 15.22、
h1 margin 8.95 vs 8.96、h1 total 9.87 vs 9.89），上半場勝負方向 61.7% vs 60.9%。規格書「ML 全指標優於 Elo」目前只有勝負達成。
預測以 `model_version='ml-v1.0'` 寫入 `predictions`（網站顯示最新一筆）；模型存檔於 `pipeline/artifacts/`（gitignore）。

### Phase C.5B — 歷史資料完整度與特徵底座（2026-10）

五季（2021-22 ~ 2025-26）的基本 box score、衍生進階指標、歷史傷病快照都已回填，並建立「賽前特徵」資料層
（**只建資料層，未重新調模型**）。詳見 [docs/phase-c5b-report.md](docs/phase-c5b-report.md)。

```bash
cd pipeline
python -m core.jobs.box_backfill --workers 6      # 五季 box score（NBA CDN；可中斷續傳；--force 重寫已完整場次）
python -m core.jobs.injury_backfill --shard 1/4   # 歷史傷病報告（可開多個 shard 並行；可續傳）
python -m core.jobs.derive_metrics                # 補算/重算 team_game_derived（公式版本升級時 --force）
python -m core.jobs.coverage_audit                # 覆蓋率稽核；--missing team_stats 列出缺漏場次；--json 輸出
python -m core.jobs.injury_validation             # 傷病狀態 vs 實際出賽（診斷用，不回饋特徵）
python run_build_features.py --offset 0           # 賽前特徵 → pipeline/artifacts/*.csv.gz
```

- **Migration**：`migrations/postgres/0003_phase_c5b.sql`（`npm run db:migrate:pg`）。純新增：`team_game_stats` 補原始計數、
  `team_game_derived`（box-v1 衍生指標）、`injury_reports` / `injury_report_entries`（完整快照）、`source_fetch_log`、`injuries` 唯一約束。
- **傷病語意**：快照 + 涵蓋範圍 → 賽前最後已知狀態（`core/injury_asof.py`）；球員不再被列出 = `Not Listed`（視同可出賽），
  該隊 NOT YET SUBMITTED = 無資訊（往前找較早報告），沒有任何報告涵蓋 = `Unknown`。`injuries`（API 讀取的最新狀態）在球員消失時補一列 `Available`。
- **衍生指標**公式與來源見 `core/metrics.py`；**賽前特徵**定義與洩漏防護見 `core/models/pregame_features.py`。

### Phase C.5C — 時間尺度建模與 walk-forward 重新評估（2026-10）

以「本季這支球隊」為主體重建特徵（pregame-v2：本季 / l20 / l10 / l5、上季先驗 + 經驗收縮、名單延續性、walk-forward 校準的傷病缺陣機率、
分鐘加權可用性），在 2024-25 + 2025-26（2,631 場）只跑一次的 walk-forward 評測。詳見 [docs/phase-c5c-report.md](docs/phase-c5c-report.md)。

| 2,631 場 | Elo / baseline | Phase C | **C.5C 選定模型** |
|---|---|---|---|
| 勝負 log loss / Brier / acc | 0.6221 / 0.2150 / 0.6697 | 0.6046 / 0.2088 / 0.6724 | **0.5922 / 0.2036 / 0.6792** |
| 分差 MAE | 11.34 | 11.39 | **10.98** |
| 總分 MAE | 15.22 | 15.25 | **14.93** |
| 上半場分差 / 總分 MAE | 8.96 / 9.89 | 8.95 / 9.87 | **8.87 / 9.64** |

兩個評測賽季單獨看也都較好。勝負/分差的改善主要來自傷病與球員可用性，總分類來自本季節奏/得失分；上季先驗/名單延續性只有很小的增量；
XGBoost 在驗證賽季全數輸給邏輯迴歸 / Ridge，未採用。`cd pipeline && python -m core.jobs.c5c_evaluate`（只評測，不寫 DB）。

## 路線圖 / 保留待辦

### [下一步] Phase C.5D：C.5C 模型的 production inference

以 C.5C 選定的模型與 pregame-v2 特徵產生實際預測（傷病校準器狀態持久化、定期重訓、開季名單延續性未知的處理），見 C.5C 報告 §14。

### [已完成] Phase C.5C：用新資料層重新評估模型

box score / 傷病的資料層已在 C.5B 完成（見上），尚未用於模型。C.5C 要做的事：
1. 以 `pregame_features.build_pregame_features()` 的特徵重新評估勝負 / 分差 / 總分 / 半場模型（walk-forward，
   評測賽季只跑一次；特徵選擇只用評測賽季之前的資料）。
2. 比較 `decision_offset_min`（0 / 60 / 120）對傷病特徵的影響，選定實際預測流程要用的決策時點。
3. 把傷病/先發特徵接上 `core/jobs/backtest.py`（Elo 傷病調整）與 `predict`。

**驗收**：在 2024-25 + 2025-26 walk-forward 評測上，勝負 accuracy / log loss / Brier 全數優於 Elo，
且分差/總分/上半場 MAE 優於 baseline。需要超越的基準（2,631 場）：
Elo acc 0.6697 / log loss 0.6221 / Brier 0.2150；margin MAE 11.34、total MAE 15.22、h1 margin 8.96、h1 total 9.89。

**環境備註**：macOS 跑 XGBoost 需要 OpenMP：`brew install libomp`（或暫以
`DYLD_FALLBACK_LIBRARY_PATH=pipeline/.venv/lib/python3.11/site-packages/sklearn/.dylibs` 借用 scikit-learn 內建的；
注意 `nohup` 會清掉 DYLD_* 變數）。Docker 映像需加裝 `libgomp1`。
