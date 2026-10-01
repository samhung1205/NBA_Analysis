# NBA 對戰預測平台 — 開發規格書 (v2.0 兩階段版)

> 用途:交付給 AI Agent 逐階段實作。
> **階段一**使用網頁建置範本(全端網站或應用程式:Hono + Node.js + Cloudflare Pages)完成網站殼子。
> **階段二**接續交給 Claude Code,補完範本做不到的資料擷取、爬蟲、排程與 ML 預測引擎。
> 最終目的:每日分析隔天 NBA 對戰,輸出「全場勝負、讓分、大小分、上/下半場表現」預測與信心度,並與台灣運彩盤口比對,輔助個人投注決策。

---

## 0. 整體架構與銜接原則

```
┌───────────────────────────┐        ┌──────────────────────────────┐
│  階段一:網頁殼子            │        │  階段二:資料工程 + ML 引擎      │
│  Hono + Cloudflare Pages   │        │  Python (Claude Code 環境)     │
│  - 登入/驗證                │        │  - 各來源 fetcher              │
│  - API 路由 (讀/寫)         │◄──────►│  - 排程器 (cron)               │
│  - 前端儀表板 (先用假資料)   │  共用   │  - 特徵工程 + Elo/XGBoost      │
│  - DB schema 定義           │  資料庫  │  - 台彩爬蟲 + 價值分析          │
└───────────────────────────┘        └──────────────────────────────┘
```

**⚠️ 銜接前提(必須在階段一就決定,否則階段二接不上):**

- **資料庫必須用階段一、階段二都連得到的獨立 Postgres**,例如 Supabase 或 Railway 的 Postgres 服務。
  **不可**使用 Cloudflare Pages 內建、僅限 edge function 存取的資料庫(如 D1 若無法從外部 Python 服務連線,則不適用),否則階段二的 Python 排程/爬蟲服務將無法寫入資料。
- 階段一建好 schema 與 API 之後,**前端一律讀資料庫,不寫死假資料在前端程式碼裡**——可以先在 DB 塞測試資料(seed),但讀取路徑要走真實 API,這樣階段二把真實資料寫進去後,前端不需要改任何程式碼就能顯示。
- 資料庫連線字串、金鑰統一放在環境變數,兩階段的服務都用同一組(或至少同一個資料庫實例的讀寫權限)。

---

## 1. 資料來源清單(階段二會用到,階段一先了解即可)

### 1.1 免費 / 開源(主力)

| 來源 | 存取方式 | 提供資料 | 注意事項 |
|---|---|---|---|
| **NBA Stats API** (stats.nba.com) | Python 套件 `nba_api`(pip) | 賽程、即時比分、逐節比分、box score、進階數據(ORtg/DRtg/Pace)、球員數據、歷史對戰 | 無官方文件、會封鎖高頻請求;需加 headers 偽裝、隨機延遲、失敗重試,排程抓取寫入 DB,絕不即時現抓 |
| **NBA 官方傷病報告** | 套件 `nbainjuries`(pip)或直抓官方 PDF/JSON 快照 | 官方申報之球員出賽狀態(Out/Doubtful/Questionable/Probable/Available)、傷病原因 | 2021-22 起有資料;賽前一日 17:00(當地)前申報,賽前會多次更新,需高頻重抓 |
| **ESPN 隱藏 API** (site.api.espn.com) | 直接 HTTP GET,無需金鑰 | 賽程、即時比分、新聞、交易異動、傷病新聞 | 無文件但多年穩定;作為 NBA API 的備援與交叉驗證 |
| **balldontlie API** | REST,免費層需註冊金鑰 | 球隊、球員、比賽、box score、即時更新 | 免費層有速率限制;第二備援 |
| **Basketball Reference** | 套件 `basketball_reference_scraper` | 深度歷史數據、進階數據、歷史傷病 | 限每分鐘 20 請求;僅用於離線回填歷史資料 |

### 1.2 付費(免費層先用)

| 來源 | 免費層 | 用途 |
|---|---|---|
| **The Odds API** | 500 req/月 | 國際盤讓分、大小分、獨贏賠率,與台彩盤口比對找價值 |

### 1.3 爬蟲

| 目標 | 內容 | 方式 |
|---|---|---|
| **台灣運彩官網** | NBA 各玩法盤口(不讓分、讓分、大小、上半場) | Playwright 無頭瀏覽器,每 30 分鐘抓一次;需容錯(改版偵測+告警) |
| Rotowire / CBS 傷病頁 | 傷病新聞補充 | 備援,低優先 |

---

## 2. 資料庫 Schema(階段一建立,雙方共用)

```sql
teams(id, nba_team_id, abbr, name, conference, division)
players(id, nba_player_id, name, team_id, position, status)
games(id, nba_game_id, season, date_utc, home_team_id, away_team_id,
      status,            -- scheduled / live / final
      home_pts, away_pts,
      home_q1..q4, away_q1..q4, home_ot, away_ot,
      home_h1, home_h2, away_h1, away_h2)   -- 半場欄位,供半場模型使用
team_game_stats(game_id, team_id, fg_pct, fg3_pct, ft_pct, reb, ast, tov,
                pace, off_rtg, def_rtg, ...)
player_game_stats(game_id, player_id, min, pts, reb, ast, ..., plus_minus)
injuries(id, report_time_utc, player_id, team_id, status, reason, game_id)
odds_snapshots(id, fetched_at, game_id, source,   -- 'twsport' / 'oddsapi'
               market,     -- ml / spread / total / h1_ml / h1_spread ...
               line, home_odds, away_odds, over_odds, under_odds)
predictions(id, game_id, model_version, created_at,
            home_win_prob, pred_margin, pred_total,
            pred_home_h1, pred_away_h1, pred_home_h2, pred_away_h2,
            confidence, features_json)
bets(id, game_id, market, selection, line, odds, stake, result, payout, note)
   -- 個人下單紀錄,用於實際績效追蹤
users(id, email, password_hash, created_at)   -- 階段一驗證用
```

---

## 3. 階段一:網頁建置範本(全端網站或應用程式)

**目標**:用 Hono + Node.js + Cloudflare Pages 範本,做出一個能登入、能讀寫資料庫、UI 齊全,但資料先是空的/測試資料的網站骨架。**驗收標準:所有頁面能跑,所有 API 能讀寫資料庫,seed 測試資料後畫面正確顯示。**

### 3.1 基礎建設
1. 初始化範本專案,設定 Postgres 連線(Supabase/Railway),確認 Cloudflare Pages 端與外部 Python 服務都能連到同一個資料庫實例
2. 用 migration 工具建立第 2 節全部 schema
3. 寫一份 seed script,灌入假資料(幾支球隊、幾場比賽、假預測、假盤口)供開發期測試 UI

### 3.2 驗證機制
4. 個人登入(email/password 即可,不需社交登入),保護 `bets`、績效頁等私人資料

### 3.3 API 路由(先接資料庫,資料是否真實不重要,重點是路徑與格式定義好)
5. `GET /api/games/tomorrow` — 隔日(台灣時間)賽事列表 + 對應最新 predictions/odds
6. `GET /api/games/:id` — 單場詳情(box score 歷史、H2H、特徵拆解 JSON)
7. `GET /api/injuries/today` — 當日各隊傷病報告
8. `GET /api/predictions/:gameId` — 該場最新預測
9. `GET /api/odds/:gameId` — 盤口快照歷史(供折線圖)
10. `POST /api/bets`、`GET /api/bets` — 個人下單紀錄 CRUD
11. `GET /api/system/status` — 各資料來源最後更新時間(先回傳假的時間戳即可,階段二再接真實狀態)

### 3.4 前端頁面(UI 先做出來,資料先用 seed 假資料驗證)
12. **今日/明日賽事總覽**:每場卡片 — 對戰、台灣時間、模型勝率、預測分差/總分、上半場預測、台彩盤口 vs 國際盤 vs 模型、edge 標示
13. **單場詳情頁**:特徵拆解、逐節預測、盤口變動歷史折線圖
14. **傷病中心**:當日各隊傷病報告 + 主力缺陣警示
15. **回測/績效頁**:模型歷史準確率、ATS、模擬 ROI、個人實際投注損益曲線
16. **系統狀態頁**:各資料來源最後更新時間、失敗告警(先顯示假資料)

### 3.5 階段一交付物
- 可部署的 Cloudflare Pages 網站,能登入
- 完整 DB schema + migration 檔案
- 所有 API 路由可用(用 seed 資料驗證過)
- 所有前端頁面渲染正確
- 一份 `.env.example` 列出資料庫連線等所需環境變數,方便階段二的 Python 服務直接沿用

---

## 4. 階段二:Claude Code 補完(資料工程 + ML 引擎)

**前置動作**:確認能用階段一相同的資料庫連線字串連進去,直接對接同一批表,不需改動階段一的 API/前端。

### Phase A — 資料基礎(驗收:DB 有近 2 季完整真實資料且每日自動更新)
1. Python 專案結構、排程器(APScheduler,之後可換 Celery beat)
2. `nba_api` fetcher:賽程、比分(含逐節)、box score、球隊/球員數據
3. 傷病 fetcher(`nbainjuries` / 官方報告)
4. ESPN 備援 fetcher
5. 回填近 5 季歷史資料(含逐節比分,供半場模型)
6. 排程上線,寫入 `system/status` 對應資料,讓階段一的系統狀態頁顯示真實新鮮度

### Phase B — Baseline 預測(驗收:Elo 回測近 2 季 accuracy ≥ 63%)
7. 特徵工程 pipeline:賽程密集度/休息日、近況(近5/10場加權)、傷病影響(主力缺陣戰力損失)、歷史對戰、主客場、賽季階段
8. Elo 模型 + walk-forward 回測框架(嚴禁隨機切分造成資料洩漏)
9. 預測結果寫入 `predictions` 表 → 階段一的總覽頁自動顯示真實預測

### Phase C — ML 模型 + 半場預測(驗收:ML 全指標優於 Elo baseline)
10. XGBoost 勝負/分差/總分模型 + 機率校準(isotonic/Platt)
11. 上半場獨立迴歸模型(上半場分差、上半場總分)
12. 特徵拆解寫入 `predictions.features_json`,供階段一單場詳情頁的「為什麼這樣預測」呈現

### Phase D — 盤口與價值分析(驗收:每日自動產出含 edge 排序的推薦清單)
13. 台灣運彩爬蟲(Playwright)+ 盤口快照寫入 `odds_snapshots`
14. The Odds API 整合(國際盤)
15. Edge 計算(模型機率 − 盤口隱含機率,需扣除運彩抽水)、Kelly 建議(預設 1/4 Kelly 上限)
16. 回測 ROI 計算模組,供階段一績效頁使用(**必須用台彩實際賠率計算,不可用國際盤賠率**)

### Phase E — 強化(選做)
- 球員層級模型(On/Off、lineup 數據)、旅行距離、裁判因素
- Line movement 特徵(盤口移動方向作為市場訊號)
- Telegram/LINE 每日推播推薦

---

## 5. 排程設計(台灣時間,階段二實作)

| 任務 | 頻率 |
|---|---|
| 抓隔日賽程 + 歷史對戰 | 每日 12:00 |
| 抓傷病報告 | 比賽日每 30 分鐘(美東尖峰時段加密到每 15 分) |
| 抓台彩盤口 + 國際盤 | 每 30 分鐘(國際盤視 Odds API 額度可降為每 2 小時) |
| 產生/更新預測 | 傷病或盤口有變動即重算;至少每 2 小時 |
| 比賽中即時比分 | 比賽進行中每 60 秒 |
| 賽後結算 + 回寫結果 | 每場結束後 30 分鐘 |
| 模型重訓 | 每週一次 |

---

## 6. 風險與注意事項

- **法遵**:僅供個人於台灣運彩(合法管道)投注參考,平台不對外提供投注服務、不代操、不收費。
- **抽水現實**:台彩返還率低於國際盤,長期獲利門檻高;所有推薦與回測 ROI 必須用台彩實際賠率計算,避免自我欺騙。
- **爬蟲穩定性**:stats.nba.com 與台彩官網皆可能改版或封鎖;需有改版偵測、告警、備援來源切換。
- **資料洩漏**:特徵計算只能使用「比賽開打前已知」的資訊(傷病報告取賽前最後一版),回測框架需強制檢查時間戳。
- **兩階段銜接風險**:若資料庫選型在階段一就選錯(例如用了 Python 服務連不到的 edge-only DB),階段二會需要重做資料庫層,務必在階段一開工前先確認。
- **不保證獲利**:模型輸出為機率參考,前端需常駐顯示此聲明與當前模型回測績效。
