# Phase D.2 — No-Vig Market Pricing, Model Edge & Expected Value 報告

日期：2026-10-04　範圍：把 D.1 canonical 盤口與 C.5E 預測分佈接起來，對每個合法市場的每個 outcome 計算
raw implied probability、overround、no-vig fair probability、model probability、push probability、model edge、EV。
**未做**：Kelly / 下注金額 / 投注推薦 / best-book 選擇 / bookmaker consensus / ROI backtest；未動 C.5C 模型、C.5E 分佈（dist-v2，μ = 0）；
未以 ROI 或真實盤口結果挑選去水方法；未使用任何 future 資料；**未寫入正式 DB（migration 0005 未套用到 Supabase）**；未 commit。

## 0. 結論摘要

| 項目 | 結果 |
|---|---|
| 舊 edge | `src/lib/edge.ts` + `buildEdgeAnalysis` 有 5 個問題（線差當 edge、獨贏用邏輯迴歸勝率、缺邊時公允機率變 1、只會兩向、TS 另一套數學 + Kelly）→ **全部移除 TS 數學**，edge.ts 刪除 |
| 唯一定價引擎 | `pipeline/core/pricing/`：`novig.py`（去水）→ `engine.py`（`price_market`）→ `alignment.py`（as-of 選擇）→ `job.py`（DB / 排程）。Node API 只讀結果 |
| 去水 | `proportional-v1`（凍結）：同一 snapshot 的全部 outcome 一起正規化；三向不拆兩向 |
| 模型機率 | 一律經 C.5E `line_probability_from_prediction_row()`（用 prediction 列記錄的 artifact 版本）；不另算 normal CDF |
| push / 三向 / 兩向 H1 | 整數線 push 退本金（EV 含 push_prob）；三向和局是 outcome；兩向上半場獨贏結算不明 → 只算 raw / 去水，不算 model / edge / EV |
| 時間對齊 | snapshot `fetched_at ≤ T`、prediction `max(created_at, prediction_as_of) ≤ T` 且有效的最新一筆；違反即 `FutureDataError` |
| 持久化 | 新表 `market_pricing_snapshots`（migration 0005；Workers 不能跑 Python、也不得在 TS 重寫數學）；唯一鍵保證冪等、決定性 |
| 排程 | `market_pricing` 每 5 分鐘（:01/:06/…，台灣），只讀 DB + 本機 artifact |
| API | `odds.pricing`（新）；`edges[]` 保留形狀但標 deprecated、只由 pricing 導出；`kelly_quarter` / `line_gap` 改為 null |
| 測試 | 新增 53 項（純邏輯 45 + 真實 artifact 4 + DB 4）；完整 `pytest` **462 passed**（既有 409 + 新增 53）；Node smoke（本機 D1 seed）**96 / 96** |
| Live data | 正式 DB：0005 未套用、odds 0 筆、未來比賽 0 場、沒有 ml-v2.0 預測 → 目前無可定價市場（預期內） |
| **可否開始 D.3** | **可以**（以 fixture / seed 設計排序與篩選）；真實資料驗證需先套用 0005 並等開季後盤口 / 預測累積（§16） |

---

## 1. Old edge audit

| 位置 | 舊行為 | 問題 | 處置 |
|---|---|---|---|
| `src/lib/edge.ts` `impliedProb` | `1/odds` | 定義正確，但與 Python 並存兩套 | **移除**（Python `novig.raw_implied_prob`） |
| `edge.ts` `devig(home, away)` | 兩向比例去水 | 只會兩向（無法處理台彩上半場三向）；**缺一邊時把該邊當 0 → 另一邊公允機率 = 1.0**；不檢查狀態 / overround 合理性 | **移除**（`novig.proportional_no_vig` + engine 完整性檢查） |
| `edge.ts` `calcEdge` | model − fair | 定義正確，但 model 用的是 `predictions.home_win_prob`（邏輯迴歸），違反 C.5E §9b 決定（獨贏用分差導出機率） | **移除**（engine：P(margin > 0)；邏輯迴歸只作一致性監控） |
| `edge.ts` `kellyFraction` | ¼ Kelly | 下注金額，超出 D.2 範圍；不處理 push；用邏輯迴歸機率 | **移除**；API `kelly_quarter` 保留欄位但為 `null` |
| `edge.ts` `edgeTier` | 標色門檻 | 純呈現 | **保留**（搬到 `src/lib/pricing.ts`，改以 `edge_vs_fair` 判斷） |
| `api.ts` 讓分 / 大小 / 上半場 | `line_gap = 預測值 ± 線`，以 line gap 決定 tier，前端在「Edge」欄顯示「差 x」 | **把線差叫 edge**；沒有機率、沒有 push | **移除**；`line_gap` / `model_value` 改為 `null`；edge 改為機率型 `edge_vs_fair` |
| `api.ts` 獨贏 | 兩邊取 edge 較大者為 `selection`、前端欄名「建議方向」 | 推薦語意 | `edges[]` 保留形狀（向下相容）但標 `deprecated`；詳情頁改為逐 outcome 全列、不再有「建議方向」/ Kelly 欄 |
| `api.ts` 國際盤 | 只挑一家顯示 | D.1 已改為決定性顯示選擇 | 保留（`international` 對照）；定價則每家 bookmaker 各自一組 |
| 時間對齊 | 最新預測（任何 model_version，含 seed / ml-v1.0）× 最新盤口 | 沒有有效性檢查、沒有 as-of 語意 | engine 只用 ml-v2.0 + artifact 版本齊全的預測；as-of 規則（§7） |
| `v_odds_quotes`（D.1 view） | 每 outcome 一列、comparator / threshold | 正確 | 保留供 SQL 檢查；定價用 Python `MarketSnapshot.quotes()`（同一語意，避免 SQL 端再算） |
| `MarketSnapshot` / `NormalizedQuote` | canonical 報價 | 正確 | engine 直接使用；DB 列一律重新經 `make_snapshot` 驗證，並核對已存 `model_threshold` |
| `probability.py` | `line_probability_from_prediction_row` 每次呼叫都重新載入 artifact（sha256 + joblib） | 一次定價多條線很慢 | 新增可選參數 `art=`（版本必須相同，否則 `ValueError`）；預設行為不變 |

## 2. Frozen definitions（`engine.py` / `novig.py`；API 欄位同名）

| 欄位 | 定義 |
|---|---|
| `decimal_odds` | bookmaker 實際提供的十進位賠率 |
| `raw_implied_prob` | `1 / decimal_odds`（含水） |
| `total_raw_implied` | Σ raw_implied_prob（同一 snapshot 的全部 outcome） |
| `market_overround` | Σ raw_implied_prob − 1 |
| `fair_no_vig_prob` | raw_implied_prob / Σ raw_implied_prob |
| `fair_prob_sum` | Σ fair（= 1，檢查值） |
| `model_prob` | C.5E 分佈下「同一 outcome、同一條線」嚴格勝出的機率 P(win) |
| `push_prob` | P(結果 = 線)（整數讓分 / 大小分線，退還本金）；其他 = 0 |
| `loss_prob` | 1 − model_prob − push_prob |
| `edge_vs_fair` | model_prob − fair_no_vig_prob（＝規格的 `model_edge`；**唯一叫 edge 的量**） |
| `ev_per_unit` | model_prob × (decimal_odds − 1) − loss_prob ≡ model_prob × decimal_odds + push_prob − 1 |
| `expected_return` | 1 + ev_per_unit |
| `ev_percent` | 100 × ev_per_unit（例：0.042 → +4.2% / 每單位投注） |

**不叫 edge**：盤口線與點預測的差（line gap，已移除）、model_prob − raw_implied_prob（不輸出）、EV（獨立欄位）。
`model_market_gap` 與 `edge_vs_fair` 數學上相同，不另外輸出重複欄位。

## 3. No-vig method

- `no_vig_method = proportional-v1`（凍結）：`q_i = 1/odds_i`、`p_i = q_i / Σq`。
- 兩向：主 + 客 / 大 + 小 一起；三向：主 + 和 + 客 一起（**不**拆成兩組兩向；`test_three_way_fair_sums_to_one_and_is_not_split_into_two_way`）。
- Shin / power 等方法**未實作**（非必要；若日後加入只能是研究工具，不得用 ROI / 真實盤結果挑選、不得成為預設）。
- 完整性（engine）：只接受**單一 snapshot**、`status = open`、`outcome_set` 的每一邊都有合法賠率；否則 `rejected`（`market_not_open:<status>` / `incomplete_market` / `outcome_set_incomplete`），不計算 fair / edge / EV。一個 `OddsInput` = 一列，結構上不可能把不同時間的兩邊拼起來（`test_never_combines_sides_from_different_snapshots`）。
- 合理性（依實測 fixture：台彩兩向 16.3–17.3%、三向 27.7%；美國 bookmaker 4.3–5.3%；寬鬆設定、不針對個別市場）：

| 條件 | 處置 |
|---|---|
| 非有限值 / 賠率 ≤ 1 | 拒絕（`overround_non_finite` / `invalid_price`） |
| overround < 0（同一 bookmaker 同一時刻的套利） | 拒絕（`negative_overround`） |
| 兩向 > 60%、三向 > 90% | 拒絕（`implausible_overround`，幾乎必然是解析錯誤） |
| 兩向 > 25%、三向 > 40% | 照算 + 警示 `overround_high` |
| < 0.5% | 照算 + 警示 `overround_very_low` |
| Σ fair 與 1 差 > 1e-9 | 拒絕（`fair_sum_not_one`，防呆） |

## 4. Push semantics

| 規則（`settlement_rule`） | 市場 | push |
|---|---|---|
| `moneyline_ot_included` | 全場獨贏兩向 | 不可能（NBA 有延長賽）；模型 push ≠ 0 → `ModelProbabilityError`（介面錯誤，不定價） |
| `half_line_no_push` | x.5 讓分 / 大小 | 0；模型 push ≠ 0 → 錯誤 |
| `integer_line_push_refund` | 整數讓分 / 大小 | 退還本金：EV = P(win)·(odds − 1) − P(loss) |
| `three_way_draw_outcome` | 三向上半場獨贏 | 沒有 push；和局是 outcome |
| `quarter_line_split_settlement` | x.25 / x.75 | 亞洲盤拆半注結算 → `unsupported_settlement`（不猜） |

測試以精確公式驗證（`test_integer_spread_push_ev_formula_exact`）：P(win) 0.47 / P(push) 0.06 / P(loss) 0.47、賠率 1.91 →
EV = 0.47 × 0.91 − 0.47 = **−0.0423**；≠ push 當輸（−0.1023）、≠ 條件化後套原賠率（−0.0450）。
讓分 0（pk）是整數線，但全場分差不會是 0 → 模型 push 自然為 0（真實 artifact 測試涵蓋）。

## 5. 2-way / 3-way handling 與 H1 moneyline settlement policy

- **三向上半場獨贏（台彩 ref 60）**：home = P(h1_margin > 0)、draw = P(h1_margin = 0)、away = P(h1_margin < 0)，三邊一起去水。
  主 / 客的 P(=0) 歸入 loss（和局開出時主 / 客都輸、不退款）；和局 EV = P(=0)·odds − 1。和局**不是** push（`test_draw_is_an_outcome_not_a_push`）。
- **兩向上半場獨贏（The Odds API `h2h_h1`）**：半場可平手，各 bookmaker 的平手結算（push / 輸 / draw-no-bet…）沒有可靠 metadata →
  `unsupported_settlement`（`h1_two_way_tie_settlement_unknown`）：保存 raw / overround / 去水，`model_prob` / `edge_vs_fair` / `ev_per_unit` 為 null，連模型都不查詢（`test_two_way_h1_moneyline_without_settlement_rule_has_no_ev`）。
- **全場三向獨贏**（常規時間 1X2；目前來源都沒有，防呆）：模型分差含延長賽、target 不同 → `unsupported_market`。

## 6. Model probability mapping

engine 只依 D.1 canonical 的 `comparator` / `model_threshold` 對應，不再做任何正負號運算：

| 市場 | outcome | 機率 |
|---|---|---|
| 全場獨贏 | home / away | P(margin > 0) / P(margin < 0) |
| 讓分 主讓 5.5（顯示線 −5.5 → threshold +5.5） | home / away | P(margin > 5.5) / P(margin < 5.5) |
| 讓分 客讓 3.5（主隊顯示線 +3.5 → threshold −3.5） | home / away | P(margin > −3.5) / P(margin < −3.5) |
| 大小 225.5 | over / under | P(total > 225.5) / P(total < 225.5) |
| 上半場讓分 / 大小 | 同上 | target = h1_margin / h1_total |
| 三向上半場獨贏 | home / draw / away | P(h1 > 0) / P(h1 = 0) / P(h1 < 0) |

- production 機率來源 `RowModelProbabilities` → `probability.line_probability_from_prediction_row(row, target, threshold, art=…)`；
  `test_model_probabilities_come_from_c5e_interface` 以真實 artifact 驗證 10 種 outcome 與 `predict_*_probability` **逐位元相同**。
- 機率來源回傳的 `artifact_version` 必須等於 prediction 列記錄的版本，否則拒絕。
- 資料品質旗標只寫進 diagnostics（C.5E：不改變分佈）。

## 7. Timestamp alignment（`alignment.py`；live 與歷史重建共用）

給定 analysis_as_of = T：
- **盤口**：每個 series (game, source, bookmaker, market) 取 `fetched_at ≤ T` 的最新一列；最新一列非 open → 該市場在 T 不可定價（不退回較舊的 open 列，與 API 一致）。
- **預測**：`available_at = max(created_at, features_json.prediction_as_of_utc) ≤ T`，且有效（`ml-v2.0`、有 `artifact_version` / `profile` / 四個點預測）的最新一筆；early / final / refresh 只看時間（不會拿 final 去定價更早的盤口）。
- **模型**：用該列的 `artifact_version`（版本目錄不可變），CURRENT 換版不影響舊預測（`test_stored_artifact_version_is_used_not_current`）。
- `price_market` 收到晚於 T 的 snapshot / prediction → `FutureDataError`。
- 輸出 `analysis_as_of = max(odds.fetched_at, prediction.available_at)`：此分析最早可成立的時點（決定性，與何時重算無關）。
- DB 讀取先以 SQL `fetched_at ≤ T` / `created_at ≤ T` 過濾（之後的列根本不讀進來），再由純函式選擇。

測試：`test_future_prediction_cannot_price_earlier_odds`、`test_future_odds_cannot_be_priced_at_earlier_time`、`test_later_odds_and_predictions_do_not_alter_earlier_analysis`、
`test_latest_valid_prediction_at_analysis_time_is_selected`、`test_analysis_as_of_is_deterministic_effective_time`、`test_reconstruction_uses_only_data_available_at_as_of`（DB）。

## 8. EV definition

- 一律用**實際提供的賠率**（不是 `1/fair`）：`test_ev_uses_offered_odds_not_fair_odds`（1.91 → +5.05%；誤用公允賠率 2.0 會得 +10%）。
- edge 與 EV 不可互換：台彩 1.75 / 1.75、模型 0.55 → edge +5.0 pp、EV **−3.75%**（`test_edge_and_ev_are_not_interchangeable`）。
- 不轉成下注金額。

## 9. Bookmaker separation

每個 (source, bookmaker, market, line) snapshot 各自去水、各自定價；不平均、不 consensus、不挑最佳盤 / 最佳價、不推薦。
`test_multiple_bookmakers_and_lines_are_priced_separately`：Pinnacle −5.5、DraftKings −5.5、FanDuel −6（整數線、push）、台彩 −5.5 → 4 組獨立結果。
台彩與國際盤不平均（`source=twsport` 與 `source=oddsapi, bookmaker=…` 並列）。

## 10. Diagnostics

- `diagnostics.ml_consistency`（全場獨贏）：分差導出 P(主勝) vs 儲存的邏輯迴歸 `home_win_prob`；差 > 5 pp → `warning: true` + `warnings: ["ml_logistic_margin_divergence"]`，**不改**模型機率。
- `warnings`：`overround_high` / `overround_very_low` / `ml_logistic_margin_divergence`（seed 另有 `seed_fixture`）。
- `diagnostics`：點預測、分佈名稱 / 中心 / 尺度、資料品質旗標。

## 11. Persistence decision

**需要新表**，理由：`model_prob` 需要 C.5E artifact（分佈狀態、版本、push 語意），只有 Python pipeline 能載入；Node API 跑在 Cloudflare Workers，
不能執行 Python，且規格禁止在 TS 再寫一份 normal CDF / 去水 / EV。→ Python 計算、寫入；Node 只讀。

`migrations/postgres/0005_phase_d2.sql`（D1：`migrations/d1/0005_phase_d2.sql`）純新增 `market_pricing_snapshots`：
一列 = odds snapshot × prediction × outcome × pricing_version；含 `odds_snapshot_id`、`prediction_id`（NULL = 當時無有效預測，`market_only`）、`analysis_as_of`、
`model_version`、`artifact_version`、`distribution_version`、`no_vig_method`、`pricing_version`、市場層（overround、Σraw、Σfair、status、settlement_rule）與
outcome 層全部欄位、`warnings` / `diagnostics`、`computed_at`（唯一非決定性欄位，只作稽核）。

- 冪等：唯一索引 `(odds_snapshot_id, COALESCE(prediction_id, 0), side, pricing_version)` + `ON CONFLICT DO NOTHING`；已存在的列永不改寫。
- 決定性：同一 snapshot、同一 prediction、不可變的 artifact 版本 → 同樣輸出（`test_pricing_job_persists_deterministically_and_idempotently` 比對存值與重算值）。
- 不寫入：最新 snapshot 非 open（API 也不顯示）、模型機率取不到（artifact 載入失敗 → 該場整場不寫，避免把暫時性錯誤永久存成 `market_only`，回報 error）。
- **D.4 歷史重建**：不依賴這張表。`reconstruct_game(game_id, T)` / `python run_pricing.py --game-id N --as-of T` 只用 T 以前的 `odds_snapshots` + `predictions` + 當時 artifact，以同一個 `price_market` 重算。這張表是 API 的計算快取與稽核紀錄。
- 注意：這條 Supabase pooler 連線的 `extra_float_digits = 0`，float8 回讀為 15 位有效數字 → 存值與記憶體重算可能差 1e-15 相對誤差（不影響結果；D.4 比對請用容差）。

**Job**：`pricing_job(now)`：未開賽、未來 48 小時（與 Odds API 視窗一致）的比賽 → 每個 series 最新 snapshot × 當時最新有效預測 → 寫入；
`data_sources.market_pricing` 心跳。排程 `market_pricing`：每 5 分鐘（台灣 :01/:06/…，在台彩 :03/:33、Odds API :40、預測寫入之後幾分鐘內）。
0005 未套用 → job 不定價、回報 warn（不會寫入任何定價列）。CLI：`python run_pricing.py [--dry-run]`。

## 12. API changes（向下相容）

| 端點 / 欄位 | 變更 |
|---|---|
| `GET /api/games/*`、`/api/games/:id` → `odds.pricing`（**新**） | `{pricing_version, no_vig_method, definitions, status: priced/partial/not_priced/no_odds/unavailable, markets[], unpriced_odds_snapshot_ids[]}`；每個 market：source、bookmaker、market、line、status、status_reason、settlement_rule、total_raw_implied、market_overround、fair_prob_sum、odds_fetched_at、analysis_as_of、prediction_id、`prediction_is_latest`、model / artifact / distribution 版本、warnings、diagnostics、outcomes[]（decimal_odds、raw_implied_prob、fair_no_vig_prob、model_prob、push_prob、loss_prob、edge_vs_fair、ev_per_unit、expected_return、ev_percent、display_line、model_threshold、comparator） |
| `odds.edges[]`（deprecated） | 形狀保留；只由 pricing 導出（台彩、`priced`、非三向）；`edge = edge_vs_fair`、新增 `ev_per_unit` / `push_prob` / `odds_snapshot_id` / `deprecated: true`；`kelly_quarter`、`line_gap`、`model_value` 為 `null`；新增 `odds.edges_deprecated` 說明 |
| `prediction.id` | 新增（對照 `pricing.markets[].prediction_id`） |
| 0005 未套用 | 查詢失敗被攔下 → `pricing.status = 'unavailable'`、`edges = []`，端點不會 500 |
| `src/lib/edge.ts` | 刪除；`src/lib/pricing.ts` 只做分組 / 命名 / 標色（`edgeTier`），不計算 |
| 前端 | 詳情頁「盤口定價」表：來源 / 玩法 / 選項 / 盤線 / 賠率 / 原始隱含 / 去水公允 / 模型 / Push / Edge / EV / 抽水，逐 bookmaker、逐 outcome；移除「建議方向」與 ¼Kelly。總覽卡片：讓分 / 大小不再顯示「差 x」，改顯示 edge_vs_fair；獨贏模型機率改用分差導出（未定價時退回邏輯迴歸） |
| seed | `seed.template.sql` 新增 40 列定價（明日 3 場的最新盤口 × seed 預測），由 `python -m core.pricing.seed_fixture` 以同一個 engine 產生（模型機率 = C.5E `predict_line_probability`(本機 production artifact, seed 點預測)），標 `seed_fixture`；`db:migrate:local` 加入 0005 |

**部署順序**：先 `npm run db:migrate:pg`（0005）→ 部署排程器（含 `market_pricing`）→ 部署 API（API 在 0005 前部署也不會壞，只是 `pricing.status = unavailable`）。

## 13. Validation examples

**手算（測試以精確公式驗證，非 snapshot）**

| 市場 | 賠率 | 模型 | raw | overround | fair | edge | EV |
|---|---|---|---|---|---|---|---|
| 兩向（規格例） | 1.91 / 1.91 | 主 0.55 | 0.52356 / 0.52356 | 4.71% | 0.5 / 0.5 | +0.05 | 0.55 × 1.91 − 1 = **+0.0505** |
| 兩向非對稱 | 1.80 / 2.10 | — | 0.5556 / 0.4762 | 3.17% | 0.5385 / 0.4615 | — | — |
| 整數讓分 主 −5 | 1.91 / 1.91 | win 0.47 / push 0.06 / loss 0.47 | — | 4.71% | 0.5 / 0.5 | −0.03 | 0.47 × 0.91 − 0.47 = **−0.0423** |
| 台彩高水 | 1.75 / 1.75 | 主 0.55 | 0.5714 | 14.29% | 0.5 | **+0.05** | 0.55 × 1.75 − 1 = **−0.0375** |
| 三向上半場 | 1.70 / 10.0 / 1.70 | 0.60 / 0.035 / 0.365 | — | 27.65% | Σ = 1 | — | 和局 0.035 × 10 − 1 = −0.65 |

**production artifact（`ml-v2.0+20261004T0339Z.20261004T033948Z`，dist-v2）、合成盤口**（點預測取 C.5E 報告 PHI@NYK：分差 +7.05、總分 223.1、上半場分差 +4.35；邏輯迴歸 0.714）

| 市場 | 賠率 | 規則 | outcome | raw | fair | 模型 | push | edge | EV |
|---|---|---|---|---|---|---|---|---|---|
| 獨贏 | 1.40 / 2.95 | ot_included | 主 | 0.7143 | 0.6782 | 0.7002 | 0 | +0.0220 | −0.0197 |
| 讓分 主 −7 | 1.91 / 1.91 | integer push | 主 / 客 | 0.5236 | 0.5000 | 0.4997 / 0.4707 | 0.0296 | −0.0003 / −0.0293 | −0.0160 / −0.0713 |
| 讓分 主 −6.5（台彩水位） | 1.75 / 1.75 | half line | 主 | 0.5714 | 0.5000 | 0.5293 | 0 | +0.0293 | −0.0737 |
| 大小 223 | 1.91 / 1.91 | integer push | 大 / 小 | 0.5236 | 0.5000 | 0.4915 / 0.4872 | 0.0214 | −0.0085 / −0.0128 | −0.0400 / −0.0481 |
| 上半場三向 | 1.55 / 10.0 / 2.45 | three-way | 主 / 和 / 客 | 0.6452 / 0.1000 / 0.4082 | 0.5594 / 0.0867 / 0.3539 | 0.6351 / 0.0331 / 0.3318 | 0 | +0.0757 / −0.0536 / −0.0221 | −0.0157 / −0.6685 / −0.1871 |

（分差導出 P(主勝) 0.7002 vs 邏輯迴歸 0.714：差 1.4 pp < 5 pp，不警示。）

## 14. Tests

新增（`tests/test_pricing.py` 44、`tests/test_pricing_artifact.py` 8、`tests/test_odds_scheduler.py` +1 → 共 53，其中 DB 4）：

| 要求 | 測試 |
|---|---|
| No-vig：2 / 3 向總和、已知例、overround、缺邊、停售 | `test_two_way_fair_probabilities_sum_to_one`（5 組）、`test_three_way_fair_sums_to_one_and_is_not_split_into_two_way`、`test_known_two_way_example_from_spec`、`test_overround_and_fair_known_asymmetric_example`、`test_incomplete_market_is_rejected_not_priced`、`test_suspended_or_closed_market_is_rejected`（3 種）、`test_never_combines_sides_from_different_snapshots` |
| Model mapping | `test_full_game_moneyline_mapping`、`test_home_favourite_spread_mapping`、`test_away_favourite_spread_mapping`、`test_total_over_under_mapping`、`test_h1_spread_and_total_use_h1_targets`、`test_h1_three_way_moneyline_mapping_and_no_vig`、`test_threshold_sign_convention_partitions_outcomes`、`test_model_probabilities_come_from_c5e_interface`（真實 artifact） |
| Push | `test_integer_spread_push_ev_formula_exact`、`test_integer_total_push_ev`、`test_half_point_line_has_no_push`、`test_full_game_moneyline_has_no_push`、`test_expected_value_function_requires_complete_probabilities`、`test_real_distribution_push_and_ev_semantics`（真實 artifact） |
| Three-way | `test_h1_three_way_moneyline_mapping_and_no_vig`、`test_draw_is_an_outcome_not_a_push` |
| Unsupported settlement | `test_two_way_h1_moneyline_without_settlement_rule_has_no_ev`、`test_quarter_line_and_regulation_three_way_are_unsupported`、`test_no_prediction_is_market_only` |
| EV | `test_known_two_way_example_from_spec`（正）、`test_negative_ev_example`、`test_ev_uses_offered_odds_not_fair_odds`、`test_edge_and_ev_are_not_interchangeable` |
| Bookmakers / lines | `test_multiple_bookmakers_and_lines_are_priced_separately` |
| 時間對齊 | `test_future_prediction_cannot_price_earlier_odds`、`test_future_odds_cannot_be_priced_at_earlier_time`、`test_later_odds_and_predictions_do_not_alter_earlier_analysis`、`test_latest_valid_prediction_at_analysis_time_is_selected`、`test_analysis_as_of_is_deterministic_effective_time`、`test_artifact_version_mismatch_is_refused`、`test_stored_artifact_version_is_used_not_current`、`test_reconstruction_uses_only_data_available_at_as_of`（DB） |
| Diagnostics | `test_logistic_vs_margin_derived_consistency_warning`、`test_ml_consistency_uses_stored_logistic_probability`、`test_vig_sanity_checks`、`test_high_vig_market_is_priced_with_warning` |
| 持久化 / job（DB） | `test_pricing_job_persists_deterministically_and_idempotently`、`test_market_only_then_priced_when_prediction_arrives`、`test_non_open_latest_snapshot_is_not_priced` |
| Serialization | `test_serialization_is_json_safe_and_rows_cover_schema`、`test_legacy_rows_without_d1_columns_are_normalized_via_canonical` |
| 排程 | `test_market_pricing_every_5_minutes_after_odds_and_predictions` |
| API 向下相容 | Node smoke（§15） |

## 15. Results

- **完整 `cd pipeline && pytest`：462 passed（既有 409 + 新增 53；910 秒）**（DB 測試使用暫存 schema `c5a_test_*`，結束後 DROP；已確認無殘留、正式 `public` 未動）。
- **Node smoke（`npm run test:api`）：96 通過 / 0 失敗**（原 90 項 + 6 項 D.2 定價檢查；其中 1 項改為「Kelly 已停用為 null」）。
  環境：`npm run build` → `npm run db:reset:local`（0001 / 0002 / 0004 / 0005 + seed，含 40 列定價）→ `wrangler pages dev dist --local --env-file <只含 SESSION_SECRET> --binding DATABASE_URL=`，
  先確認 `/api/system/status` 的 `db_driver = d1`（不連 Supabase）。另以瀏覽器確認詳情頁「盤口定價」表正確渲染（台彩 / oddsapi 分列、整數線 231 的 push 2.1%、EV 與 edge 分欄）。

## 16. Live-data readiness

正式 DB（2026-10-04，唯讀檢查）：`market_pricing_snapshots` **不存在**（0005 未套用）、`odds_snapshots` 0 筆、未來未開賽比賽 0 場、predictions 只有歷史的 `ml-v1.0` / `elo-v1.0`（皆不可定價，正確被排除）。
`python run_pricing.py --dry-run` → 「尚未套用 0005，不定價」。依約定未造任何 production 資料。

開始產生真實定價需要：
1. **使用者套用 migration**：`DATABASE_URL=… npm run db:migrate:pg`（只新增一張表）。
2. 排程器部署後（含 `market_pricing`），等每日賽程同步帶入 2026-27 例行賽、`predict_early` 產生 ml-v2.0 預測、Odds API 48 小時視窗出現 NBA 盤口 → 每 5 分鐘自動定價。
3. 台彩自動擷取仍被 Cloudflare 擋（D.1 §5）；HAR 匯入的快照同樣會被定價。

## 17. Limitations

1. **整數線的 edge_vs_fair 有結構性偏差**：model_prob 是 P(win)（不含 push），而去水公允機率在兩邊間總和為 1 → 兩邊 edge 相加 = −push_prob。整數線請以 EV（已正確處理 push）判斷；這是凍結定義的直接結果，未另做條件化調整。
2. 整數讓分 / 大小分一律假設「push 退還本金」（兩個來源皆適用的業界慣例；台彩籃球多為 .5 線）。若日後看到台彩整數線且規則不同，需在 `settlement_rule` 增加來源別規則。
3. 四分之一線、兩向上半場獨贏、全場三向獨贏 → 不定價（不猜）。
4. proportional 去水不處理 favourite–longshot bias；Shin / power 未實作（研究用途才可加，不得以結果挑選）。
5. 分差導出的獨贏機率 calibration slope ≈ 1.1（C.5E §9b，略保守、未校正）；2026-27 為前瞻驗證期。
6. 定價最多落後 5 分鐘：新盤口 / 新預測寫入後，API 以 `unpriced_odds_snapshot_ids`、`prediction_is_latest` 標示尚未重算。
7. 快照只在輪詢時觀測（D.1 §9）；`last_seen_at` 的時效判斷（某時點報價是否仍掛牌）留給 D.4。
8. seed 的定價列是 fixture（假預測 × 真實分佈），只供 UI / smoke test。
9. Supabase pooler 連線 `extra_float_digits = 0`（§11）。

## 18. Phase D.3 可以開始嗎？

**可以。** 定義、去水、模型對應、push / 三向 / 不支援結算、時間對齊、持久化與 API contract 都已完成並有測試；D.3 可直接讀 `market_pricing_snapshots` /
`odds.pricing.markets` 設計排序與篩選（用 seed / fixture 開發）。以真實盤口驗證前需要：套用 0005、部署排程器、開季後累積盤口與 ml-v2.0 預測。
本階段到此停止，未開始 D.3。

## 附錄 A. 檔案

| 檔案 | 內容 |
|---|---|
| `pipeline/core/pricing/novig.py`（新） | 去水（proportional-v1）與合理性界線 |
| `pipeline/core/pricing/engine.py`（新） | 定義、結算規則、模型機率對應、edge / EV、`price_market` |
| `pipeline/core/pricing/alignment.py`（新） | DB 列 → canonical、as-of 選擇、`price_game_as_of` |
| `pipeline/core/pricing/job.py`（新） | DB 讀寫、`pricing_job`、`reconstruct_game` |
| `pipeline/core/pricing/seed_fixture.py`（新） | seed 定價列產生器 |
| `pipeline/run_pricing.py`（新） | CLI |
| `pipeline/core/production/probability.py` | `line_probability_from_prediction_row(..., art=None)`（可選參數，預設行為不變） |
| `pipeline/core/scheduling.py` | `market_pricing` job |
| `migrations/postgres/0005_phase_d2.sql`、`migrations/d1/0005_phase_d2.sql`（新） | `market_pricing_snapshots` |
| `src/lib/pricing.ts`（新）、`src/lib/edge.ts`（刪除） | 序列化 / 標色 |
| `src/db/queries.ts`、`src/routes/api.ts` | 讀取定價、`odds.pricing`、deprecated `edges[]` |
| `public/static/js/game-detail.js`、`games.js`、`common.js` | 定價表 / 卡片 edge |
| `seed/seed.template.sql`、`package.json`、`scripts/smoke-test.mjs` | seed 定價、本機 migration、smoke 檢查 |
| `pipeline/tests/test_pricing.py`、`test_pricing_artifact.py`（新）、`test_odds_scheduler.py`、`conftest.py` | 測試 |
| `README.md` | Phase D.2 摘要 |
