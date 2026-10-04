# Phase D.4 — Backtest Engine, Settlement & Prospective Performance Tracking 報告

日期：2026-10-04　範圍：建立 leakage-safe、可重現的策略執行與績效評估層：
odds / prediction as-of 重建 → decision → 理論執行 → settlement → bankroll ledger → yield / bankroll return / drawdown / log-growth → prospective paper tracking。
**未做**：任何依 ROI 的調整（risk-v1、timing、EV 門檻）、best-book / line shopping / consensus、C.5C / C.5E / dist-v2 / proportional-v1 變更、
造 historical odds、以現在盤口回填過去、自動下注、寫入 `bets`、D.5；**未對 production Supabase 套用 0007、未寫入任何 production 資料**（只做唯讀查詢）；未 commit。

## 0. 結論摘要

| 項目 | 結果 |
|---|---|
| 證據分級 | A. 歷史**模型**評估：有（C.5C / C.5E walk-forward OOS，五季）　B. 歷史**投注** ROI：**沒有任何證據**（正式 DB 0 筆盤口）　C. Prospective：從 2026-27 開始記錄（本階段建好） |
| execution-v1（凍結） | T = 開賽 − 60 分；analysis_as_of = T（不是 job 執行時間）；Asia/Taipei betting day；day_start_bankroll 為當日 stake 基準；依 T 排序、同 T 為一個批次；已 committed stake 占用 8% 單日額度；D.3 actionable 且 final > 0 才執行 |
| Scope | primary = `twsport:twsport`（台彩）；`oddsapi:<單一 bookmaker>` 只算 international_market_diagnostic；不做 best-book / consensus / 平均 |
| As-of | 直接呼叫 D.2 `price_game_as_of` + D.3 `size_pricings`（純函式），不讀 `market_pricing_snapshots` / `bet_sizing_snapshots`；**修正一個 hindsight 漏洞**：`last_seen_at > T` 時改用 T 以前的輪詢紀錄判定新鮮度（§4.3） |
| Settlement | settle-v1：ML（含 OT）/ 讓分 / 大小 / 上半場讓分 / 上半場大小 / 上半場三向（和局是 outcome）；只有 `final` 才結算；缺上半場比分、比分不一致、取消 / 延期 / 改期 → `ungradable`（不猜 void） |
| Ledger | migration **0007**：`paper_strategy_days` / `paper_strategy_decisions` / `paper_strategy_bets`；no-bet 也保存；一場一 strategy 一筆；decision 不可改 / 刪、結算後不可改（DB trigger）；與 `bets` 完全分離 |
| Scheduler | `paper_strategy` 每 5 分（:02/:07/…）；冪等、advisory lock、heartbeat、dry-run、CLI；晚 > 15 分 → `decision_window_missed`（記錄，不補做）；與 pricing / sizing job 互不影響 |
| Evidence guard | odds 列分 observed / seed_fixture / synthetic_fixture / unverified；歷史引擎只用 observed；沒有 → `historical_evidence_available = false`、metrics = null（不是 ROI = 0） |
| Fixture validation | 2 betting day、9 場：win / loss / push / H1 和局 / 每種 no-bet / game cap / daily cap / stale / 缺預測；bankroll 路徑與 Fraction 手算完全一致（1 → 2243/2200 → 2243/2200 × 1.01）；**validation_only，不是 ROI 證據** |
| 真實歷史結果 | 正式 DB 唯讀查詢：2024-10-22…2026-06-30 共 2,643 場、twsport 盤口 0、Odds API 盤口 0、T 時點可用預測 0 → `evidence_class = none` |
| 測試 | 新增 81 項（純邏輯 69、DB 12）；完整 `pytest` **625 passed**；Node smoke（本機 D1）**106 / 106** |
| Migration | 0007（Postgres + D1）新增；**未套用 production**（§18） |
| **D.5 readiness** | 程式面可以開始（§20）；但「策略有沒有 edge」目前**沒有任何投注證據**，2026-27 prospective ledger 需累積 ≥ 30 個有注單的 betting day 才有 bootstrap CI |

---

## 1. Data availability audit

### 1.1 程式碼

| 元件 | 狀態 / D.4 用法 |
|---|---|
| `core/jobs/backtest.py`（`run_backtest.py`） | Elo walk-forward **模型**回測（accuracy / log loss / Brier → `model_metrics`）；不涉及盤口。名稱不衝突：D.4 CLI 為 `run_strategy_backtest.py`、模組 `core/execution/` |
| `model_metrics.sim_roi` / 績效頁「模擬 ROI」 | 欄位只有 seed 資料填值；Python 從不寫入。**D.4 不寫這個欄位**（避免把 fixture / 模型模擬混成投注績效）；D.4 指標用 `yield` / `bankroll_return`，不叫 ROI |
| `games` | `status` 只會是 scheduled / live / final（`GAME_STATUS_MAP`、ESPN map）；全場 `home_pts/away_pts`、四節 + OT、`home_h1/away_h1` |
| `odds_snapshots`（D.1） | 時間序列；`fetched_at` = 實際觀測時間（HAR 匯入 = 擷取時間）；內容不變只推 `last_seen_at`（**只存最後一次**確認）；`fetch_run_id` → `odds_fetch_runs` |
| `odds_fetch_runs` | 每次輪詢一列：source、fetched_at、outcome、`diagnostics.events[]`（每個事件的 game_id / matched / n_snapshots，最多 60 筆） |
| `predictions`（C.5D） | 無唯一鍵；`created_at` = DB 寫入時間；`features_json.prediction_as_of_utc / artifact_version / profile / prediction_kind`；final 預測在開賽前 75→70 分寫入（T-60 前可用） |
| `market_pricing_snapshots` / `bet_sizing_snapshots` | D.2 / D.3 live job 的 cache / 稽核；**D.4 不讀**（歷史重建不能拿最新 cache 假裝當時知道） |
| `bets` | 使用者手動下注；D.4 不讀不寫 |
| D.2 `price_game_as_of` / `reconstruct_game` | 純函式 as-of 定價（fetched_at ≤ T、available_at ≤ T、該列 artifact 版本） |
| D.3 `size_pricings` | 記憶體定價 → risk-v1 sizing（static portfolio；不含先後順序） |
| Artifact | `versions/<v>/manifest.json`（`trained_at_utc`、bundle sha256，`load_version` 驗證）；版本目錄不可變 |

### 1.2 正式 DB（2026-10-04，唯讀查詢）

| 表 | 內容 |
|---|---|
| `schema_migrations` | 0001–0006（0006 已套用） |
| `games` | 6,605 場，全部 `final`；2021-22 … 2025-26（含 play-in / playoffs）；全場、四節、上半場比分齊全，上半場 = Q1 + Q2 全數一致；**未來比賽 0 場**（2026-27 賽程尚未同步） |
| `odds_snapshots` / `odds_fetch_runs` | **0 / 0** |
| `market_pricing_snapshots` / `bet_sizing_snapshots` / `bets` | 0 / 0 / 0 |
| `predictions` | `ml-v1.0` 2,631、`elo-v1.0` 2,643——全部在 **2026-10-01** 一次寫入（比賽結束很久之後）；`ml-v2.0`（可定價的 production 版本）0 筆 |

### 1.3 三種證據能支持到什麼程度（不混用）

| 證據 | 支持程度 | 依據 |
|---|---|---|
| **A. Historical model evaluation** | **可以**。C.5C / C.5E 以 walk-forward 重建每場 G 在 T-60 的 production 輸入，五季 OOS 評估點預測與機率校準 | 只用比分與 as-of 特徵，不涉及盤口；屬模型準確度，不是投注績效 |
| **B. Historical betting ROI** | **完全不能**。沒有任何真實觀測的盤口；DB 內的 ml-v1.0 / elo-v1.0 預測寫入時間在比賽之後，as-of 規則下對任何歷史決策都不可用；D.4 的歷史引擎對 2024-25 / 2025-26 回報 `historical_evidence_available = false` | 禁止用現在的盤口倒填過去；fixture 只是引擎驗證 |
| **C. Prospective betting tracking** | **從 2026-27 開始**（本階段建好 ledger + 排程）。前提：0007 套用、排程器部署、`odds_twsport` 實際取得台彩快照（目前 Cloudflare 擋自動擷取 → 只有 HAR 匯入的 snapshot 會進入）；沒有 snapshot 的比賽一律記 `no_odds` | 每場在 T-60 一次決策，不事後重做 |

## 2. execution-v1（凍結；`core/execution/policy.py`）

| 參數 | 值 | 說明 |
|---|---|---|
| `GAME_T_MINUS_MINUTES` / `decision_offset` | **60 分** | decision_time T = scheduled_tipoff − 60 分 = analysis_as_of |
| `betting_day_timezone` | Asia/Taipei | 與 D.3 `sizing.engine.betting_day` 同一函式 |
| `starting_bankroll_units` | 1.0 | 策略一律以 normalized bankroll 計算 |
| `max_evaluation_lag` | 15 分 | prospective：job 晚於 T 超過 → `decision_window_missed`（排程每 5 分 → 3 次機會） |
| `reschedule_tolerance` | 60 分 | 決策後開賽時間變動超過 → `ungradable`（bookmaker 改期規則未驗證） |
| `result_known_after_tipoff` | 4 小時 | **只用於歷史重建**：前一日注單何時視為已結算（決定是否回到下一日 day_start）；prospective 用實際 `settled_at` |
| `risk_policy_version` | risk-v1 | 綁定；risk-v1 參數（¼ Kelly、2% / 3% / 8%、2 × 輪詢間隔 stale）一個都不改 |

- 自 2026-10-04 起凍結，**在看到任何 ROI 之前**定義。`ExecutionPolicy` 是 frozen dataclass；`assert_registered()`：寫入 ledger 前版本字串必須對應登記參數（`replace(EXECUTION_V1, decision_offset=15 分)` 冒充 execution-v1 → 拒絕）；沒有環境變數 / CLI 參數可改 timing、EV 門檻、bookmaker 混合（測試掃描 CLI flag 與 `os.environ`）。
- `strategy_id` = `execution-v1/risk-v1/sizing-v1/pricing-v1/kelly-push-v1/<source>:<bookmaker>`；任何版本或 scope 改變 = 不同策略。
- 其他 timing（T-30、T-15、closing line…）只能是新的 execution policy 版本，不得冒充 execution-v1。

### 2.1 T-60 理由

- C.5C：開賽前 1 小時重算主要改善分差；T-15 − T-60 log loss −0.0013 [−0.0033, +0.0005]、分差 MAE −0.010，**皆不顯著**。
- C.5D：production final 預測就是 T-60 profile（每 5 分檢查開賽前 75→70 分寫入），T-60 時 final 預測已可用。
- 因此 T-60 是「模型輸入已是 production final、又不追求無增益的更晚時點」的決策點；不是看 ROI 選的。

## 3. Source / bookmaker scope

| scope | kind | evidence_label | 意義 |
|---|---|---|---|
| `twsport:twsport` | primary | `taiwan_sports_lottery_strategy` | 本專案嚴格 ROI 目標（台彩實際賠率） |
| `oddsapi:<bookmaker>` | diagnostic | `international_market_diagnostic` | 各 bookmaker 分開；**永遠不是台彩證據** |

- `StrategyScope` 拒絕 `*` / `all` / `best` / `consensus`、`oddsapi:twsport`、`twsport:<非 twsport>`；oddsapi 必須指定單一 bookmaker。
- 盤口在**定價前**就依 scope 過濾（台彩策略看不到 Pinnacle 價格；反之亦然），測試確認。
- 不做 best-book、line shopping、consensus、bookmaker averaging。

## 4. Exact as-of reconstruction（`core/execution/asof.py`）

### 4.1 每場 G、T = tipoff − 60 分

| 資料 | 規則 |
|---|---|
| odds | 單一 scope、`fetched_at ≤ T`；每個 series 取 T 時最新列（D.2 `latest_snapshots_as_of`；最新列非 open → 該市場在 T 不可下注，不退回較舊的 open 列） |
| freshness | risk-v1 規則（last_seen_age > 2 × 輪詢間隔 → stale），但 last_seen 必須是 **T 以前**的確認（§4.3） |
| prediction | `available_at = max(created_at, prediction_as_of) ≤ T` 的最新有效預測（D.2 `select_prediction`；不看 kind） |
| artifact | 該預測列記錄的 `artifact_version`；manifest `trained_at_utc ≤ T`；`load_version` 驗證 sha256（被改動 → `artifact_unavailable`） |
| pricing | D.2 `price_game_as_of`（純函式） |
| sizing | D.3 `size_pricings`（純函式；risk-v1） |

`market_pricing_snapshots` / `bet_sizing_snapshots` 一律不讀。重建結果附 `pricing_fingerprint` / `sizing_fingerprint`（sha256），同樣輸入 → 同樣 fingerprint。

### 4.2 Quote availability gate（固定順序）

`no_odds`（T 時沒有任何 snapshot）→ `market_not_open`（全部 suspended / closed）/ `invalid_market_data` → `unsupported_market`（只有兩向上半場獨贏、四分之一線、全場三向）
→ `stale_odds`（所有支援的 open 市場都 stale）→ `no_prediction` → `artifact_unavailable`。任一成立 → no_bet，**不會**用 T+10 分才出現的 quote 或 T+5 分的預測回填。

### 4.3 修正：`last_seen_at` 的 hindsight

D.1 的 `last_seen_at` 只保存**最後一次**確認。D.3 歷史重建遇到 `last_seen_at > T` 時把它截到 T（視為 T 時新鮮）——但那代表 **T 之後**的輪詢又確認了同一內容；例如輪詢在 T−2h 到 T 之間全部失敗、T+3 分才恢復，T 時點真實知道的是「2 小時沒確認」（應該 stale）。
D.4 `confirmed_last_seen_as_of(row, T, runs)`：

- `last_seen_at ≤ T` → 原值（所有確認都在 T 以前）。
- `last_seen_at > T` → T 以前最後一次「同來源、outcome ∈ success/partial、`diagnostics.events` 記錄這場比賽 matched 且有市場」的輪詢時間；找不到 → 保守退回 `fetched_at`（只會低估新鮮度）。

這是 as-of **輸入**的修正，risk-v1 的新鮮度規則本身不變（`quote_is_stale` 與 D.3 `_freshness` 同一規則，測試核對）。live 的 D.3 job 不受影響（即時執行時 last_seen 必然 ≤ now）。
限制：事件層級的涵蓋判斷無法分辨「Odds API 某 bookmaker 某次輪詢暫時下架、之後原價恢復」；events 只存 60 筆（找不到 = 不確定 → 不採用）。

### 4.4 Prospective job 晚執行

decision 的 analysis_as_of 永遠是 T；`evaluated_at` / `evaluation_lag_seconds` 只是稽核欄位。例：開賽 10:00、T = 09:00、job 09:02 執行 → 只用 `fetched_at ≤ 09:00` 的盤口、`available_at ≤ 09:00` 的預測、09:00 以前的輪詢確認；09:01 才出現的盤口 / 預測都不用（DB 測試）。

## 5. Decision semantics（每場一定有一筆 decision）

| `decision_status` / `no_bet_reason` | 條件 |
|---|---|
| `bet` | 至少一個 D.3 actionable、final_stake_fraction > 0 的 outcome 在剩餘額度內被執行 |
| `no_odds` / `market_not_open` / `invalid_market_data` / `unsupported_market` / `stale_odds` / `no_prediction` / `artifact_unavailable` | §4.2 gate |
| `portfolio_cap` | D.3 有 actionable outcome，但當日剩餘額度 = 0 |
| `stale_odds`（sizing 後） | 有正 EV 但報價 stale（D.3 `stale_quote`） |
| `mutually_exclusive_positive_kelly` | D.3 互斥規則拒絕 |
| `no_positive_ev` | 所有可定價 outcome EV ≤ 0 |
| `data_unavailable` | 機率 / 定價不合法 |
| `decision_window_missed` | prospective：job 晚於 T 超過 15 分（記錄，不補做） |

- 一個 strategy × 一場比賽只有一個 execution-v1 decision；之後出現更好的賠率、更新的預測都**不會**重做（UNIQUE + advisory lock + 不可變 trigger）。
- no-bet 一律保存（含原因、T 時看到的 snapshot id、預測 id、artifact、每個 outcome 的 D.3 結果摘要），避免只留下有下注的樣本（selection bias）。
- 執行條件 = D.3：`actionable` 且 `final_stake_fraction > 0`（EV > 0、無最低 EV 門檻、不用 edge、資料品質旗標不改 stake）。

## 6. Sequential day execution（execution-v1 portfolio controller；`core/execution/engine.py`）

D.3 是某一時刻的 static portfolio；controller 加上先後順序（不改 risk-v1 定義）：

```
每個 betting day d（Asia/Taipei）：
  day_start(d) = 起始 + Σ（第一個 T 以前已結算）損益 − Σ（仍未結算的前日）stake       ← 前一日全結算時 = day_end(d−1)
  依 decision_time 排序；同一 T 的比賽 = 一個批次（一起交給 D.3 size_pricings，與 game_id 無關）
  批次：D.3 static final（含同場 3%、批次內 8%）→ batch_total
        remaining = 0.08 − committed_d（已 committed 的 stake 占用額度）
        scale = 1（batch_total ≤ remaining）| remaining / batch_total | 0（remaining = 0 → portfolio_cap）
        stake_fraction = D.3 final × scale；stake_units = day_start(d) × stake_fraction
        committed_d += Σ stake_fraction
  當日中途已結算的比賽不釋放、不增加額度（controller 沒有任何結算輸入）
  全部結算後 day_end(d) = day_start(d) + Σ 當日損益 → 次日基準
```

- 等比例縮放（不挑最高 EV、不排序）；單筆 2% / 同場 3% 由 D.3 保證；單日 8% 由 controller 跨批次保證（fixture：當日 Σ stake_fraction = 0.08）。
- 測試：day_start 當日固定；earlier committed stake 占用；後面批次被縮放；同日 G101 / G102 全贏或全輸 → G103 stake 逐位元相同、只影響次日 day_start；次日 bankroll 含前日結算。

## 7. Bankroll semantics

- Canonical：`starting_bankroll_units = 1.0`；所有 decision / stake / profit 都是 normalized 單位。
- `--starting-bankroll 10000` 只把結果乘上 10000（`display` 區塊）；選擇邏輯、stake_fraction 完全相同（測試：10000 倍 bankroll → decision 相同、金額等比例、yield / bankroll_return 相同）。
- 不寫真實金額、不寫 `bets`。
- **未結算（pending / ungradable）的 stake 不回到下一日 day_start**（不猜輸贏；保守、不會用不存在的錢放大後面的注碼）；`bankroll_status = provisional_unresolved`、另列 `unresolved_stake_units`。

## 8. Settlement rules（settle-v1；`core/execution/settlement.py`）

依**下注當時**的 outcome 規格（`model_target / model_threshold / comparator / settlement_rule`、當時賠率）結算，不重查目前盤口線。

| 市場 | target | 規則 |
|---|---|---|
| 全場獨贏 | margin（含 OT） | > 0 主勝 / < 0 客勝；分差 0 = 比分不一致 → ungradable |
| 全場讓分 | margin vs 當時 threshold | 半分線 win / loss；整數線 = threshold → push |
| 全場大小 | total | over / push / under |
| 上半場讓分 | h1_margin（home_h1 − away_h1） | 同上 |
| 上半場大小 | h1_total | 同上 |
| 上半場三向 | h1_margin vs 0 | 和局 → `settled_draw_win`（outcome，不是 push）；和局時主 / 客 = loss |
| 兩向上半場獨贏、四分之一線、全場三向、其他 | — | 永遠不結算成有效注單（ungradable: unsupported_settlement）；D.3 本來就不會 size |

- 只有 `games.status = 'final'` 才結算；scheduled / live → `pending`。
- `ungradable`（含 exclusion reason）：缺上半場比分、全場 ≠ 四節 + OT、上半場 ≠ Q1 + Q2、上半場 > 全場、cancelled / postponed、決策後開賽時間移動 > 60 分、半分線卻剛好等於線。
- `void`：只有登記在 `VOID_RULES` 的明確通用規則才使用；**execution-v1 沒有登記任何規則**（取消 / 延期 / 改期的退款規則依 bookmaker 與時限而定，未驗證 → ungradable，不自行假定）。
- 帳務：win / draw_win → stake × (odds − 1)；loss → −stake；push / void → 0；pending / ungradable → None（不入帳）。本金已在 bankroll 內，不重複加回。
- Prospective：結算後 `settled_*` / `void` 不可再改（DB trigger）；事後比分修正不影響已結算注單；`pending` / `ungradable` 每次排程重新檢查。

## 9. Metrics（`core/execution/metrics.py`）

| 指標 | 定義 |
|---|---|
| n_decisions / n_bet_decisions / n_no_bet / action_rate | decision 數 / 有下注的 decision / no-bet / n_bet_decisions ÷ n_decisions；`no_bet_reasons` 計數 |
| n_bets、wins（含 draw_wins）、losses、pushes、void、ungradable、pending | 依 settlement_status |
| total_staked = turnover | Σ stake（**只計 graded**：win / loss / push / draw_win） |
| net_profit | Σ 已結算損益 |
| **yield** | net_profit ÷ total_staked（turnover-based） |
| **bankroll_return** | ending_bankroll ÷ starting_bankroll − 1 |
| ending_bankroll | starting + net_profit − unresolved_stake |
| log_bankroll_growth | ln(ending ÷ starting)；另有每個有注單 betting day 的平均 |
| avg stake fraction / avg decimal odds / avg EV / stake-weighted EV | |
| expected_profit vs realized | Σ stake × ev_per_unit vs 實際；差值、模型標準差 √Σ stake²(p_w·b² + p_l − EV²)、z |
| max drawdown | bankroll 路徑（起始 + 每日 day_close）最大 peak-to-trough（單位與 %） |
| worst / best betting day | 依 day_profit |

yield 與 bankroll_return 不叫 ROI（測試確認輸出鍵不含 `roi`）。expected vs realized 只是 calibration / variance 診斷，**不用來調模型或 risk-v1**。

Subgroups（market、source、bookmaker、month、side、EV bin、odds range、early season（開季 30 天內）vs later）一律標 `exploratory_descriptive_only`：
只供描述，**不得**據以加 strategy filter、最低 EV 門檻或任何參數；「事後按 EV bin 分組」只是報告，不是 production strategy。

## 10. Bootstrap

- 抽樣單位 = **betting day**（同場、同日 bets 相關；不做逐 bet IID）。每次重抽 n_days 個 day block：yield* = Σprofit* ÷ Σstake*；bankroll return* = Π(1 + day_return*) − 1。
- 輸出：yield point / 95% percentile CI、bankroll return 分位數（5/25/50/75/95%）、P(return < 0)；預設 10,000 次、固定 seed（20261004）。
- **最低 30 個有 graded bet 的 betting day**，否則 `insufficient_sample`（不輸出 CI）。依據：cluster bootstrap 在 cluster < ~30 時 percentile 區間覆蓋率明顯不足（few-clusters problem），且日報酬厚尾（單日上限 8%）——統計穩定性門檻，不是看結果決定。
- 限制：day block 視為可交換，忽略跨日序列相關。
- 測試：每日一贏一輸的對沖日 → 以日重抽 CI 退化為 0（逐 bet 重抽則不會）；29 天 → insufficient；同 seed 結果相同、不同 seed 不同。

## 11. Paper ledger design（migration 0007）

| 表 | 鍵 / 不可變性 | 內容 |
|---|---|---|
| `paper_strategy_days` | PK (strategy_id, betting_day)；UPDATE / DELETE → exception | day_start_bankroll、established_at（第一個 T）、起始 bankroll、prior_resolved_profit、prior_unresolved_stake |
| `paper_strategy_decisions` | UNIQUE (strategy_id, game_id)；UPDATE / DELETE → exception；CHECK（no_bet ⇔ 有原因） | game、scheduled_tipoff、decision_time（T）、evaluated_at、lag、execution / risk / sizing / pricing / kelly 版本、scope、evidence_label、source / bookmaker、status、no_bet_reason、blockers、odds_snapshot_ids、prediction_id / available_at、artifact / model / distribution 版本、pricing / sizing fingerprint、每個 outcome 的 sizing 摘要、day_start、committed_before、remaining、execution_scale、總 stake |
| `paper_strategy_bets` | UNIQUE (decision_id, odds_snapshot_id, side)；結算後 UPDATE → exception；執行欄位任何時候不可改；DELETE → exception | outcome / line / 賠率 / 機率 / EV / D.3 final / execution_scale / stake_fraction / stake_units / expected_profit、settlement_status / reason / version、actual_value、比分、profit、settled_at |

- FK 對 games / predictions / odds_snapshots 用 `RESTRICT`（稽核紀錄不會因清資料而消失；D.2 / D.3 cache 表是 CASCADE）。
- 與 `bets` 完全分離（名稱、表、strategy_id、無 user_id）；D.5 若要實際下注 / bankroll sync，需另建且不得與 paper 混用。
- D1 版本 `migrations/d1/0007_phase_d4.sql`（SQLite trigger `RAISE(ABORT)`），`db:migrate:local` 已加入；Node 端目前不讀這些表（無 API 變更）。

## 12. Prospective scheduler（`core/execution/ledger.py`）

- `paper_strategy`：`CronTrigger(minute="2-59/5", Asia/Taipei)` → `paper_strategy_scheduled`：先 `paper_decision_job`，再 `paper_settlement_job`（各自 try；決策失敗不影響結算），心跳 `data_sources.paper_strategy`；由 `scheduling.guarded` 包住；與 `market_pricing`（D.2 / D.3）分開、不讀寫其表。
- `paper_decision_job(now)`：`T ≤ now`、T 在 [max(2026-10-01, now − 48h), now]、尚未有 decision 的比賽 → 依 betting day / T 分批 → 交易內 `pg_advisory_xact_lock` → day row（不存在才以第一個 T 的可用 bankroll 建立，之後凍結）→ 批次重建 + D.3 + controller → 寫 decision + bets（`ON CONFLICT DO NOTHING`）。`now − T > 15 分` → `decision_window_missed`。
- `paper_settlement_job(now)`：pending / ungradable → settle-v1；terminal 才寫 `settled_at = now`；`WHERE settlement_status IN ('pending','ungradable')` + trigger 雙重保護。
- day_start 計算只計 `settled_at ≤ 該日第一個 T` 的結算（嚴格 as-of）。
- CLI：`python run_paper.py [--dry-run] [--now ISO] [--scope twsport|oddsapi:<book>] [--settle-only] [--report [--starting-bankroll N]]`；`--report` = prospective performance（ledger → 同一套 metrics / bootstrap / subgroups，evidence_class = `prospective_paper`）。
- 0007 未套用時：job 回報 warn、不寫入。
- 部署順序：`npm run db:migrate:pg`（0007）→ 部署排程器。

## 13. Historical engine（`core/execution/backtest.py`）

```
python run_strategy_backtest.py --source twsport --from 2026-10-20 --to 2026-12-31 [--summary] [--out f.json]
python run_strategy_backtest.py --source oddsapi --bookmaker pinnacle --from … --to …     # international_market_diagnostic
python run_strategy_backtest.py --fixture                                               # validation_only
```

- 只讀 DB（不寫任何表）；期間 = Asia/Taipei betting day。
- 只用 **observed** odds 列；預測只用 production（`ml-v2.0`、有 artifact、非 seed / fixture）且 as-of 合法者。
- 沒有 observed 列 → `historical_evidence_available = false`、`metrics = null`（不是 ROI = 0、不造 fixture）。
- 有 → 與 prospective 同一個 `simulate`（as-of → D.2 → D.3 → controller → settle-v1）→ metrics、bootstrap、subgroups、evidence。
- prospective decision 與之後的歷史重建一致（DB 測試：decision、pricing / sizing fingerprint、stake 完全相同，即使 T 之後同內容輪詢把 `last_seen_at` 推到 T 之後）。

## 14. Evidence classification（`core/execution/evidence.py`）

| odds 列 provenance | 條件 |
|---|---|
| `observed` | 有 `content_hash`、`normalizer_version`；`fetch_run_id` 指向同來源、outcome ∈ success / partial、`fetched_at` 相同的 `odds_fetch_runs`；`last_seen_at ≥ fetched_at` |
| `seed_fixture` | 無 content_hash / normalizer_version（seed、舊格式）或 raw_json 標 seed |
| `synthetic_fixture` | raw_json 標 fixture / synthetic（D.4 validation slate） |
| `unverified` | 有 D.1 欄位但沒有對應的成功抓取紀錄 |

| run `evidence_class` | 意義 |
|---|---|
| `historical_observed` | 所有用到的 snapshot 都是 observed（否則 `EvidenceError`，程式層 guard） |
| `fixture_validation` | validation_only = true；statement 明寫「NOT historical betting performance」 |
| `prospective_paper` | paper ledger |
| `none` | 期間內沒有 observed 盤口 |

`is_taiwan_sports_lottery_evidence` 只有 source = twsport 且 observed / prospective 時為 true；Odds API 結果 statement 明寫「not Taiwan Sports Lottery evidence」。

## 15. Fixture validation result（`core/execution/fixture.py`；validation_only）

模型機率為合成值（FixtureModel），價格 / 去水 / EV / Kelly / risk-v1 / controller / 結算全部走 production 程式碼。

| 場 | T（UTC） | 決策 | 執行（D.3 final → 執行後） | 結果 | 損益 |
|---|---|---|---|---|---|
| **Day 1（台灣 2026-10-22）day_start = 1** | | | | | |
| G101 | 22:00 | bet | ML 主 2.00（p .60、¼K .05 → 2%）、讓分 −4 主 2.00（p .44/.20/.36、full .10 → 2%）；同場 4% → ×0.75 → 各 **1.5%** | 110–106（分差 4） | ML **+0.015**、讓分 **push 0** |
| G102 | 22:00（同批次） | bet | 大分 220.5（2%）、上半場三向「和」12.0（p .10、full 1/55 → ¼ **1/220**） | 108–102（210）、上半場 50–50 | 大分 **−0.02**、和局 **+11/220 = +0.05** |
| | | | 批次 0.0545… ≤ 8% → 全額；committed = 0.05 + 1/220 | | |
| G103 | 00:30 | bet | ML 2% + 讓分 −5.5 2% → 同場 3%；剩餘 0.03 − 1/220 = **7/275** → scale **28/33** → 各 **7/550** | 100–108 | 兩筆 **−7/550** |
| G104 | 01:00 | no_bet `portfolio_cap` | D.3 actionable，但剩餘 0（當日已 8%） | | |
| G105 | 01:30 | no_bet `no_prediction` | 預測 T+5 分才寫入（不回填） | | |
| | | | day_profit = 0.015 − 0.02 + 0.05 − 14/550 = **43/2200**；day_end = **2243/2200 ≈ 1.0195455** | | |
| **Day 2（台灣 2026-10-23）day_start = 2243/2200** | | | | | |
| G106 | 22:00 | no_bet `stale_odds` | 報價最後確認 T−3h（台彩上限 60 分；EV 為正但 stale） | | |
| G107 | 22:30 | bet | ML 主 2.00、p .52 → EV .04 → full .04 → ¼ **1%** → stake = 2243/220000 | 112–100 | **+0.0101955** |
| G108 | 23:00 | no_bet `no_positive_ev` | 兩邊 EV < 0 | | |
| G109 | 23:30 | no_bet `no_odds` | 盤口 T+10 分才出現（不回填） | | |
| | | | day_end = 2243/2200 × 1.01 ≈ **1.0297409** | | |

另含：G101 在 T+10 分有更佳賠率 2.50（不使用）。程式輸出與手算 Fraction 逐項一致（`test_fixture_wagers_settlement_and_hand_computed_bankroll_path`、`test_bankroll_conservation`）。
Fixture metrics（僅示範引擎）：9 decisions、4 bet decisions、7 bets（3 勝含 1 和局、3 負、1 push）、yield 0.3297、bankroll return 0.0297、max drawdown 0——**這不是 ROI 證據**。

## 16. Real historical evidence currently available

正式 DB 唯讀執行：

| 指令 | 結果 |
|---|---|
| `run_strategy_backtest.py --source twsport --from 2024-10-22 --to 2026-06-30` | 2,643 場、odds 0、T 時點可用預測 0 → `evidence_class = none`、`historical_evidence_available = false`、`metrics = null` |
| `--source oddsapi --bookmaker pinnacle --from 2025-10-21 --to 2026-06-30` | 1,322 場、odds 0 → 同上 |

**目前沒有任何可信的 historical betting ROI**；任何 2024-25 / 2025-26 的投注績效數字都不存在、也不應被製造。

## 17. Tests / results

新增 `tests/test_execution.py`（69，純邏輯）、`tests/test_execution_db.py`（12，暫存 schema + 真實合成 artifact）；`conftest.py` TRUNCATE 清單加入 paper 表。

| 要求 | 測試 |
|---|---|
| As-of：T-60、T 後資料不用、晚執行仍 T、quote / prediction 不回填、artifact 不可變 | `test_decision_time_is_exact_t_minus_60`、`test_exact_t60_reconstruction_equals_direct_d2_d3_pure_functions`、`test_data_after_t_never_used`（刪掉 / 加入極端未來資料 → decision 逐欄相同）、`test_job_running_after_t_still_reconstructs_exact_t`、`test_no_later_quote_backfill`、`test_no_later_prediction_backfill`、`test_last_seen_after_t_is_not_hindsight_confirmation`、`test_stale_rule_matches_d3_freshness`、`test_artifact_must_exist_before_t_and_load`、`test_tampered_artifact_is_unavailable`（DB 檔）、`test_late_job_uses_only_data_at_exact_t`（DB） |
| Decision | `test_every_game_has_exactly_one_decision`、`test_no_positive_ev_and_stale_and_positive_sizing`、`test_only_actionable_positive_stakes_are_executed`、`test_no_minimum_ev_threshold`、`test_paper_decisions_persisted_idempotent_and_never_replaced`（DB）、`test_missed_decision_window_recorded_not_backfilled`（DB） |
| Sequential exposure | `test_day_start_fixed_during_day_and_committed_stakes_consume_budget`、`test_same_decision_time_is_one_batch_independent_of_game_id`、`test_same_day_settlement_does_not_increase_later_stakes`、`test_next_day_bankroll_includes_prior_day_settlement`、`test_unresolved_prior_stake_not_added_back`、`test_available_bankroll_formula`、`test_day_start_frozen_and_next_day_uses_settled_bankroll`（DB）、`test_unsettled_prior_stake_excluded_from_next_day_start`（DB） |
| Settlement | `test_settlement_outcomes`（18 組：ML / 讓分 / 大小 / 上半場 / 三向）、`test_h1_three_way_draw_is_outcome_not_push`、`test_missing_or_inconsistent_h1_is_ungradable`（3）、`test_non_final_pending_and_cancel_postpone_ungradable_no_void_guess`、`test_unsupported_markets_never_settle_as_valid_bets`、`test_impossible_results_are_ungradable` |
| Accounting | `test_payouts`、`test_fixture_wagers_settlement_and_hand_computed_bankroll_path`、`test_bankroll_conservation`、`test_multi_bet_daily_pnl`、`test_normalized_vs_display_bankroll_proportionality` |
| Metrics | `test_metrics_exact`、`test_yield_and_bankroll_return_are_distinct`、`test_max_drawdown_exact`、`test_drawdown_uses_day_close_path`、`test_subgroups_are_descriptive_only` |
| Bootstrap | `test_bootstrap_resamples_days_not_bets`、`test_bootstrap_insufficient_sample`、`test_bootstrap_deterministic_seed` |
| Evidence | `test_evidence_classification`、`test_fixture_marked_validation_only_never_historical`、`test_historical_guard_rejects_non_observed_and_no_evidence`、`test_oddsapi_results_never_labelled_twsport`、`test_prediction_provenance`、`test_scope_filters_other_bookmakers_before_pricing`、`test_backtest_without_observed_odds_has_no_evidence`（DB）、`test_oddsapi_backtest_is_international_diagnostic_only`（DB）、`test_prospective_decision_equals_historical_reconstruction`（DB） |
| Persistence | `test_paper_decisions_persisted_idempotent_and_never_replaced`、`test_settlement_then_immutable`、`test_pending_bet_execution_fields_immutable`、`test_dry_run_writes_nothing`（全部 DB；含 `bets` 未被寫入） |
| Policy / 無回饋 | `test_execution_v1_is_frozen`、`test_no_timing_or_ev_threshold_knobs_in_cli_or_env`、`test_strategy_scope_single_bookmaker_and_labels`、`test_engine_has_no_result_feedback_into_selection` |
| Scheduler | `test_paper_job_scheduled_every_5_minutes_separately_from_pricing`、`test_paper_scheduled_isolates_decision_failure_from_settlement` |

結果：

- `pytest tests/test_execution.py`：**69 passed**；`pytest tests/test_execution_db.py`：**12 passed**（暫存 schema `c5a_test_*`，結束 DROP，不碰 production `public`）。
- 完整 `cd pipeline && pytest`：**625 passed**（既有 544 + 新增 81；818 秒）
- Node smoke（`npm run test:api`）：**106 通過 / 0 失敗**。環境：`npm run build` → `npm run db:reset:local`（0001/0002/0004–0007 + seed）→ `wrangler pages dev dist --local --env-file <只含 SESSION_SECRET> --binding DATABASE_URL=`，先確認 `/api/system/status` 的 `db_driver = d1`；本機 D1 確認三張 paper 表存在、SQLite trigger 阻擋 UPDATE。

## 18. Migration status

- 新增 `migrations/postgres/0007_phase_d4.sql`、`migrations/d1/0007_phase_d4.sql`（純新增三張表 + trigger；不改既有表）。
- 正式 DB：0001–0006 已套用；**0007 未套用**（依約定不寫 production；需要時由使用者執行 `npm run db:migrate:pg`）。未套用時 paper job 回報 warn、不寫入；其他 job 不受影響。

## 19. Operational risks

1. **台彩資料**：自動擷取仍被 Cloudflare 擋；沒有 HAR 匯入的比賽全部是 `no_odds`。HAR 手動匯入的報價 60 分後即 stale——必須在 T-60 前 60 分鐘內匯入，否則 `stale_odds`。prospective 台彩 coverage 可能很低，ledger 會如實呈現。
2. **HAR 晚匯入**：`fetched_at` = 擷取時間，若在 job 跑完 T 決策後才匯入，decision 不會重做（設計如此）；若在 T 與 job 執行之間匯入（≤ 15 分），會被採用——屬實際觀測、時間 ≤ T，但操作上應避免在看到臨場資訊後才匯入。
3. **新鮮度解析度 = 輪詢間隔**；`last_seen` 只存最後一次 → 歷史重建依賴 `odds_fetch_runs.diagnostics.events`（事件層級、最多 60 筆）；無法證明時保守判 stale。
4. **開賽時間是 decision 當時的 `games.date_utc`**；games 表不保存改期歷史。歷史重建用目前值；prospective ledger 存 scheduled_tipoff，改期 > 60 分 → ungradable。
5. **未結算 stake**：ungradable 不入帳且不回到 bankroll（保守）；長期 ungradable 會讓 bankroll 低估，需人工檢查原因（目前沒有 void 規則）。
6. **歷史重建的結算時間假設**（開賽 + 4h）只影響「前一日 stake 是否回到次日 day_start」；NBA 賽程上前一日最後一場與次日第一個 T 通常相隔 > 12 小時。
7. **相關性**：仍只靠 risk-v1 的 hard cap（同場 3%、單日 8%）；先到的批次先占額度（first-come），不做 ranking。
8. **樣本量**：bootstrap 需 ≥ 30 個有注單的 betting day；在那之前只有點估計，且必須標 provisional。expected vs realized 只作診斷。
9. **ledger 不可變**：錯誤的 decision 無法修改，只能以新的 execution policy 版本（新 strategy_id）重新開始；FK RESTRICT 會阻擋刪除被引用的 games / predictions / odds。

## 20. D.5 readiness

**程式面可以開始 D.5**：as-of 重建、決策、執行、結算、帳務、metrics、paper ledger、prospective 排程都已完成並有測試；D.5 可在此之上做 actual-bet exposure（`bets` 已下注金額計入同場 / 單日上限）、bankroll sync、使用者介面。

**限制**：
- 目前**沒有任何**投注績效證據（歷史 0、prospective 尚未開始）；D.5 不得以「策略有 edge」為前提設計推薦語意。
- 0007 需先套用、排程器需部署，2026-27 prospective tracking 才會開始累積；台彩 snapshot 取得方式（HAR / 授權）仍是瓶頸。
- D.5 的實際下注紀錄必須與 paper ledger 分開；不得把 paper 結果寫入 `bets` 或反之。

本階段到此停止，未開始 D.5。

## 附錄 A. 檔案

| 檔案 | 內容 |
|---|---|
| `pipeline/core/execution/policy.py`（新） | execution-v1（凍結、登記檢查）、StrategyScope、strategy_id |
| `pipeline/core/execution/asof.py`（新） | T-60 as-of 重建、`confirmed_last_seen_as_of`、artifact registry、availability gate、no-bet reasons |
| `pipeline/core/execution/engine.py`（新） | controller（`execute_batch`）、Wager / GameDecision / DayLedger、`available_bankroll`、`simulate` |
| `pipeline/core/execution/settlement.py`（新） | settle-v1、payout |
| `pipeline/core/execution/metrics.py`（新） | metrics、drawdown、day-block bootstrap、descriptive subgroups |
| `pipeline/core/execution/evidence.py`（新） | provenance、evidence class、historical guard |
| `pipeline/core/execution/ledger.py`（新） | paper ledger DB、decision / settlement job、排程進入點、prospective performance |
| `pipeline/core/execution/backtest.py`（新） | 歷史引擎（DB 載入 + simulate + evidence） |
| `pipeline/core/execution/fixture.py`（新） | deterministic synthetic validation slate + 手算 EXPECTED |
| `pipeline/run_strategy_backtest.py`、`pipeline/run_paper.py`（新） | CLI |
| `pipeline/core/scheduling.py` | 新增 `paper_strategy` job |
| `migrations/postgres/0007_phase_d4.sql`、`migrations/d1/0007_phase_d4.sql`（新）、`package.json` | paper ledger 表；本機 D1 migrate 加入 0007 |
| `pipeline/tests/test_execution.py`、`test_execution_db.py`（新）、`conftest.py` | 測試 |
| `README.md` | Phase D.4 摘要 |
