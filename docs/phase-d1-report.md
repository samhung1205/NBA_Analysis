# Phase D.1 — Odds Ingestion & Market Normalization 報告

日期：2026-10-04　範圍：fetch → parse → normalize → match game → persist snapshots → scheduler / monitoring。
**未做**：去水 / edge / EV / Kelly / 推薦 / ROI backtest；未動 C.5C 模型、C.5E 分佈；未購買任何方案；未繞過任何 anti-bot；
未寫入正式 DB（odds_snapshots 仍 0 筆、未套用 migration 0004、predictions / bets 未動）；未 commit。

## 0. 結論摘要

| 項目 | 結果 |
|---|---|
| Schema | 既有 `odds_snapshots` 不足以無損保存（無 bookmaker / period / 來源事件 id / 來源時間 / 狀態 / 門檻 / 去重鍵）→ 最小必要 migration `0004_phase_d1.sql`（純新增欄位 + 2 張表 + 1 個 view），API / 前端向下相容 |
| Canonical market | `(market_type, period)` 明確身分；讓分正負號只在 `core/odds/canonical.py` 定義一次；每個 outcome 都有 `model_target` / `model_threshold` / `comparator`，直接對上 C.5E 的 `P(target > threshold)` |
| 賽事對應 | 獨立 matching 層 + `odds_event_links`；別名表可檢查、不做模糊比對；不唯一 / 主客相反 / 延期 一律不寫入並告警 |
| 快照 | 時間序列：內容沒變只推 `last_seen_at`；線 / 價 / 狀態一變就新增；舊列永不改寫；亂序資料拒收；advisory lock + 部分唯一索引保證併發冪等 |
| 台灣運彩 | Parser 以**真實回應**（一般瀏覽器擷取）驗證，6 種主要市場（含上半場三向獨贏）+ 狀態 + 交叉驗證；**自動化瀏覽器被 Cloudflare 擋（403）**，依約定不繞過 → job 回報 `blocked`，提供 HAR 匯入的合規備援 |
| The Odds API | Adapter 完成（多 bookmaker 保留、額度標頭、免費端點先探、額度保留門檻）；**環境中沒有 `ODDS_API_KEY`**，無法 live 驗證（0 credits 使用） |
| 排程 | 台彩每 30 分（:03/:33）、Odds API 每日 4 次（00/06/12/18:40 台灣，≈ 372 credits/月）；各自 guarded、互不影響 |
| 測試 | 新增 104 項（純邏輯 87 + DB 17）；完整 `pytest` **409 passed**；Node smoke（本機 D1 seed）**90 / 90** |
| Historical odds | 正式 DB **0 筆**；嚴格 historical ROI 只能從開始累積快照當天起算（§9） |
| **可否開始 D.2** | **可以開始設計與實作（以 fixture / 合成資料）**，但 D.2 的實證工作需要先解決兩個前置：套用 migration + 設 `ODDS_API_KEY`；台彩資料取得方式（§14） |

---

## 1. Existing schema audit

`odds_snapshots`（0001_init）：`id, fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds, raw_json`。
API：`getLatestOdds` 取每個 (game, source, market) `MAX(fetched_at)`；`buildEdgeAnalysis` 以 `pick(source, market)` 取第一筆；
`/api/odds/:gameId` 以 `source:market` 分時間序列。Seed：一列一個市場（兩邊賠率），讓分 `line` 為「主隊讓分顯示線（負 = 主讓）」。
`bets.market` 驗證值為 `ml / spread / total / h1_ml / h1_spread / h1_total`。正式 DB：`odds_snapshots` 0 列、`data_sources` 有 seed 遺留的 `twsport` / `oddsapi`（2026-08-01，非真實）。
排程：無盤口 job。`.env`：無 `ODDS_API_KEY`（`.dev.vars` / `pipeline/.env` / shell 皆無）。既有程式：無任何台彩 / Odds API code。

| 需求欄位 | 既有 | 處理 |
|---|---|---|
| source | ✅ `source` | 沿用 |
| bookmaker | ❌（Odds API 多家會互相覆蓋 / API 取第一筆） | `bookmaker` |
| source event id | ❌ | `source_event_id`（+ `odds_event_links`） |
| fetched_at | ✅ | 沿用；語意明定為「我們收到回應的時間」 |
| market / period | ⚠️ 只有字串 `market`（`h1_spread` 靠字串前綴） | `market_type` + `period`；`market` 保留為既有顯示代碼，由同一函式導出 |
| side | ⚠️ 隱含在欄位名（home/away/over/under），無和局 | 一列 = 一個完整市場；新增 `draw_odds`、`outcome_set`；`v_odds_quotes` 攤成每 outcome 一列 |
| sportsbook display line | ⚠️ 只有一條 `line` | `line`（主隊 / 總分）+ `away_line`（客隊顯示線，明存不在 SQL 算） |
| normalized model threshold | ❌ | `model_target` + `model_threshold` |
| decimal odds | ✅ | 沿用；原始格式在 `raw_json.outcomes[].price_raw` |
| source publish/update time | ❌ | `source_updated_at`（與 `fetched_at` 分開） |
| 狀態 suspended/closed | ❌ | `market_status` |
| 去重 / 最後確認 | ❌ | `content_hash`、`last_seen_at`、`fetch_run_id`、`normalizer_version` |

**設計選擇：擴充 `odds_snapshots`，不另開 per-outcome 表。** 一列 = 某一刻某 bookmaker 某市場的**完整兩/三向報價**（D.2 去水需要同一時刻的兩邊）；
per-outcome 的 canonical quote 由 view `v_odds_quotes` 提供（不做正負號運算，門檻已由 Python 寫入）。一張表 = 一套去重規則、API 幾乎不用改。

**Migration `0004_phase_d1.sql`（Postgres）/ `migrations/d1/0004_phase_d1.sql`（本機 D1）**：
`odds_snapshots` +16 欄（全部 nullable，舊列不受影響）、部分唯一索引 `uq_odds_series_fetch (game_id, source, bookmaker, market, fetched_at) WHERE content_hash IS NOT NULL`、
latest 索引；新表 `odds_fetch_runs`、`odds_event_links`；`data_sources` + `last_outcome`、`meta`；view `v_odds_quotes`。`npm run db:migrate:local` 改為依序套用 D1 的 0001 / 0002 / 0004（0002 先前未納入）。

## 2. Canonical market model（`pipeline/core/odds/canonical.py`）

| canonical key | market_type | period | 既有 `market` | outcome_set | model_target |
|---|---|---|---|---|---|
| moneyline | moneyline | full_game | ml | two_way | margin |
| spread | spread | full_game | spread | two_way | margin |
| total | total | full_game | total | two_way | total |
| h1_moneyline | moneyline | h1 | h1_ml | two_way（Odds API）/ **three_way（台彩，含和局）** | h1_margin |
| h1_spread | spread | h1 | h1_spread | two_way | h1_margin |
| h1_total | total | h1 | h1_total | two_way | h1_total |

`MarketSnapshot`：game 由對應層決定；`source, bookmaker, source_event_id, market_type, period, outcome_set, status, line, away_line, home/away/draw/over/under 賠率, source_market_id, source_updated_at, raw`。
`quotes()` → 每個 outcome 的 `NormalizedQuote(side, price, display_line, model_target, model_threshold, comparator)`。

驗證（`make_snapshot`）：decimal ∈ [1.001, 1000] 且有限；open 市場每一邊都必須有合法賠率（否則整個市場拒收，不寫半個市場）；
讓分兩邊必須對稱（不對稱 → `asymmetric_spread` 拒收，不猜）；線必須是 0.25 的倍數；全場讓分 |line| ≤ 40、上半場 ≤ 25；全場總分 150–320、上半場 70–170；
非 open（suspended / closed / unknown）照實保存狀態、非法賠率存 NULL，**絕不當成 active quote**。賠率一律 decimal；台彩分數 `up/down` → `1 + up/down`（Fraction 精確計算、四捨五入 4 位）；美式有轉換器（防呆）。
不計算 implied probability / 去水。

## 3. Spread sign convention（全專案唯一定義處）

```
模型：margin = home_score − away_score；C.5E 介面回答 P(target > threshold)

主隊顯示線 h（例 −5.5）：主隊過盤 ⇔ margin + h > 0 ⇔ margin > −h      → threshold = −h = +5.5，comparator gt
客隊顯示線 a（例 +5.5）：客隊過盤 ⇔ −margin + a > 0 ⇔ margin < a       → threshold =  a = +5.5，comparator lt
大小分：大 ⇔ total > line（gt）、小 ⇔ total < line（lt）；threshold = line
獨贏：主 ⇔ margin > 0；客 ⇔ margin < 0；和（三向）⇔ margin = 0
整數線的 push（= threshold）不屬任何一邊，由 C.5E probability_push 表達（D.2 處理結算）
```

DB：`line` = 主隊顯示線（與既有 seed / API 慣例「主隊讓分為負值」相同）、`away_line` = 客隊顯示線、`model_threshold` = 主隊 / 大 那一邊的門檻。
D.2 用法：`P(主隊過盤) = probability_above(target, model_threshold)`、`P(客隊過盤) = probability_below(...)`。
實測對照（台彩 2026-10-04）：丹佛金塊（主）−3.5 → threshold +3.5；洛杉磯快艇（主）+2.5 → threshold −2.5（網站顯示「金州勇士 −2.5 / 洛杉磯快艇 +2.5」）。
測試：`test_home_spread_sign_conversion`、`test_away_spread_sign_conversion_is_symmetric`、`test_home_and_away_cover_regions_partition_margin`（−30…+30 每個 margin 驗證兩邊與門檻一致）、`test_canonical_quote_view`（DB view）。

## 4. Source-specific mapping

| canonical | 台灣運彩（Betradar 參照碼） | The Odds API |
|---|---|---|
| moneyline | `219`：outcome 4 主 / 5 客 | `h2h`：outcome.name == home_team / away_team |
| spread | `223/hcp=X`：1714 主 / 1715 客；X = **主隊**顯示線 | `spreads`：各隊 `point` |
| total | `225/total=X`：12 大 / 13 小 | `totals`：Over / Under + `point` |
| h1_moneyline | `60`：1 主 / **2 和** / 3 客（三向） | `h2h_h1`（兩向） |
| h1_spread | `66/hcp=X` | `spreads_h1` |
| h1_total | `68/total=X` | `totals_h1` |
| 原始價格 | 分數 `currentpriceup/currentpricedown` | `oddsFormat=decimal` |
| 來源時間 | 無（`tsbetstart/tsbetend` 存 raw） | `markets[].last_update`（無則 `bookmakers[].last_update`） |
| 狀態 | event/market `istradable`、market `idfobolifestate`（O 開 / S 停 / C·R·V·X 關）、selection `idfoselectionsuspensiontype` | 下架即不回傳（無狀態欄位）→ 一律 open |

`raw_json` 每列保存：parser 版本、原始 market ref / 名稱 / id、主盤選擇規則、每個 outcome 的原始名稱 / ref / hadvalue / 原始價格與格式 / 原始線、來源事件 id、canonical key、normalizer 版本（足以在 normalization 出錯時回溯）。

## 5. Taiwan Sports Lottery implementation（`core/odds/twsport.py`）

**網站 audit（2026-10-04，一般瀏覽器、只讀）**
- `www.sportslottery.com.tw/sportsbook` 的盤口在 iframe：ORAKO sportsbook `www-talo-ssb-pr.sportslottery.com.tw`。資料來自網站自己使用的同源 JSON：
  `POST /services/content/get {"contentId":{"type":T,"id":ID},"clientContext":{...}}`，T = `boNavigationList`（運動 → 國家 → 聯賽樹）、`eventGroup`（聯賽賽事 + 主盤）、`event`（單場全部玩法）。
- 籃球 → 美國 → 聯賽：`美國職籃`（休賽期 0 場，節點仍存在）、`NBA盃`、`美國職籃熱身賽`（當日 2 場）、WNBA 等。名稱帶 `\r`。
- 事件：`participantname_home / _away` 明確（名稱為「客 @ 主」）、`tsstart` 帶 `+08:00`。coupon（eventGroup）只含主盤（全場 3 種）；上半場玩法只在 `event` 詳細資料。
- 詳細頁同一玩法有多條線（讓分 1.5 / 2.5 / 3.5），以 `ismainline` 標示主盤；上半場讓分只有一條但 `ismainline=false`。
- 陷阱：大小分的 `hadvalue` 是 A=大 / H=小（不可用）；讓分的正負號只出現在選項名稱，`currenthandicap` 兩邊同值（= 主隊線）。

**實作**
- Fetch：`TwsportClient`（Playwright + 本機 Google Chrome）載入 sportsbook，於頁面內呼叫同一個同源 JSON（與網站自己的呼叫相同）；請求間隔 1–2 秒；每輪 ≈ 3 + NBA 聯賽數 + NBA 場次 個請求。導覽以名稱 / 型別（不依賴 nth-child / DOM）。
- Parse（純函式）：以參照碼辨識市場；主盤規則 `ismainline` 唯一 → 採用；沒有標示且只有一條 → 採用（`single_line`）；其他 → `ambiguous_main_line` 不猜。
  交叉驗證：ref 與名稱（`[上半場]`、不得含「節」）一致；`hcp` = `market.line`；選項名稱尾端帶號數字 = 主 `+hcp` / 客 `−hcp`；ML / 讓分 `hadvalue` 與主客一致；大小與「大 / 小」一致；provider 必須是 Betradar。任何不一致 → 該市場拒收並記原因。
- 改版偵測：缺 `data.bonavigationnodes` / 找不到「美國」/ 找不到「美國職籃」節點 / 有場次卻沒有 MATCHESCOUPON / eventGroup 沒有 `events` / event 缺主客或時間 / 宣稱 N 場卻解析 0 場 / 有場次但 0 個合法市場 → `parser_changed`（error），並保存 raw 樣本（`artifacts/odds_raw/`，gzip、上限 4 MB、保留 30 份）。
- **Anti-bot 結果（重要）**：
  - curl / requests：Cloudflare managed challenge（403 "Just a moment..."）。
  - Playwright headless Chrome：challenge 不會通過（標題變成「請稍候...」）。
  - Playwright 一般視窗 Chrome：頁面可載入（取得 `cf_clearance`），但**網站自己的 `/services/content/get` 也回 403**——Cloudflare 對自動化瀏覽器擋 API。
  - 再往下就需要隱藏 webdriver、偽造指紋 / UA、stealth 外掛或換 IP，屬於 anti-bot circumvention → **依約定不做**。job 回報 `blocked`（error），連續 3 次後 6 小時退避，不反覆開瀏覽器。
- **合規備援**：`python run_odds.py --source twsport --from-file capture.har` —— 使用者用一般瀏覽器開 NBA 聯賽 / 賽事頁、DevTools 匯出 HAR，即可匯入（`fetched_at` = 所用回應中最晚的 `startedDateTime`，不宣稱更早看到）。也可匯入含 `captured_at` 的 raw JSON。
- Fixture：`tests/fixtures/odds/twsport_raw_preseason_20261004.json`（NBA 熱身賽 2 場，真實回應，去除顯示用欄位）、`twsport_event_detail_h1_20261004.json`（當時唯一有上半場玩法的籃球賽事，WNBA，只用於測上半場 / 多線 / 停售解析，已裁成相關市場）。

## 6. The Odds API implementation（`core/odds/oddsapi.py`）

- 流程：`GET /v4/sports/basketball_nba/events`（**不扣額度**，未來 48 小時沒比賽就停）→ `GET /odds?markets=h2h,spreads,totals&regions=us&oddsFormat=decimal`（3 credits）。
  上半場只能 `GET /events/{id}/odds`（每場 3 credits）→ 免費方案負擔不起，`ODDS_API_INCLUDE_H1=false` 預設關閉。
- 保留：event id、commence_time、每家 bookmaker key / title / last_update、market key / last_update、outcome name / price / point。**不平均、不選最佳盤、不做 consensus / 去水**。
- 額度：每個回應的 `x-requests-remaining / used / last` → `odds_fetch_runs` 與 `data_sources.meta`；剩餘 − 本次成本 < `ODDS_API_QUOTA_RESERVE`（25）→ 不呼叫、`quota_low`；剩餘 < `ODDS_API_QUOTA_WARN`（100）→ 系統狀態 warn。
- 錯誤：401 → `auth_failed`、429 → `rate_limited`、422 → `parser_changed`（參數不被接受）、網路 → `network_failure`、無 key → `not_configured`。
- 限制（只記錄、未購買）：歷史端點（`/v4/historical/...`，cost ×10）僅付費方案；免費方案 500 credits/月。
- **Live 驗證：未執行**——環境中沒有 `ODDS_API_KEY`（未建立帳號、未申請 key）。Fixture `oddsapi_raw_synthetic.json` 是依官方 v4 文件 schema 建的**合成資料**（明確標示）。

## 7. Game reconciliation strategy（`core/odds/matching.py`、`team_aliases.py`）

1. 隊名 → abbr：`team_aliases.TEAM_ALIASES`（30 隊；英文全名 / 簡稱 / LA·L.A. 變體 / 台灣常用譯名與異體），`normalize()`（NFKC 全形轉半形、去 `\r` 等控制字元、去句點撇號、合併空白、小寫）後**完全相等**才算；同一別名對到兩隊 → 載入時就失敗。刻意不收「Los Angeles / LA / 洛杉磯」。
2. 人工對應（`odds_event_links.manual = true`）→ 直接採用、永不被自動覆寫。
3. 既有自動對應仍一致（同主客、開賽差 ≤ 36h）→ 沿用（`link`）；不一致 → 重新比對並標 `link_invalidated`。
4. 同主客、開賽差 ≤ 3h：唯一 → `teams_time`；多筆 → `ambiguous`。
5. 嚴格視窗只有主客相反的比賽 → `rejected / home_away_reversed`（寫進去會讓所有正負號顛倒）。
6. 同主客、開賽差 ≤ 36h 且比賽尚未開打：唯一 → `rescheduled`（會寫入，但標為告警；時區錯誤也長這樣）；多筆 → `ambiguous`。
7. 否則 unmatched：來源標示季前賽 → `preseason_not_tracked`（games 依設計不收季前賽，不告警）；超出賽程同步範圍 → `beyond_schedule_horizon`（不告警）；其餘 `no_candidate`（告警）。比賽 postponed / cancelled → `rejected / game_postponed`。
8. 只寫入 `matched` 且 `fetched_at < 開賽時間` 的市場（D.1 只收賽前盤；開賽後的報價記為 `skipped_in_play`）。
時區：台彩 `+08:00`、Odds API `Z` 都在 adapter 轉成 aware UTC；比對一律 UTC。每個來源事件的結果寫入 `odds_event_links`（含候選清單、時間差、原始隊名），可人工檢查 / 指定。

## 8. Snapshot / dedup semantics（`core/odds/store.py`）

- series = (game_id, source, bookmaker, market)。`content_hash` = sha256(game, source, bookmaker, market, market_type, period, outcome_set, status, line, away_line, 各邊 decimal〔4 位〕)。
- 與 **series 最新一列**比較：相同 → 只 `last_seen_at = GREATEST(...)`；不同（線動、只動價、狀態變）→ INSERT 新列。A → B → A 產生三列。舊列的價格 / 線 / 時間 / 雜湊永不改寫（`last_seen_at` 是唯一可變欄位，只會往後移）。
- **與需求例子的差異**：`source_updated_at` 不放進去重身分。Odds API 的 `last_update` 會在價格沒變時前進（同一 bookmaker 其他市場變動也會帶動），放進身分會每次輪詢都新增重複列；改為：第一次觀測時的來源時間存在該列，之後的確認以 `last_seen_at` 表示。
- 時間：`fetched_at` = 我們收到回應的時間；早於 series 最新 `fetched_at`（或內容不同且早於 `last_seen_at`）→ **stale 拒收**（不讓晚到的舊觀測插進歷史）；來源時間晚於 `fetched_at` + 2 分 → 欄位留 NULL、原值存 raw。
- 冪等 / 併發：同一 source 的寫入在單一交易內 `pg_advisory_xact_lock`；部分唯一索引擋同一 `fetched_at` 重跑。測試：4 個執行緒同時寫同一份新內容 → 只有一列。
- API 的 latest：每個 (game, source, bookmaker, market) 取最新一列；最新一列不是 open → 不顯示（不退回較舊的 open 報價）。歷史全部保留在 `/api/odds/:gameId`。

## 9. Historical-backtest readiness

- 正式 DB `odds_snapshots` **0 筆**；沒有任何 2025-26 或更早的真實盤口。→ **以台彩實際賠率的嚴格 historical ROI backtest，最早只能從「migration 0004 套用 + 開始實際累積台彩快照」那天的比賽起算**（2026-27 例行賽 2026-10-20 前後開打；目前台彩自動擷取被擋，見 §5）。
- 國際盤歷史：The Odds API 歷史端點只限付費方案（cost = 10 × markets × regions），本專案未購買、不假設可用。
- 從 D.1 起保證：快照不覆蓋、`fetched_at` 為觀測時間且只往前、`source_updated_at` 分開、亂序拒收、in-play 報價不寫、每列可回溯原始資料與 parser 版本；`fetched_at ≤ T ≤ last_seen_at`（+ 輪詢間隔容差）即「T 時點可看到的報價」。
- D.4 注意：快照只在輪詢時觀測；兩次輪詢之間的變動看不到（台彩 30 分、Odds API 6 小時解析度）。

## 10. Scheduler（`core/scheduling.py`）

| job | 觸發（台灣） | 說明 |
|---|---|---|
| `odds_twsport` | 每小時 :03 / :33 | 啟動時補跑；連續 3 次 blocked → 6 小時內不重開瀏覽器；`TWSPORT_ENABLED=false` 可停用 |
| `odds_oddsapi` | 00:40 / 06:40 / 12:40 / 18:40（`ODDS_API_CRON_HOURS`） | 4 × 3 credits × 31 天 ≈ 372 < 500；**啟動時不補跑**（避免重啟花額度）；無比賽不花額度 |

全部 job 經 `guarded`（例外只記 log）；odds job 自己再吞掉所有例外並寫 `odds_fetch_runs` / 心跳 → 不影響賽程 / 傷病 / 預測。
CLI：`python run_odds.py --source twsport|oddsapi|all [--dry-run] [--from-file F] [--save-raw F]`、`--print-next-runs`；`python scheduler.py --print-next-runs` 也列出兩個 odds job。
未套用 migration 0004 時，非 dry-run 的 job 會明確拒絕寫入（dry-run 照常唯讀運作）。

## 11. Source health / diagnostics

`odds_fetch_runs`（每次一列：outcome、延遲、請求數、事件 / 對應 / 市場 / 新增 / 未變 / stale 計數、額度、錯誤、每場摘要）+ `data_sources.last_outcome`：

| outcome | last_status | 意義 |
|---|---|---|
| success | ok | 全部對應、全部市場合法 |
| no_nba_markets | ok | 來源與 parser 正常，目前沒有 NBA 盤（訊息明寫） |
| partial | warn | 有寫入，但有 unmatched / ambiguous / rejected / 改期 / 無效市場 |
| quota_low / rate_limited / not_configured | warn | 額度不足主動略過 / 429 / 缺 key |
| parser_changed | error | 預期有 NBA 事件或市場但結構對不上（保存 raw 樣本） |
| auth_failed / network_failure / blocked / error | error | 401 / 網路 / anti-bot / 未預期例外 |

`last_success_at` 只在 ok 時更新；`/api/system/status` 新增 `last_outcome` 欄位（純新增）。

## 12. Tests / results

新增 104 項（`tests/test_odds_*.py`）：

| 要求 | 測試 |
|---|---|
| moneyline / home spread / away spread / total / H1 / 三向 | `test_moneyline_threshold_zero`、`test_home_spread_sign_conversion`、`test_away_spread_sign_conversion_is_symmetric`、`test_home_and_away_cover_regions_partition_margin`、`test_total_over_under`、`test_h1_markets_use_h1_targets`、`test_h1_three_way_moneyline_has_draw_eq_zero` |
| decimal 驗證 / NaN / 0 / 缺邊 / 線不合理 | `test_decimal_conversions_and_validation`、`test_open_market_requires_valid_prices_on_all_sides`、`test_unreasonable_lines_rejected`、`test_spread_from_away_line_only_and_asymmetric_rejected` |
| suspended | `test_suspended_market_keeps_status_and_never_looks_active`、`test_suspended_and_closed_state_preserved`、`test_suspension_is_a_new_snapshot` |
| exact / alias / timezone / ambiguous / 主客反轉 / 改期 / 延期 | `test_exact_match_by_teams_and_time`、`test_team_alias_resolution`、`test_alias_map_covers_30_teams_and_is_unambiguous`、`test_timezone_conversion_taipei_vs_utc`、`test_ambiguous_match_is_not_guessed`、`test_home_away_reversal_is_rejected`、`test_rescheduled_game_matched_within_wide_window_with_delta`、`test_postponed_game_rejected`、`test_existing_link_is_reused_and_invalidated_when_inconsistent` |
| 台彩 fixture / 缺盤 / 改版 / 重複 | `test_preseason_fixture_full_game_markets`、`test_event_detail_h1_markets_and_main_line_selection`、`test_missing_market_is_not_fabricated`、`test_ambiguous_main_line_is_skipped_not_guessed`、`test_cross_checks_reject_inconsistent_spread`（5 種）、`test_total_side_mapped_by_ref_and_name_not_hadvalue`、`test_structure_change_raises_parser_changed`、`test_events_with_no_recognized_markets_are_reported`、`test_fetch_raw_navigation_and_change_detection`、`test_duplicate_events_across_groups_deduped`、`test_har_import_uses_latest_response_time` |
| Odds API 多 bookmaker / fixture / 額度 / 缺盤 / 錯誤 | `test_multi_bookmaker_preserved_never_averaged`、`test_fixture_normalization_and_outcome_mapping`、`test_quota_metadata_and_free_events_call_first`、`test_no_events_spends_no_quota`、`test_quota_reserve_blocks_paid_call`、`test_missing_and_malformed_markets`、`test_http_errors_classified`、`test_missing_key_is_not_configured` |
| 儲存（DB） | `test_identical_poll_does_not_duplicate`、`test_line_move_and_price_only_move_create_new_snapshots_and_never_overwrite`、`test_out_of_order_and_time_semantics`、`test_bookmakers_are_separate_series`、`test_concurrent_writers_are_idempotent`、`test_canonical_quote_view` |
| Job / 心跳（DB） | `test_job_end_to_end_success_then_idempotent_rerun`、`test_dry_run_writes_nothing`、`test_partial_when_an_event_cannot_be_matched`、`test_preseason_events_are_expected_unmatched_not_alerts`、`test_in_play_quotes_are_not_written`、`test_no_markets_vs_parser_changed_are_distinguishable`、`test_one_source_failing_does_not_break_another`、`test_blocked_backoff_skips_browser`、`test_unexpected_exception_is_contained`、`test_oddsapi_not_configured_is_reported` |
| 排程 | `test_twsport_every_30_minutes_taipei`、`test_oddsapi_four_times_a_day_within_free_quota`、`test_guarded_job_failure_does_not_propagate`（+ 既有 `test_scheduler.py` 全部 job 非 paused / Cron 皆 Asia/Taipei） |

**完整 `cd pipeline && pytest`：409 passed（既有 305 + 新增 104；639 秒）**（DB 測試使用暫存 schema `c5a_test_*`，結束後 DROP；已確認無殘留、正式 `public` 未動）。

**Node smoke（`npm run test:api`）：90 通過 / 0 失敗**——對本機 D1 seed（`npm run db:reset:local` 套用 0001/0002/0004），啟動時以 `--env-file`（只含 SESSION_SECRET）+ `--binding DATABASE_URL=` 確保不連 Supabase，並先確認 `/api/system/status` 的 `db_driver = d1`。
另以本機 D1 手動插入 3 列驗證新行為：Odds API 兩家 bookmaker 各自為一條 latest / 序列；台彩最新一列 suspended → 總覽不顯示該市場（ML edge 隨之消失），歷史序列保留 4 個點。驗證後已重置本機 D1 並重跑 90 / 90。

## 13. Live smoke-test result（2026-10-04 台灣 12:00–13:00，dry-run，未寫入正式 DB）

| 來源 | 結果 |
|---|---|
| 台彩（一般瀏覽器、只讀，作為 audit 與 fixture 擷取） | 籃球 → 美國：`美國職籃` 0 場、`NBA盃` 0 場、`美國職籃熱身賽` **2 場**（金州勇士 @ 洛杉磯快艇、猶他爵士 @ 丹佛金塊，10-05 07:00 台灣）。市場：每場 moneyline / spread / total 各 1（共 6），0 無效；上半場玩法當下未提供（熱身賽 coupon `eventMarketCount=3`）。每次 `eventGroup` / `event` 回應 < 1 秒 |
| 台彩（`run_odds.py --source all --dry-run`，Playwright headless Chrome） | **blocked**（Cloudflare，約 27 秒後放棄），0 請求成功 |
| 台彩擷取資料 → dry-run 對正式 DB（唯讀） | parse 2 場 / 6 市場；對應 0 matched、2 unmatched（`preseason_not_tracked`：games 不收季前賽，且正式 DB 尚無 2026-27 賽程）；outcome success（預期內，不告警） |
| 台彩擷取資料 → 對 NBA CDN 2026-27 賽程（唯讀、**另外驗證，非 DB**） | CDN 10/03–10/06 共 9 場（皆熱身賽）；2 場皆 **matched / teams_time**：`0012600066`（GSW @ LAC）、`0012600067`（UTA @ DEN），開賽時間差 0 分、主客方向正確 |
| The Odds API | **not_configured**（無 `ODDS_API_KEY`）；0 請求、0 credits |
| 正式 DB 事後檢查 | `odds_snapshots` 0、`data_sources` 的 twsport / oddsapi 未變、無 0004 表、無殘留測試 schema、predictions / bets 未動 |

## 14. Schema / API compatibility changes

- 純新增欄位 / 表 / view；既有欄位語意不變（`line` 仍是主隊讓分顯示線）；舊 seed 列 `bookmaker` 為 NULL → API 視為來源本身。
- `getLatestOdds`：latest 改以 (source, bookmaker, market) 分組，並排除最新一列非 open 的市場。
- `buildEdgeAnalysis.pick`：同來源多 bookmaker 時以固定偏好順序（pinnacle、draftkings、fanduel、betmgm、williamhill_us、betrivers，其餘字母序）選一家作「國際盤對照」——決定性的顯示選擇，不是最佳盤 / 平均；新增 `international_books`（全部 bookmaker 的最新報價）。
- `shapeOdds` 新增欄位（bookmaker、market_type、period、outcome_set、away_line、draw_odds、model_target、model_threshold、market_status、source_updated_at、last_seen_at）；`/api/odds/:gameId` 的 series 以 `source:bookmaker:market` 分組並帶 `bookmaker`；`/api/system/status` 新增 `last_outcome`。前端未改（只讀既有欄位）。
- **部署順序**：先 `npm run db:migrate:pg`（0004）再部署 API；否則新查詢引用的欄位不存在。正式 DB **尚未套用**（本階段不寫正式 DB）。
- 既有 edge 計算（`src/lib/edge.ts`、讓分 `line_gap`）**未改**，留給 D.2 以 C.5E 機率重做。

## 15. Operational risks

1. **台彩自動擷取目前不可用**（Cloudflare 擋自動化瀏覽器）。這是 D.2 / D.4「以台彩實際賠率」的最大阻礙。可行且合規的方向：(a) 向台彩洽詢官方資料授權 / feed；(b) HAR 手動匯入（已支援，但無法每 30 分鐘）；(c) Cloudflare 規則日後若放寬，job 會自動恢復（退避後每 6 小時重試一次）。不建議也未實作任何繞過。
2. **The Odds API 尚無 key**；免費方案 500 credits/月只夠全場盤每日 4 次；上半場需付費等級才能常態抓取。
3. 正式 DB 尚無 2026-27 賽程（每日 12:00 同步「到台灣明天」為止）→ 開季前的盤口大多會是 `beyond_schedule_horizon`（不告警、不寫入）。
4. 台彩中文隊名目前只實測 4 隊；其餘為常用譯名。查不到 → `unknown_team` 告警，補 `team_aliases.py` 即可（不會誤寫）。
5. 台彩上半場獨贏是**三向**（和局另有賠率）；Odds API 的 `h2h_h1` 是兩向，平手結算依 bookmaker 而定 → D.2 定價時兩者不可混用（`outcome_set` 已區分）。
6. 台彩無來源更新時間 → `source_updated_at` 為 NULL，時間解析度 = 輪詢間隔。
7. `odds_event_links` 以來源事件 id 為鍵；若來源重發新 id，會走一次完整比對（不影響正確性）。
8. 本機 D1 舊狀態需 `npm run db:reset:local` 才有 0004 欄位（`db:migrate:local` 已更新）。
9. Docker 映像未更新 Playwright 瀏覽器（台彩被擋，現階段無意義）；若之後恢復，需 `playwright install chromium` 並設 `TWSPORT_BROWSER_CHANNEL=`。
10. `requirements.txt` 新增 `playwright==1.62.0`（已安裝於 `pipeline/.venv`，使用本機 Google Chrome，未下載 Chromium）。

## 16. Phase D.2 可以開始嗎？

**可以開始，但有前置條件：**
- 資料層（canonical market、正負號、門檻、對應、快照、健康度）已完成並有測試；D.2 的去水 / 定價可直接以 `MarketSnapshot.quotes()` / `v_odds_quotes` + C.5E `probability.py` 實作，並用本報告的 fixture 與合成資料測試。
- 要用**真實**盤口驗證 D.2 前，需要使用者處理：
  1. 套用 `migrations/postgres/0004_phase_d1.sql`（`npm run db:migrate:pg`）。
  2. 在 `pipeline/.env` 設 `ODDS_API_KEY`（免費方案即可），讓國際盤開始累積。
  3. 決定台彩資料的取得方式（官方授權 / 手動 HAR 匯入 / 暫以國際盤開發）。規格書要求 ROI「必須用台彩實際賠率」——在台彩資料穩定取得之前，D.4 的嚴格回測無法開始。

## 附錄 A. 檔案

| 檔案 | 內容 |
|---|---|
| `migrations/postgres/0004_phase_d1.sql`、`migrations/d1/0004_phase_d1.sql`（新） | schema（§1） |
| `pipeline/core/odds/canonical.py`（新） | canonical market、正負號、賠率轉換與驗證、去重雜湊 |
| `pipeline/core/odds/team_aliases.py`（新） | 球隊別名表 |
| `pipeline/core/odds/matching.py`（新） | 來源事件 → games.id |
| `pipeline/core/odds/twsport.py`（新） | 台彩 client / fetch / parser / HAR 匯入 |
| `pipeline/core/odds/oddsapi.py`（新） | The Odds API client / fetch / parser |
| `pipeline/core/odds/store.py`（新） | 快照寫入、對應、抓取紀錄、心跳 |
| `pipeline/core/odds/ingest.py`（新） | job 主流程、outcome 判定、dry-run |
| `pipeline/core/odds/errors.py`（新） | outcome 分類 |
| `pipeline/run_odds.py`（新） | CLI |
| `pipeline/core/scheduling.py`、`scheduler.py`、`core/config.py`、`requirements.txt`、`.env.example` | 排程 / 設定 |
| `src/db/queries.ts`、`src/routes/api.ts`、`package.json` | API（§14）、本機 D1 migration |
| `pipeline/tests/test_odds_*.py`、`tests/fixtures/odds/*`、`tests/conftest.py` | 測試與 fixture |
| `README.md` | Phase D.1 摘要 |
