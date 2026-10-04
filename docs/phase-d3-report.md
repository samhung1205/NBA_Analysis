# Phase D.3 — Bet Qualification, Risk Controls & Kelly Sizing 報告

日期：2026-10-04　範圍：在 D.2 定價結果（`market_pricing_snapshots`）上建立 push-aware Kelly、凍結的保守 fractional Kelly 政策（risk-v1）、
單筆 / 同場 / 單日 exposure 上限、qualification / rejection 原因、決定性的組合縮放與持久化。
**未做**：ROI backtest、以 ROI 調任何參數、best-book 選擇、consensus、recommendation ranking、multivariate Kelly、寫入 `bets`；
未改 C.5C 模型、dist-v2、proportional-v1；未使用 C.5D confidence_factor；**未對 production Supabase 套用 0006、未寫入任何 production 資料**；未 commit。

## 0. 結論摘要

| 項目 | 結果 |
|---|---|
| 舊 Kelly 殘留 | TS 端已在 D.2 移除；唯一殘留 `pipeline/core/config.py` 的 `kelly_fraction`（環境變數 `KELLY_FRACTION` 可調、無人使用）→ **移除**（含 `.env.example`） |
| Kelly 數學（`kelly-push-v1`） | f* = (p_win·b − p_loss) / (b·(p_win + p_loss)) = EV / (b·(1 − p_push))；EV ≤ 0 → 0；只用 P(win) / P(push) / P(loss) / 實際賠率，**不用 edge** |
| risk-v1（凍結） | full Kelly × 0.25 → 單筆 ≤ 2% → 同場合計 ≤ 3% → 單日（Asia/Taipei）合計 ≤ 8%；超過按比例縮放 |
| Qualification | 10 種狀態；mathematically eligible 與 actionable 分開；兩者都不是推薦 |
| 資料品質旗標 | 只加 warning，不改 stake（測試證明逐位元相同） |
| 互斥 | 同一 snapshot 互斥 outcome 有 > 1 個正 Kelly → 整個市場拒絕 |
| 新鮮度 | last_seen_age > 2 × 來源輪詢間隔（台彩 60 分、Odds API 12 小時）→ `stale_quote`（不 actionable）；未知來源 → 不 actionable |
| 持久化 | 新表 `bet_sizing_snapshots`（migration 0006；不改 0005 歷史列）；唯一鍵 (定價列, policy, sizing 版本, portfolio_key) 冪等 |
| 排程 | 併入 `market_pricing`：同一 T 先定價、再 sizing |
| API / UI | `odds.sizing`、`pricing.markets[].outcomes[].sizing`、`GET /api/sizing?date=`；詳情頁新增「理論注碼」表；TS 不含任何 Kelly / cap / scaling 運算 |
| 測試 | 新增 82 項（純邏輯 76（含參數化）、DB 5、排程 1）；完整 `pytest` **544 passed**（既有 462 + 新增 82）；Node smoke（本機 D1）**106 / 106** |
| Production | 0005 已套用；odds 0 筆、定價 0 筆、未來比賽 0 場、bets 0 筆；0006 **未套用**（§15） |
| **D.4 readiness** | 可以開始（§17）；真實 ROI 需等開季後實際累積盤口 |

---

## 1. Audit

### 1.1 D.2 欄位：Kelly 真正需要 vs 只作診斷

| 欄位（`market_pricing_snapshots`） | D.3 用途 |
|---|---|
| `decimal_odds` | **Kelly 輸入**（b = odds − 1；實際提供的賠率） |
| `model_prob` / `push_prob` / `loss_prob` | **Kelly 輸入**（p_win / p_push / p_loss；三者和必須 = 1） |
| `ev_per_unit` | **一致性檢查**：必須 = p_win·b − p_loss（差 > 1e-9 → `invalid_probability:ev_mismatch`）；EV ≤ 0 → Kelly 0 |
| `status` / `status_reason` / `settlement_rule` | **Qualification gate**（priced / market_only / unsupported_* / rejected；支援的結算規則白名單） |
| `odds_snapshot_id`、`prediction_id`、`analysis_as_of`、`odds_fetched_at`、`artifact_version`、`pricing_version` | 身分 / 時間對齊 / 互斥分組 / artifact 檢查 |
| `market_type` / `period` / `outcome_set` / `line` / `display_line` / `side` | 防呆（兩向上半場獨贏、四分之一線再檢查一次）、顯示 |
| `diagnostics.data_quality_flags`、`warnings` | **只**轉成 warning |
| `edge_vs_fair`、`fair_no_vig_prob`、`raw_implied_prob`、`market_overround`、`total_raw_implied`、`fair_prob_sum`、`expected_return`、`ev_percent`、`diagnostics.ml_consistency` | **只作診斷**（`edge_vs_fair` 複製到 sizing 列供市場分歧檢視；不進入任何 stake 計算） |

`odds_snapshots.last_seen_at`（D.1）→ 新鮮度；`games.date_utc` → betting day、已開賽判斷。

### 1.2 舊 1/4 Kelly 殘留

| 位置 | 狀態 | 處置 |
|---|---|---|
| `src/lib/edge.ts` `kellyFraction` | D.2 已刪除 | — |
| API `edges[].kelly_quarter` | D.2 起為 `null`（deprecated 形狀） | **維持 null**（向下相容）；D.3 數字在 `odds.sizing`，說明文字指向新欄位 |
| 前端 | D.2 已移除 ¼Kelly 欄 | D.3 新增唯讀「理論注碼」表（不計算） |
| `pipeline/core/config.py` `kelly_fraction = KELLY_FRACTION env`（0.25） | 未被任何程式使用，但讓 multiplier 可由部署環境變數改動 | **移除**；`.env.example` 改為說明 policy 凍結於 `core/sizing/policy.py`（`test_kelly_fraction_env_setting_removed`） |
| `bets` 表 / `api.ts` 的 `stake × odds` | 個人下單紀錄的 payout 結算（使用者自填金額） | 不是 sizing，不動；D.3 **不寫入 bets** |
| 總覽頁「值得關注機會」（依 edge tier 計數） | 推薦語意 | 改為中性「edge ≥ 3% 的 outcome（市場分歧，非推薦）」 |

### 1.3 其他 audit

- **D.2 定價輸出**：每個 outcome 一列、市場層欄位重複；`priced` 列的 P(win)+P(push)+P(loss)=1、EV 定義與本階段一致 → 可直接當 Kelly 輸入。
- **D.1 新鮮度**：`fetched_at` = 我們收到該價格的時間（價格首次觀測）；內容沒變只把 `last_seen_at` 往後推（只會往後）；輪詢間隔 = `core/odds/ingest.SOURCES`（台彩 30 分 :03/:33、The Odds API 6 小時 00/06/12/18:40）。舊列（seed）`last_seen_at` 為 NULL。
- **時間**：UI（`src/lib/time.ts`）、排程（`TPE`）都以 Asia/Taipei 為主要時間 → betting day 採台灣日期（§7）。

## 2. Kelly derivation（凍結：`pipeline/core/sizing/kelly.py`，`kelly-push-v1`）

單一 outcome，下注 bankroll 比例 f，b = decimal_odds − 1。push 退還本金（報酬 0）：

```
G(f)  = p_win·log(1 + b·f) + p_loss·log(1 − f) + p_push·log(1)
G'(f) = p_win·b/(1 + b·f) − p_loss/(1 − f) = 0
      ⇒ p_win·b·(1 − f) = p_loss·(1 + b·f)
      ⇒ f* = (p_win·b − p_loss) / (b·(p_win + p_loss)) = EV / (b·(1 − p_push))
G''(f) = −p_win·b²/(1 + b·f)² − p_loss/(1 − f)² < 0   → 凹函數、唯一最大值
```

- **無 push**（p_push = 0）：f* = (p_win·b − p_loss)/b，即標準 Kelly。
- **EV ≤ 0**：G'(0) = EV ≤ 0 且 G 凹 → [0, 1) 上最佳 f = 0（不反向下注）。分子 ≤ 0 → 直接回傳 0，不會因 edge / 公允機率為正而給正 stake。
- **p_loss = 0、p_win > 0**：G 單調遞增 → f* = 1（公式同樣給 1）；之後由 × 0.25 與 2% 上限處理。p_loss > 0 → f* < 1。
- **p_push = 1**：p_win = p_loss = 0 → EV = 0 → Kelly 0（不除以 0）。
- **等價形式**：f* = (p'·b − q')/b，其中 p' = p_win/(1 − p_push)、q' = p_loss/(1 − p_push)——push-aware Kelly = 「不 push 條件下」的標準 Kelly（push 等同沒下注）。注意：**EV**（每單位）仍是未條件化的 p_win·b − p_loss（D.2 定義）；條件化只出現在 Kelly 比例上。錯誤做法「push 當輸」會得 (p_win·b − p_loss − p_push)/b（測試排除）。
- **輸入驗證**：機率有限、在 [0, 1]、總和 = 1（容差 1e-9）；賠率有限且 > 1 → 否則 `invalid_probability`。

### 三向市場（台彩上半場主 / 和 / 客）

一個 outcome 就是一般單一 outcome Kelly：和局 p_win = P(h1 = 0)、p_loss = 1 − P(h1 = 0)、p_push = 0；主 / 客的 P(h1 = 0) 屬於 loss（D.2 定義，不是 push）。
**不**拆成兩向、**不**對多個互斥 outcome 同時下注（§6）。

## 3. Frozen risk policy（risk-v1，`pipeline/core/sizing/policy.py`）

| 參數 | 值 | 說明 |
|---|---|---|
| `KELLY_MULTIPLIER` | **0.25** | fractional_kelly = max(0, full × 0.25) |
| `MAX_BET_BANKROLL_FRACTION` | **0.02** | single_bet_capped = min(fractional, 0.02) |
| `MAX_GAME_BANKROLL_FRACTION` | **0.03** | 同一場所有 eligible stake 合計上限 |
| `MAX_DAY_BANKROLL_FRACTION` | **0.08** | 同一 betting day 合計上限（在 game scaling 之後） |
| `stale_after_poll_intervals` | **2** | last_seen_age > 2 × 來源輪詢間隔 → stale |
| `source_poll_interval_min` | twsport 30、oddsapi 360 | = D.1 宣告的輪詢間隔（測試核對 `core/odds/ingest.SOURCES`） |

- 這些是**保守的 operational risk limits，不是從歷史 ROI 最佳化出來的**；自 2026-10-04 起凍結，D.4 不得回頭依 ROI 調整。
- `RiskPolicy` 是 frozen dataclass、輪詢表是唯讀 mapping；`assert_registered()`：寫入 DB 前版本字串必須對應登記的參數（不能用 `risk-v1` 名字寫入別的參數）；未登記的 policy 只能 dry-run。
- 要改任何值 → 新版本（如 `risk-v2`）+ 登記 → 新的 sizing 列；risk-v1 舊列保留。
- 沒有環境變數、沒有 CLI 參數可以改這些值。
- **無最低 EV 門檻**：EV > 0 且通過所有檢查即有正 Kelly；小正 EV 自然得到很小的 stake（`test_no_minimum_ev_threshold_small_positive_ev_gets_small_stake`：EV +0.08% → stake < 0.05%）。UI 只顯示 EV 數值，不改公式。

`sizing_version = sizing-v1`（qualification / portfolio 演算法）與 `risk_policy_version`（參數）分開記錄。

## 4. Qualification semantics（`pipeline/core/sizing/engine.py`）

| `qualification_status` | 條件 | stake |
|---|---|---|
| `eligible` | priced、機率合法、EV > 0、互斥檢查通過、報價新鮮、未被 exposure 縮放 | final = single_bet_capped |
| `exposure_scaled` | 同上，但 game 或 day 縮放因子 < 1 | final = capped × game_scale × daily_scale |
| `no_positive_ev` | EV ≤ 0 | Kelly 0（full / fractional / capped / final 全 0） |
| `stale_quote` | 數學上 eligible，但 last_seen_age > 上限、或來源輪詢間隔未知 | 顯示理論 Kelly；final 0；不計入 exposure |
| `mutually_exclusive_positive_kelly` | 同一 snapshot 互斥 outcome 有 > 1 個正 Kelly | 整個市場 final 0 |
| `unsupported_settlement` | 兩向上半場獨贏（平手結算不明）、四分之一線、全場三向；或 priced 列的 settlement_rule 不在白名單 | 不計算 |
| `market_not_open` | 定價列 `market_not_open:*`（suspended / closed）、或已開賽 | 不計算 |
| `no_prediction` | market_only（沒有模型機率） | 不計算 |
| `invalid_probability` | 機率缺漏 / 越界 / 總和 ≠ 1、賠率不合法、no-push 規則卻有 push、EV 與機率不一致 | 不計算 |
| `unavailable` | future-data violation、artifact 不符、定價列不合法（rejected 非 not-open）、該 betting day 組合不完整 | 不計算（或 final 0） |

- 每列另有 `reasons[]`（為什麼是這個狀態 / 套用了哪個上限 / 縮放）與 `warnings[]`（資料品質、定價警示、`quote_not_confirmed_by_latest_poll`、`portfolio_incomplete:*`、`seed_fixture`）。
- **`mathematically_eligible`** = 正 Kelly 且通過全部 hard check 與互斥檢查（stale 仍為 true）。
  **`actionable`** = 另外報價新鮮、組合完整、`final_stake_fraction > 0`。兩者都**不是**推薦（D.5）。
- 檢查順序（hard → soft）：future data → 已開賽 → 選擇層判定（artifact 不符）→ 定價狀態 / 結算 → 機率合法性 / EV 一致 → EV ≤ 0 → Kelly → 新鮮度 → 互斥 → 組合完整性 → exposure。

## 5. Data-quality handling

C.5D / C.5E 證明 `season_opener` / `low_sample` / `injury_unknown` / `missing_box_recent` / `pending_prior_game` 沒有足夠樣本外證據可調整分佈。
→ D.3 **不做** `stake *= confidence_factor` / `× 0.6` / `× 0.8` 之類 heuristic：旗標轉成 `data_quality:<flag>` warning、在 sizing 輸出與 UI 顯示，stake 不變
（`test_no_heuristic_confidence_multiplication`：五個旗標 + 定價警示 → 四個 fraction 逐位元相同；`engine.py` 原始碼不含 "confidence"）。
只有 hard-invalid（模型機率缺、artifact 不符、future data、市場非 open、結算不支援）才拒絕。

## 6. Mutual exclusivity

- 分組：同一 `odds_snapshot_id` × `prediction_id` × `pricing_version`（= 同一 bookmaker、同一市場、同一時刻、同一模型輸入）。
- 互斥 outcome 中 full Kelly > 0 的數量 > 1 → 該市場全部 outcome 標 `mutually_exclusive_positive_kelly`、final 0、`mathematically_eligible = false`，**不自行挑一邊**；其他市場不受影響。
- 兩向市場：若 overround ≥ 0 且兩邊機率一致（away 的 win = home 的 loss），兩邊同時正 EV 數學上不可能
  （需 p_h + p_a > (1 − p_push)·(1/o_h + 1/o_a) ≥ 1 − p_push = p_h + p_a，矛盾）→ 出現即代表機率 / 結算 / 解析有問題（`test_two_way_consistent_probabilities_cannot_both_be_positive`）。
- 三向市場：2.2 / 5.0 / 2.2、0.46 / 0.08 / 0.46 時主、客都可正 EV（overround 10.9%，非套利）——數學上可能，但多 outcome 聯合 Kelly 不在範圍 → 一樣拒絕。

## 7. Betting day & exposure scaling

**betting_day = 開賽時間（`games.date_utc`）的 Asia/Taipei 日曆日**（`engine.betting_day`）。理由：UI 的今日 / 明日、排程、使用者都以台灣時間為主；NBA 一個美東夜晚的賽程在台灣是同一個早上（ET 19:00–22:30 → 台灣隔日 07:00–11:30），所以一個 ET slate = 一個 betting day；邊界在台灣 00:00（UTC 16:00），只有美東中午場（例如聖誕節）會落在台灣 01:00，仍與同一天晚上的比賽同屬一個台灣日期（測試涵蓋 15:59:59 / 16:00 UTC 與聖誕節）。

演算法（決定性、與輸入順序無關）：

```
participants = qualification_status == eligible 的 outcome（不含 stale / 拒絕 / 無正 EV）
game:  before_g = Σ capped（同一 game 全部 participants；不同玩法、不同 bookmaker 都算）
       scale_g  = 1 if before_g ≤ 0.03 else 0.03 / before_g
       adjusted = capped × scale_g
day:   before_d = Σ adjusted（同一 betting day）
       scale_d  = 1 if before_d ≤ 0.08 else 0.08 / before_d
       final    = adjusted × scale_d
```

- 記錄 `game_exposure_before / game_scale_factor / game_exposure_after`、`daily_exposure_before / daily_scale_factor / daily_exposure_after`（每列都帶所屬 game / day 的值）。
- 等比例縮放保留比例；**不**挑最高 EV、**不**依 EV / ROI 排序砍單（那會變成 recommendation ranking）。
- 單筆 2% 上限不是 exposure scaling（狀態維持 `eligible`、`reasons` 記 `single_bet_cap_applied`）。
- 選擇層（`job.select_candidates`）：T 時點每個 series 最新 snapshot（非 open → 不可下注、略過，與 D.2 / API 一致）× T 時點最新有效預測的定價列。
  若有可下注盤口找不到對應定價（定價落後 / artifact 失敗）→ 該 betting day `portfolio_incomplete`：全部 eligible 改 `unavailable`、不 actionable（exposure 無法完整計算時不給 stake）。
- `load_inputs`：betting day 只要有比賽超出定價 horizon（48 小時）→ 整天延後到之後的執行（避免 daily cap 用不完整的日子計算）。

## 8. Correlation limitation

同場 ML / 讓分 / 大小 / 上半場讓分 / 上半場大小、以及不同 bookmaker 的同一 outcome **不是獨立**下注。D.3 **不**建立 covariance matrix、**不**做 multivariate Kelly；
改用保守的 hard cap（同場 3%）。這代表：

1. 同場正相關的組合（例：主隊獨贏 + 主隊讓分）仍可能合計拿到 3%，實際風險比單筆 3% 略高（相關性 < 1 時較低）；3% 是刻意的保守上限。
2. 跨場之間假設近似獨立，只受單日 8% 上限。
3. 多家 bookmaker 同一 outcome（測試：Pinnacle / DraftKings / 台彩各 2%）→ 各自是機會（不做 best-book），但合計受同場上限（6% → 各 1%）——不假設在多家同押是獨立風險。
4. 跨 bookmaker 的相反方向（不同線的 middle）不屬於「同一 snapshot 互斥」檢查，只受 exposure 上限。

## 9. Odds freshness

| 欄位 | 定義 |
|---|---|
| `quote_age_seconds` | T − `fetched_at`（這個價格首次被觀測至今；只顯示） |
| `last_seen_age_seconds` | T − 最後一次輪詢確認（`last_seen_at`；舊列 NULL → `fetched_at`；歷史重建時 last_seen_at > T → 以 T 截斷：D.1 保證 fetched_at ≤ T ≤ last_seen_at 即 T 時點掛牌中） |
| `max_quote_age_seconds` | 2 × 來源輪詢間隔（台彩 3600、Odds API 43200） |
| `source` / `bookmaker` | 來源 |

依據：D.1 宣告的輪詢間隔 + 系統狀態頁既有的「超過預期間隔 2 倍 = stale」規則。意義是「最近的輪詢沒有再確認這個報價」（輪詢失敗、盤口撤下），
**不是**「價格已變」——輪詢之間的變動本來就看不到（D.1 §9）。寬鬆 safety guard，不是依來源或結果調出來的門檻。
超過 1 × 間隔加 warning `quote_not_confirmed_by_latest_poll`；超過 2 × → `stale_quote`（理論 Kelly 照算、final 0、不計入 exposure）；
沒有宣告輪詢間隔的來源 → 無法定義 → `stale_quote:poll_interval_unknown`（不偷偷假設）。
限制：Odds API 12 小時上限很寬（免費額度只允許每 6 小時一次）；台彩 HAR 手動匯入的報價 60 分鐘後即視為過舊。

## 10. Persistence design（migration 0006）

`migrations/postgres/0006_phase_d3.sql`（D1：`migrations/d1/0006_phase_d3.sql`）**純新增** `bet_sizing_snapshots`；不修改 `market_pricing_snapshots`、不寫 `bets`、不存 bankroll 金額。

- 欄位：`sizing_version`、`risk_policy_version`、`kelly_math_version`、`portfolio_key`、`market_pricing_snapshot_id`（FK）、`odds_snapshot_id`、`prediction_id`、`pricing_version`、`game_id`、`betting_day`、
  `analysis_as_of`（sizing 時點 T）、`pricing_analysis_as_of`、市場 / outcome 身分、`decimal_odds`、`p_win` / `p_push` / `p_loss`、`ev_per_unit`、`edge_vs_fair`（診斷）、
  `full_kelly_fraction`、`kelly_multiplier`、`fractional_kelly_fraction`、`max_bet_fraction`、`single_bet_capped_fraction`、`max_game_fraction`、`game_exposure_before` / `game_scale_factor` / `game_exposure_after` / `game_adjusted_fraction`、
  `max_day_fraction`、`daily_exposure_before` / `daily_scale_factor` / `daily_exposure_after`、`final_stake_fraction`、`qualification_status`、`mathematically_eligible`、`actionable`、`reasons`、`warnings`、
  `odds_fetched_at` / `odds_last_seen_at` / `quote_age_seconds` / `last_seen_age_seconds` / `max_quote_age_seconds`、`computed_at`（唯一非決定性欄位）。
- **唯一鍵** `(market_pricing_snapshot_id, risk_policy_version, sizing_version, portfolio_key)` + `ON CONFLICT DO NOTHING`。
  - 為什麼要 `portfolio_key`：final stake 取決於同一 betting day 的其他機會（game / day cap），只用 (定價列, policy) 當鍵會讓第一次寫入的縮放結果永遠凍住、之後組合改變也看不到。
  - `portfolio_key` = sha256(sizing_version、policy 全部參數、betting_day、每個成員的定價列 id + exposure 前狀態)。T 前進但成員 / 狀態不變 → 同一 key → 重跑不新增（`analysis_as_of` / 年齡保留第一次的值）；新盤口、新預測、報價變舊、policy 改版 → 新 key → 整天新列，舊列保留。
- 同一定價輸入 + 同一 policy + 同一組合 → 冪等、決定性（`test_sizing_job_persists_deterministically_and_idempotently`：存值 = 重算值、重跑 0 新增且舊列逐欄相同）。
- risk-v1 列不可改寫；policy 改版產生新列（`test_policy_version_change_creates_new_rows_and_keeps_risk_v1`）。
- **Job**：`core/sizing/job.py`；排程 `market_pricing`（每 5 分鐘）改為 `pricing_and_sizing_scheduled`：同一個 T 先 `pricing_job` 再 `sizing_job`（sizing 讀到同一輪的定價列）；心跳 `data_sources.bet_sizing`。0006 未套用 → 不計算、回報 warn。
- **CLI**：`python run_sizing.py [--dry-run] [--now T] [--bankroll 10000]`；bankroll 只輸出 `stake_amount = bankroll × final_stake_fraction`，不寫入 DB、不是 strategy 參數。
- **D.4 歷史重建**：`job.size_pricings(price_game_as_of(...)輸出, game_starts, T)`——記憶體中的 D.2 定價直接 sizing，不依賴這張表（`test_in_memory_reconstruction_path_matches`）。

## 11. API / UI changes（向下相容）

| 端點 / 欄位 | 變更 |
|---|---|
| `odds.pricing.markets[].outcomes[].pricing_id`（新） | 定價列 id（對照 sizing） |
| `odds.pricing.markets[].outcomes[].sizing`（新） | 該 outcome 最新 sizing：full / fractional / capped / final fraction、game / daily scale、status、mathematically_eligible、actionable、reasons、warnings、報價年齡；尚未 sizing → null |
| `odds.sizing`（新） | `{risk_policy_version, definitions, status: sized/partial/not_sized/no_pricing/unavailable, policy, betting_day, analysis_as_of, game_exposure{before, scale_factor, after}, daily_exposure{…}}` |
| `GET /api/sizing?date=YYYY-MM-DD`（新） | 該 betting day 最新組合的全部 sizing（按比賽分組）與單日 exposure；日期格式錯誤 400；表不存在 → `status: unavailable` |
| `odds.edges[].kelly_quarter` | 維持 `null`（deprecated）；說明文字指向 `odds.sizing` |
| `src/lib/sizing.ts`（新） | 只做欄位命名 / 對照 / 分組；**無任何乘除**（測試掃描） |
| 詳情頁 | 新增「理論注碼（risk-adjusted stake，bankroll 比例）」表：賠率、P(勝/退/輸)、EV、Full Kelly、分數 Kelly、單筆上限後、最終（風險調整）、資格、原因 / 警示；同場 / 單日 exposure 前後與縮放因子；明示「不是投注建議」 |
| 總覽頁 | 「值得關注機會」→「edge ≥ 3% 的 outcome（市場分歧，非推薦）」 |

用詞全部中性（sizing / qualification / theoretical stake / risk-adjusted stake）；沒有「必買 / 強推 / 最佳投注 / 今日推薦」。
`test_no_kelly_or_stake_math_in_typescript_or_frontend`：TS / 前端程式碼（去掉註解、字串，保留 `${…}`）中提到 kelly / stake_fraction / capped / scale_factor / exposure / bankroll 的運算式不得有 `*`、`/`、`Math.min/max`。

**部署順序**：`npm run db:migrate:pg`（0006）→ 部署排程器 → 部署 API（API 先部署也不會壞：`sizing.status = unavailable`）。

## 12. Validation examples（程式精確計算；測試以解析式驗證，不是 snapshot）

| 例 | 賠率 | p_win / p_push / p_loss | EV | full Kelly | 數值最大化 | ¼ Kelly | 單筆上限後 |
|---|---|---|---|---|---|---|---|
| **A**（無 push） | 1.91（b 0.91） | 0.55 / 0 / 0.45 | +0.0505 | **0.0554945**（(0.55·0.91 − 0.45)/0.91） | 0.0554945 | **0.0138736**（1.39%） | 0.0138736（< 2%，不受限） |
| **B**（整數線 push） | 1.91 | 0.47 / 0.06 / 0.47 | **−0.0423** | **0** | 0 | 0 | 0（`no_positive_ev`） |
| **C**（正 EV + push，手算） | 2.00（b 1） | 0.50 / 0.10 / 0.40 | +0.10 | **1/9 = 0.111111**（0.10/(1·0.9)） | 0.1111111 | 1/36 = 0.027778 | **0.02**（`single_bet_cap_applied`） |
| C2（實際水位） | 1.91 | 0.52 / 0.05 / 0.43 | +0.0432 | 0.0499711（0.0432/0.8645） | 0.0499711 | 0.0124928 | 0.0124928 |
| 錯誤做法對照（C2） | | push 當輸 | | −0.0075 → 會錯誤地拒絕 | | | |
| 三向和局 | 10.0 | 0.12 / 0 / 0.88 | +0.20 | 0.0222222（(0.12·9 − 0.88)/9） | 0.0222222 | 0.0055556 | 0.0055556 |
| push 近 1 | 1.91 | 0.015 / 0.98 / 0.005 | +0.00865 | 0.4752747 | 0.4752747 | 0.1188187 | 0.02 |
| 台彩高水（D.2 例） | 1.75 | 0.55 / 0 / 0.45 | −0.0375（edge **+5 pp**） | **0** | 0 | 0 | 0 |

**Portfolio 例**（`test_daily_cap_above_eight_percent_scaled_using_game_capped_input`）：4 場 × 兩筆 2% → 每場 4% → × 0.75 → 每場 3%；當日 12% → × 2/3 → 每筆 1.0%、當日 8%（daily 輸入是 game-capped 的 12%，不是 16%）。

**production artifact（`ml-v2.0+20261004T0339Z.20261004T033948Z`，dist-v2）、合成盤口、D.2 報告的 PHI@NYK 點預測**（分差 +7.05、總分 223.1、上半場分差 +4.35），T = 開賽前 3.5 小時：

| bookmaker | 市場 | outcome | 賠率 | p_win / p_push | EV | edge | full | ¼ | capped | final | 狀態 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| pinnacle | 獨贏 | 主 | 1.52 | 0.7002 / 0 | +0.0643 | +0.0604 | 0.1237 | 0.0309 | 0.0200 | 0.0112 | exposure_scaled |
| draftkings | 讓分 −6（整數） | 主 | 1.95 | 0.5293 / 0.0296 | +0.0617 | +0.0398 | 0.0670 | 0.0167 | 0.0167 | 0.0094 | exposure_scaled |
| fanduel | 大小 221（整數） | 大 | 1.95 | 0.5341 / 0.0212 | +0.0628 | +0.0446 | 0.0675 | 0.0169 | 0.0169 | 0.0094 | exposure_scaled |
| 台彩 | 讓分 −6.5 | 主 | 1.75 | 0.5293 / 0 | −0.0737 | **+0.0293** | 0 | 0 | 0 | 0 | no_positive_ev |
| 台彩 | 上半場三向 | 主 | 1.55 | 0.6351 / 0 | −0.0157 | **+0.0757** | 0 | 0 | 0 | 0 | no_positive_ev |
| fanduel | 上半場兩向獨贏 | 主 / 客 | 1.50 / 2.60 | — | — | — | — | — | — | 0 | unsupported_settlement |

同場 exposure 5.36% → 3.00%（× 0.5594）。台彩兩列 edge 為正但 EV 為負 → Kelly 0（edge 不是 stake 依據）。

## 13. Tests

新增 `tests/test_sizing.py`（76，純邏輯）、`tests/test_sizing_db.py`（5，暫存 schema）、`tests/test_odds_scheduler.py` +1：

| 要求 | 測試 |
|---|---|
| Kelly：標準 / push 解析解 / 數值最大化 / EV ≤ 0 / push 近 1 / 賠率邊界 | `test_standard_no_push_kelly_exact_example_a`、`test_push_aware_kelly_exact_analytic_solution`、`test_example_c_positive_ev_with_push_exact_and_numerical`、`test_numerical_expected_log_maximization_matches_analytic`（8 組）、`test_example_b_integer_push_negative_ev_gives_zero_kelly`、`test_ev_le_zero_is_zero_kelly`（5 組）、`test_push_probability_near_one`、`test_decimal_odds_edge_cases`（8 組）、`test_invalid_probabilities_rejected`（4 組）、`test_stored_ev_must_match_probabilities_and_odds` |
| 三向 | `test_draw_kelly_uses_single_outcome_formula`、`test_three_way_other_outcomes_are_losses`、`test_three_way_only_one_outcome_can_be_sized` |
| 不支援 | `test_h1_two_way_moneyline_unknown_settlement_never_sized`、`test_quarter_line_never_sized`、`test_missing_model_is_no_prediction`、`test_closed_or_suspended_market_never_sized`（2）、`test_started_game_and_invalid_rows` |
| Policy | `test_risk_v1_is_frozen`、`test_kelly_fraction_env_setting_removed`、`test_quarter_kelly_exactly`、`test_two_percent_per_bet_cap`、`test_no_heuristic_confidence_multiplication`、`test_edge_is_not_used_as_kelly_input`、`test_no_minimum_ev_threshold_small_positive_ev_gets_small_stake` |
| Game cap | `test_game_cap_below_three_percent_unchanged`、`test_game_cap_above_three_percent_scaled_proportionally`、`test_game_cap_counts_ml_spread_total_and_h1_together` |
| Daily cap | `test_daily_cap_below_eight_percent_unchanged`、`test_daily_cap_above_eight_percent_scaled_using_game_capped_input`、`test_daily_cap_is_per_taipei_betting_day`、`test_non_actionable_rows_do_not_consume_exposure` |
| Correlation | `test_multiple_books_same_game_count_toward_same_game_cap`、`test_game_cap_counts_ml_spread_total_and_h1_together` |
| 互斥 | `test_both_sides_positive_kelly_rejects_market`、`test_two_way_consistent_probabilities_cannot_both_be_positive`、`test_three_way_only_one_outcome_can_be_sized`、`test_other_markets_unaffected_by_rejected_market` |
| 時間 / 新鮮度 | `test_future_pricing_snapshot_is_unavailable`、`test_stale_quote_behavior`、`test_last_seen_after_as_of_means_quote_was_live_at_as_of`、`test_asia_taipei_betting_day_boundary`（5）、`test_boundary_games_land_in_different_daily_caps`、`test_select_candidates_uses_latest_snapshot_and_latest_prediction`、`test_select_candidates_pricing_lag_and_artifact_mismatch_and_closed`、`test_portfolio_incomplete_blocks_actionable` |
| 決定性 / 持久化（DB） | `test_deterministic_and_order_independent`、`test_portfolio_key_changes_only_when_portfolio_changes`、`test_sizing_job_persists_deterministically_and_idempotently`、`test_policy_version_change_creates_new_rows_and_keeps_risk_v1`、`test_portfolio_change_creates_new_rows_old_rows_immutable`、`test_dry_run_and_bankroll_never_write_and_bets_untouched`、`test_stale_quote_persisted_as_not_actionable` |
| Bankroll / D.4 路徑 | `test_bankroll_amount_is_derived_not_stored`、`test_in_memory_reconstruction_path_matches` |
| Serialization / API | `test_result_serialization_is_json_safe_and_covers_columns`、`test_no_kelly_or_stake_math_in_typescript_or_frontend`、Node smoke（§14） |
| 排程 | `test_sizing_runs_in_same_job_right_after_pricing_with_same_as_of` |

## 14. Results

- **完整 `cd pipeline && pytest`：544 passed（既有 462 + 新增 82；703 秒）**。DB 測試使用暫存 schema `c5a_test_*`（結束後 DROP），不碰 production `public`。
- **Node smoke（`npm run test:api`）：106 通過 / 0 失敗**（D.2 的 96 項 + 10 項 D.3：policy、每個 outcome 都有 sizing、最終 ≤ 上限後 ≤ 分數 ≤ full、EV ≤ 0 → 0、actionable ⇔ final > 0、同場 ≤ 3%、同市場最多一個正注碼、kelly_quarter 仍 null、`/api/sizing` 單日 ≤ 8% 且 = Σ final、400）。
  環境：`npm run build` → `npm run db:reset:local`（0001/0002/0004/0005/0006 + seed，含 40 列定價 + 40 列 sizing）→ `wrangler pages dev dist --local --env-file <只含 SESSION_SECRET> --binding DATABASE_URL=`，
  先確認 `/api/system/status` 的 `db_driver = d1`（不連 Supabase）。瀏覽器確認詳情頁「理論注碼」表（seed BOS 場：同場 12.96% → 3.00%、單日 9.00% → 8.00%；上半場讓分 −2 整數線 push 3.5% 正確顯示）。
- seed：`python -m core.sizing.seed_fixture` 以同一個 `size_portfolio`（risk-v1）從 seed 定價列產生；`scripts/render-seed.mjs` 新增 `{{TPEDATE:+N}}` token（betting_day）。
  `python -m core.pricing.seed_fixture` 重構後輸出與既有 seed 區塊逐位元相同。

## 15. Migration status / production

正式 DB（2026-10-04，唯讀查詢）：`schema_migrations` = 0001–0005（**0005 已套用**）；`market_pricing_snapshots` 0 列、`odds_snapshots` 0 列、未來未開賽比賽 0 場、`bets` 0 列、
predictions 只有歷史 `ml-v1.0` / `elo-v1.0`。`bet_sizing_snapshots` 不存在——**0006 未套用**（依約定不寫 production；需要時由使用者執行 `npm run db:migrate:pg`，只新增一張表）。
0006 未套用時：排程的 sizing 回報 warn 不寫入、API `sizing.status = unavailable`，定價（D.2）不受影響。

## 16. Operational risks

1. **相關性只用 hard cap 處理**（§8）；同場正相關組合的實際風險可能高於單筆加總的直覺。
2. **exposure 不含已下的注**：組合只看 T 時點仍可下注的理論機會；使用者在較早時點已下的注（`bets`）、已開賽比賽的注不計入同場 / 單日上限 → D.5 需要 bets-aware exposure。
3. **組合是「全部 eligible 機會同時下注」的理論值**：若只執行其中一部分，未執行部分的縮放額度不會重新分配（刻意不做 ranking）。
4. **報價新鮮度解析度 = 輪詢間隔**：Odds API 每 6 小時一次 → 12 小時上限很寬；價格在輪詢之間變動看不到；台彩自動擷取仍被擋（D.1），HAR 匯入的報價 60 分鐘後即 stale。
5. **定價落後**：同一 job 內先定價再 sizing；仍有缺定價（artifact 失敗等）→ 整個 betting day 不 actionable（寧可不給 stake）。
6. **模型風險**：Kelly 對機率誤差敏感；分差導出獨贏機率 calibration slope ≈ 1.1（C.5E §9b，未校正）、2026-27 為前瞻驗證期；¼ Kelly 與 2% / 3% / 8% 是對此的保守緩衝，**不是**依 ROI 校準。
7. **push 結算假設**：整數線一律退本金（D.2 §17-2）；若日後台彩整數線規則不同需新增結算規則。
8. Supabase pooler `extra_float_digits = 0`：sizing 存值回讀 15 位有效數字（測試以 1e-12 相對誤差比較）。

## 17. D.4 historical ROI readiness

**可以開始 D.4**，前提與限制：
- 可用：`pricing.alignment.price_game_as_of(odds_rows, pred_rows, game, T, model_for)` → `sizing.job.size_pricings(pricings, game_starts, T, policy=RISK_V1)`，只用 T 以前的盤口 / 預測 / artifact；
  risk-v1 已凍結，D.4 只能**評估**、不得依結果調整 multiplier / caps / 新鮮度規則（要改 → 新版本並明確記錄是 ex-post）。
- 需要在 D.4 定義（本階段刻意不做）：決策時點 T 的選擇規則（例如開賽前 60 分鐘）、同一 betting day 多個 T 的組合如何轉成實際下注序列、結算（`games` 終場比分 → win / push / loss / draw）、bankroll 隨時間更新。
- 資料：正式 DB 目前沒有任何盤口快照；嚴格的歷史 ROI 只能從開季後實際累積 `odds_snapshots` 開始（D.1 §8）；規格要求 ROI 以**台彩實際賠率**計算，而台彩自動擷取仍不可用。

本階段到此停止，未開始 D.4。

## 附錄 A. 檔案

| 檔案 | 內容 |
|---|---|
| `pipeline/core/sizing/kelly.py`（新） | push-aware Kelly、EV、輸入驗證、期望對數成長 |
| `pipeline/core/sizing/policy.py`（新） | risk-v1（凍結）、新鮮度上限、policy 登記檢查 |
| `pipeline/core/sizing/engine.py`（新） | qualification、互斥、game / day exposure、portfolio_key、betting_day |
| `pipeline/core/sizing/job.py`（新） | as-of 選擇、DB 讀寫、`sizing_job`、`size_pricings`（D.4）、排程進入點 |
| `pipeline/core/sizing/seed_fixture.py`（新） | seed sizing 列產生器 |
| `pipeline/run_sizing.py`（新） | CLI（`--dry-run` / `--now` / `--bankroll`） |
| `pipeline/core/pricing/seed_fixture.py` | 重構出 `seed_pricings()`（輸出不變） |
| `pipeline/core/scheduling.py` | `market_pricing` → `pricing_and_sizing_scheduled` |
| `pipeline/core/config.py`、`.env.example` | 移除 `kelly_fraction` / `KELLY_FRACTION` |
| `migrations/postgres/0006_phase_d3.sql`、`migrations/d1/0006_phase_d3.sql`（新） | `bet_sizing_snapshots` |
| `src/lib/sizing.ts`（新）、`src/lib/pricing.ts`、`src/db/queries.ts`、`src/routes/api.ts` | 唯讀序列化、`odds.sizing`、`/api/sizing` |
| `public/static/js/game-detail.js`、`games.js` | 理論注碼表、中性用詞 |
| `seed/seed.template.sql`、`scripts/render-seed.mjs`、`package.json`、`scripts/smoke-test.mjs` | seed sizing、`{{TPEDATE}}`、本機 0006、smoke 檢查 |
| `pipeline/tests/test_sizing.py`、`test_sizing_db.py`（新）、`test_odds_scheduler.py`、`conftest.py` | 測試 |
| `README.md` | Phase D.3 摘要 |
