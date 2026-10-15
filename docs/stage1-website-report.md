# 階段一：網站殼子開發報告（Stage 1 Website）

> 本文由原 `README.md` 的開發紀錄整理而來（2026-10-15 移出）。內容為**當時的開發紀錄**，數字與狀態以標註日期為準；目前狀態請看根目錄 [README.md](../README.md) 與 [project-audit-2026-10-15.md](project-audit-2026-10-15.md)。

## 1. 目標與範圍

- 規格書 v2.0 **階段一**：用 Hono + Cloudflare Pages 完成可登入、能讀寫資料庫、UI 齊全的網站骨架。
- 資料為 seed 測試資料，但**讀取路徑全部走真實 API + 資料庫**；前端程式碼不含任何寫死的假資料（規格書 §0 銜接原則）。
- 技術棧：Hono + TypeScript + TailwindCSS（CDN）+ Chart.js（CDN）+ Cloudflare Pages。
- 程式碼倉庫：https://github.com/samhung1205/NBA_Analysis

## 2. 前端頁面（規格書 §3.4）

| 頁面 | 路徑 | 內容 |
|---|---|---|
| 今日決策中心（D.5） | `/decision` | 台彩機會 × risk-v1（含已實際下注 exposure）× bankroll：ACTIONABLE / REVIEW / RECORDED / BLOCKED、記錄下注、Evidence、System readiness（需登入） |
| 資金（D.5） | `/bankroll` | 手動 strategy bankroll：append-only ledger、day-start 凍結、User actual betting record（需登入） |
| 賽事總覽 | `/` | 每場卡片：對戰、台灣時間、模型勝率、預測分差/總分、上半場預測、台彩 vs 國際盤 vs 模型、Edge 標示；可切換 今日/明日/後天 或任選日期 |
| 單場詳情 | `/games/:id` | 盤口定價（每個 bookmaker / outcome：原始隱含、去水公允、模型、push、edge、EV；D.2）、逐節/半場表格、特徵拆解、盤口變動折線圖、本場傷病、球隊數據、雙方近況、H2H |
| 傷病中心 | `/injuries` | 依球隊分組的官方傷病申報 + 主力缺陣警示（警示球隊排序在前） |
| 回測 / 績效 | `/performance` | 模型歷史準確率、ATS、大小分、上半場命中率、Log Loss/Brier、模擬 ROI；個人實際投注損益曲線 |
| 投注紀錄 | `/bets` | 實際下注紀錄（手動記錄需確認流程）、作廢 / 更正（不刪除）、策略合規標示、手動結算 |
| 系統狀態 | `/status` | 各資料來源最後更新時間、新鮮度、失敗告警；每分鐘自動刷新 |
| 登入 / 註冊 | `/login` | email + password |

所有頁面皆常駐顯示免責聲明與台彩抽水提醒（規格書 §6）。

## 3. API 路由（規格書 §3.3）

| # | 端點 | 說明 |
|---|---|---|
| 5 | `GET /api/games/tomorrow` | 隔日（台灣時間）賽事 + 最新 predictions/odds + edge 分析 |
| — | `GET /api/games/today` | 今日賽事 |
| — | `GET /api/games?date=YYYY-MM-DD` | 任意台灣日期（格式錯誤回 400） |
| 6 | `GET /api/games/:id` | 單場詳情：box score、H2H、近況、特徵拆解、盤口歷史、傷病 |
| 7 | `GET /api/injuries/today?date=` | 當日各隊傷病報告（含主力缺陣警示） |
| 8 | `GET /api/predictions/:gameId` | 該場最新預測 |
| 9 | `GET /api/odds/:gameId` | 盤口快照歷史 + 已分組時間序列（供折線圖） |
| — | `GET /api/sizing?date=YYYY-MM-DD` | D.3：該 betting day 最新一次理論注碼與單日 / 單場 exposure（只讀、非推薦） |
| 10 | `GET/POST /api/bets`、`PATCH /api/bets/:id`、`POST /api/bets/:id/void`、`POST /api/bets/:id/correction`、`DELETE /api/bets/:id`（= void） | 實際下注紀錄（需登入；D.5 provenance / risk-v1 檢查 / 稽核） |
| — | `GET /api/decision-board?date=` | D.5 決策中心（Python 物化；需登入） |
| — | `GET /api/bankroll`、`POST /api/bankroll/entries` | D.5 strategy bankroll（需登入） |
| 11 | `GET /api/system/status` | 各資料來源最後更新時間與健康度 |
| — | `GET /api/metrics` | 模型回測績效 |
| — | `GET /api/teams` | 球隊清單 |
| — | `POST /api/auth/register`、`/login`、`/logout`、`GET /api/auth/me` | 驗證機制（§3.2） |
| — | `GET /healthz` | 健康檢查 |

## 4. 資料架構

- **Schema**（初版 12 張表）：`teams` `players` `games` `team_game_stats` `player_game_stats` `injuries` `odds_snapshots` `predictions` `bets` `users`，另加 `data_sources`（資料來源健康度）、`model_metrics`（回測績效）。後續各 Phase 的新增表見各 Phase 報告。
- **Migration**：`migrations/postgres/`（正式，權威版）與 `migrations/d1/`（本機開發）欄位語意一致。
- **半場欄位**：`games.home_h1/home_h2/away_h1/away_h2` 與 `predictions.pred_home_h1...` 自階段一即預留。
- **時區**：DB 一律存 UTC ISO8601，台灣時間換算集中在 `src/lib/time.ts`。

### 4.1 資料庫選型與 DB adapter

規格書要求「階段一、階段二都連得到的獨立 Postgres」。`src/db/index.ts` 依環境變數自動切換驅動：

| 環境 | 條件 | 驅動 |
|---|---|---|
| 正式（階段一 + 階段二共用） | 已設 `DATABASE_URL` | Postgres（Supabase） |
| 本機開發 | 未設 `DATABASE_URL`，有 D1 binding | D1（local SQLite） |

業務程式碼只依賴統一介面，SQL 一律用 `?` 佔位符（Postgres adapter 自動轉 `$1..$n`），切換資料庫不需改程式碼。D1 僅供開發，Python 服務無法連 D1。

### 4.2 切換到正式 Postgres（已於 2026-08 完成）

```bash
DATABASE_URL="postgresql://..." npm run db:migrate:pg   # 建立 schema
DATABASE_URL="postgresql://..." npm run db:seed:pg      # （選用）測試資料
npx wrangler pages secret put DATABASE_URL
npx wrangler pages secret put SESSION_SECRET            # openssl rand -base64 32
```

當時狀態：Supabase Postgres 建立、migration 套用、90 項 smoke test 在真實 Postgres + 並行流量下全數通過。

### 4.3 Seed 測試資料

單一樣板 `seed/seed.template.sql`，由 `scripts/render-seed.mjs` 把時間 token（`{{NOW:-30m}}`、`{{TPE:+1 08:00}}`）展開成固定 UTC 字串，**同一份 SQL 同時餵給 D1 與 Postgres**。

內容：10 支球隊、16 名球員、17 場比賽（明日 3 場含預測與盤口、今日 1 場進行中 + 1 場已結束、12 場歷史）、4 筆預測、28 筆盤口快照、9 筆傷病、8 個資料來源狀態、2 筆回測績效。

本機 seed 測試帳號 `demo@example.com` / `nba12345678` 是**公開密碼**，絕不可存在於正式資料庫（見 [production-runbook.md](production-runbook.md) §6）。

## 5. 階段一為階段二預留的接口

| 階段二只要寫入… | 網站就會自動顯示於… |
|---|---|
| `games`（含逐節/半場欄位） | 賽事總覽、詳情頁逐節表 |
| `predictions`（含 `features_json.contributions`） | 總覽卡片、詳情頁特徵拆解 |
| `odds_snapshots`（`source='twsport'` / `'oddsapi'`） | 盤口比較表、變動折線圖 |
| `market_pricing_snapshots`（D.2） | 詳情頁盤口定價表、總覽 Edge 標示 |
| `bet_sizing_snapshots`（D.3） | 詳情頁理論注碼表、`/api/sizing` |
| `injuries` | 傷病中心、主力缺陣警示、詳情頁 |
| `data_sources` | 系統狀態頁 |
| `model_metrics` | 回測績效頁 |

## 6. 驗收測試紀錄

`npm run test:api`（`scripts/smoke-test.mjs`）依賴 **seed 測試資料**，須對本機 D1 執行；對正式 Supabase 執行會出現「games=0」類失敗，屬預期。

| 時間點 | 檢查數 | 結果 |
|---|---|---|
| 階段一完成 | 90 | 90 / 0 |
| Phase D.2 | 96（+6 定價檢查） | 全數通過 |
| Phase D.3 | 106（+10 理論注碼不變量） | 全數通過 |
| 2026-10-15 稽核（含 D.5） | 160 | 160 / 0 |

涵蓋：頁面渲染、全部 API 契約、定價讀取（去水總和 = 1、edge = 模型 − 公允）、抽水 > 0、授權保護（401）、bets 寫入→讀回→結算→作廢、ROI 以台彩實際賠率計算、404/400 錯誤處理。

## 7. 當時待與使用者確認的事項（已結案 / 保留）

1. ~~Edge 精算範圍~~：D.2 已改為機率型 `edge_vs_fair = 模型 − 去水公允`，線差（`line_gap`）不再稱為 edge；Kelly 於 D.3 以 push-aware 公式重做。
2. 球隊中文名：目前 seed 用常見譯名，可依偏好調整（例如「塞爾提克」vs「凱爾特人」）。
3. 正式部署與 Hyperdrive：見 [dev-pitfalls.md](dev-pitfalls.md) §1。
