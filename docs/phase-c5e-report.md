# Phase C.5E — Predictive Distributions & Probability Calibration 報告

日期：2026-10-04（同日修訂：§9 評測後 production 修正、§9b 獨贏機率比較）　範圍：把分差 / 總分 / 上半場分差 / 上半場總分的點預測轉成預測分佈，回答任意盤口線的 P(Y > line)、P(Y = line)、P(Y < line)。
**未抓台彩 / The Odds API、未做去水 / EV / Kelly / ROI、未重新調整 C.5C 點預測模型、未使用 C.5D full-fit 的 in-sample 殘差、未 commit、未寫入正式資料庫。**

## 0. 結論摘要

| 問題 | 答案 |
|---|---|
| 預先登記的選擇（驗證賽季） | 四個 target 都是**全域高斯殘差**（μ = 樣本外平均殘差、σ = 樣本外殘差標準差），連續性修正離散化到整數比分；全場分差排除 0（延長賽） |
| **目前 production（dist-v2）** | 同上，但 **μ = 0**（σ = 以 0 為中心的 OOS 殘差均方根）。這是**評測後的 production 修正**（§9），2026-10-04 起凍結，2026-27 賽季為前瞻驗證期；2024-25 / 2025-26 上的改善**不是**確認性證據 |
| 情境尺度（季初 / 傷病未知 / 預測水準 / 季後賽）是否有 OOS 改善？ | **沒有**。驗證賽季上全部不顯著（分差甚至顯著較差），評測賽季也全部不顯著 → 用較簡單的全域尺度；資料品質旗標只作 UI 警示 |
| 校準（預先登記的 μ = OOS 平均） | 評測 2024-25 + 2025-26（每個 profile 2,631 場）：50 / 80 / 90 / 95% 區間覆蓋率 分差 52.3 / 79.5 / 89.0 / 93.3%、總分 49.0 / 79.8 / 89.6 / 94.8%、上半場分差 50.7 / 80.4 / 90.2 / 94.4%、上半場總分 50.4 / 80.9 / 90.2 / 94.9% |
| 合成盤口線 calibration | 半分線（預測值 ±10 分）機率分箱的 ECE：分差 1.2%、總分 2.1%、上半場分差 1.5%、上半場總分 2.9%（§6） |
| push | 整數線 push 機率 vs 實際：分差 2.71% vs 2.85%、總分 2.07% vs 2.07%、上半場分差 3.27% vs 3.30%、上半場總分 3.02% vs 3.07% |
| early vs final | 只有分差的殘差變異 early 較大（MSE 比 1.016，95% CI [1.006, 1.026]，σ 差約 0.1 分）；總分類無差異。依預先規則分差 / 總分 / 上半場分差共用分佈；上半場總分分開（差異可忽略，§8） |
| 位置參數 μ | 季間漂移造成 1–3 個百分點的系統偏差；使用者決定改用 μ = 0 並凍結（§9，評測後修正） |
| 獨贏（Phase D） | 分差分佈導出的 P(主勝) 對專用邏輯迴歸**非劣**（預先固定的 δ；評測合併、每季、每 profile 皆通過；兩種中心結論相同）→ Phase D 獨贏優先用分差導出機率（§9b） |
| Production | artifact schema v2（`profiles.<p>.distributions.<target>`）、分佈版本 dist-v2；`run_retrain.py` 重訓時以 walk-forward OOS 殘差擬合；`ml-v2.0+20261004T0339Z.20261004T033948Z` 已上線；schema v1 與 dist-v1 artifact 被明確拒絕 |
| 測試 | 新增 45 項；完整 `pytest` **305 passed** |
| **可否開始 Phase D** | **可以**（§15） |

---

## 1. OOS residual data audit

| 檔案 / 資料 | 內容 | 是否真正樣本外 | 能否用於分佈選擇 |
|---|---|---|---|
| `artifacts/c5c_eval_predictions.csv.gz`（C.5C） | 2024-25、2025-26 每個特徵組的預測、y、min_gp | **是**（fold 模型只用更早賽季訓練；超參數在 2023-24 選定） | **不能**：只有評測賽季，沒有 2023-24 殘差；沒有情境欄位（只有 min_gp） |
| `artifacts/c5c_timing_predictions.csv.gz`（C.5C） | 同上，early / T-60 / T-15 / 固定傷病常數 | 是 | 同上 |
| C.5D production artifact 的 `in_sample` | full-fit 模型對訓練資料的預測 | **否（in-sample）** | **禁止使用**：殘差偏小（測試驗證：同一批比賽 in-sample 殘差標準差 < OOS） |
| **`artifacts/c5e_oos_predictions.csv.gz`（本階段產生）** | production 相同流程（pregame-v2.1、重播 Elo、C.5C 固定規格）的 walk-forward：測試季 S 只用 S 之前訓練；2022-23 ~ 2025-26 × early / final × 5 targets；含 min_gp、傷病是否已知、延續性是否已知、季後賽、`train_max_season` | **是**（每列 `train_max_season < season`，程式內 assert + 測試） | **是** |

一致性檢查：本階段 OOS 對 2024-25 / 2025-26 的總分類預測與 C.5C 檔案**完全相同**（MAE 14.925 / 9.637），分差類差 ≤ 1.2 分（v2.1 交易感知名單 + 重播 Elo；MAE 10.977 vs 10.978）。

OOS 殘差（final profile；early 幾乎相同）：

| 賽季 | 分差 μ / sd | 總分 μ / sd | 上半場分差 μ / sd | 上半場總分 μ / sd |
|---|---|---|---|---|
| 2022-23（fold 只用 2021-22 訓練） | +0.80 / 12.76 | −2.73 / 18.87 | +0.57 / 10.93 | −1.91 / 12.33 |
| 2023-24（驗證） | +0.29 / 13.97 | −1.71 / 18.16 | +0.23 / 11.27 | −0.32 / 12.19 |
| 2024-25（評測） | −0.30 / 14.00 | +0.94 / 18.42 | −0.27 / 11.10 | +0.91 / 11.79 |
| 2025-26（評測） | −0.47 / 14.20 | −0.80 / 18.94 | −0.31 / 11.27 | −0.05 / 12.33 |

情境欄位可用性（OOS 比賽）：

| 旗標 | 歷史樣本 | 能否評估 |
|---|---|---|
| early_season / low_sample / season_opener（min_gp） | 11.8% 場次 min_gp < 10 | 可 |
| injury_unknown | early profile 32%（歷史 early 決策時點常常還沒有涵蓋報告）、final 0.1%（6 場） | early 可；final 樣本不足 |
| 季後賽 | 6.4% | 可 |
| 預測水準 / 預測分差大小 | 全部 | 可 |
| missing_box_recent | 歷史只缺 1 場 box score → 幾乎沒有樣本 | **不可**（UI 警示） |
| pending_prior_game / stale_results | 歷史回填時前一場都已完賽 → 0 樣本 | **不可**（UI 警示） |

殘差形狀：大致對稱、峰度略高（分差 excess kurtosis 0.42、總分 0.03）。全場分差從不為 0（延長賽，5.2% 場次）。

## 2. Probability targets

每場、每個 target：`error = actual − predicted`（walk-forward OOS 預測）。預測分佈：Y = pred + ε，ε 的 CDF 為 G，整數結果的機率質量（連續性修正）：

```
P(Y = k) = G(k + 0.5 − pred) − G(k − 0.5 − pred)，支撐範圍兩端吸收尾巴（總和恰為 1）
全場分差：P(Y = 0) = 0，其餘質量重新正規化（條件於非平手）
任意線 L：above = P(Y > L)、push = P(Y = L)（只有整數 L 可能 > 0）、below = P(Y < L)；三者和 = 1
```

「主隊讓 L 分」= P(margin > L)；客隊那一邊 = probability_below；整數線另有 push。四分之一線（如 −2.25）push = 0，`quarter_line_components()` 只做數學拆解（−2.25 → −2.5 / −2.0），不寫死任何 bookmaker 結算規則。

## 3. Distribution candidates & fitting method

| 候選 | 定義 |
|---|---|
| A. global empirical | ε = μ + σ·z，z 的分佈 = 標準化 OOS 殘差的核平滑 ECDF（Silverman 帶寬），存成固定格點（±12σ，0.01 間距）上的 CDF |
| B. global Gaussian | ε ~ N(μ, σ²) |
| C. conditional scale（A / B 各一） | log σ(x) = b0 + Σ b_j x_j；高斯概似 + 岭懲罰（λ），懲罰把係數收縮回全域 σ；σ(x)/σ 限制在 [0.5, 2] |

情境特徵（看任何結果之前固定）：分差類 = |預測分差|、季初斜坡 (10 − min_gp)/10、傷病未知、季後賽；總分類 = 預測水準、季初、傷病未知、季後賽。連續特徵標準化；0/1 特徵不標準化，λ 的單位約等於「虛擬觀測數」——出現次數遠少於 λ 的情境（例如 final 的傷病未知只有 6 場）係數會被收縮到接近 0。

**時間順序（§5 的要求）**：分佈參數**每週重擬合**（與 production 每週重訓相同）：某週比賽只用該週 ET 週一 00:00 之前已結束比賽的 OOS 殘差（含同季較早的週）。程式內 assert「擬合集最晚時間 < 該週最早比賽」，測試以竄改未來殘差驗證過去評分不變。

**預先登記的選擇規則**（只在驗證賽季 2023-24；擬合集 = 2022-23 OOS + 2023-24 較早的週）：
1. 尺度模型的 λ ∈ {50, 200, 1000}：驗證 RPS 最小。
2. 情境尺度 vs 全域：只有在 RPS 差的比賽日區塊 bootstrap 95% CI 完全 < 0 時才用情境尺度。
3. empirical vs Gaussian：RPS 較低者；差異 CI 含 0 → Gaussian（較簡單、尾巴平滑、有封閉解）。
4. profile：early / final 分開 vs 合併殘差；任一 profile 分開顯著較好 → 分開，否則合併。

評分：RPS（整數結果的離散 CRPS，主要指標）、離散 NLL（−log P(Y = y)，離散分佈的合法對數概似）、隨機化 PIT（離散結果的正確 PIT）。評測賽季 2024-25 / 2025-26 只用凍結的選擇跑一次。

## 4. Heteroskedastic uncertainty（敘述統計；final profile，sd = 殘差標準差）

| 情境 | 分差 pre-eval / eval | 總分 pre-eval / eval |
|---|---|---|
| 全部 | 13.38 / 14.10 | 18.52 / 18.70 |
| 預測水準五分位（低 → 高） | 13.9, 13.3, 12.4, 13.7, 13.6 / 14.2, 14.8, 14.4, 13.3, 13.8 | 17.8, 17.9, 18.7, 18.9, 19.4 / 18.5, 18.3, 18.5, 18.6, 19.6 |
| 本季第 1–5 / 6–10 / 11–20 / 21+ 場 | 13.4 / 12.4 / 12.9 / 13.5 ‖ 13.9 / 12.0 / 13.0 / 14.4 | 18.8 / 17.9 / 18.8 / 18.5 ‖ 19.4 / 16.8 / 19.4 / 18.6 |
| 季後賽 vs 例行賽 | 14.6 vs 13.3 / 15.9 vs 14.0 | 17.5 vs 18.6 / 18.6 vs 18.7 |
| 傷病未知（early profile） | 13.5 vs 13.5 / 14.9 vs 13.9 | — |

- 唯一兩期方向一致的訊號：**總分 σ 隨預測總分上升**（最高五分位比最低高約 6–9%）、**季後賽分差較寬**。
- 季初（第 1–10 場）沒有一致的變寬；early profile 的傷病未知只有評測期較寬。
- 這些效應都小，在情境尺度模型裡也沒有轉成顯著的 RPS 改善（§7）。

> §5–§8 是**預先登記設定（μ = OOS 平均，dist-v1）**的原始結果，為透明起見完整保留；production 已改為 μ = 0，見 §9。

## 5. Interval coverage（評測，SELECTED = 全域高斯、μ = OOS 平均；early + final 共 5,262 場次）

| Target | 50% | 80% | 90% | 95% | PIT 最大偏差 | RPS | NLL |
|---|---|---|---|---|---|---|---|
| 分差 | 52.3% | 79.5% | 89.0% | 93.3% | 0.014 | 7.905 | 4.045 |
| 總分 | 49.0% | 79.8% | 89.6% | 94.8% | 0.018 | 10.578 | 4.352 |
| 上半場分差 | 50.7% | 80.4% | 90.2% | 94.4% | 0.011 | 6.287 | 3.835 |
| 上半場總分 | 50.4% | 80.9% | 90.2% | 94.9% | 0.015 | 6.839 | 3.915 |

每季 × profile：分差 95% 覆蓋 92.8–93.8%（尾巴比高斯略厚；empirical 為 94.1%，但兩者 RPS 差異不顯著）；上半場總分 2024-25 略寬（95.5%）、2025-26 略窄（94.3%）。驗證賽季（2023-24）覆蓋：分差 51.2 / 78.0 / 88.7 / 93.3%、總分 49.9 / 81.8 / 91.5 / 96.1%。

## 6. Probability calibration（合成盤口線，不使用真實賠率）

每場 20 條半分線：floor(pred) + 0.5 + d，d = −10…+9（不會 push），預測 P(Y > L) vs 實際 1[Y > L]。

| Target | ECE（全部） | Brier | 0.30–0.70 區間 ECE |
|---|---|---|---|
| 分差 | 1.17% | 0.222 | 1.29% |
| 總分 | 2.05% | 0.237 | 1.98% |
| 上半場分差 | 1.48% | 0.213 | 1.31% |
| 上半場總分 | 2.94% | 0.219 | 3.06% |

接近 50% 的分箱（實際最常用的盤口區域）：

| 預測 | 分差 實際 | 總分 實際 | 上半場分差 實際 | 上半場總分 實際 |
|---|---|---|---|---|
| 0.35–0.40（平均 0.375） | 0.352 | 0.399 | 0.360 | 0.408 |
| 0.45–0.50（0.475） | 0.461 | 0.496 | 0.463 | 0.505 |
| 0.50–0.55（0.525） | 0.515 | 0.543 | 0.514 | 0.552 |
| 0.60–0.65（0.625） | 0.621 | 0.644 | 0.612 | 0.656 |

偏差方向固定：分差類略高估「主隊蓋盤」、總分類略低估「大分」——這正是 §9 的位置參數 μ 問題（μ 來自前幾季的平均殘差，季間會翻號）。

整數線 push（round(pred) + d，|d| ≤ 4）：預測與實際一致（上表摘要）；小分差的機率質量：分差 P(Y = ±1) 預測 2.6% / 實際 1.9–2.4%、±2 2.5% / 2.7–2.8%、±3 2.5% / 2.4–2.5%；上半場分差 P(Y = 0) 預測 3.33% / 實際 3.53%。

## 7. Global vs context-aware

| Target | 驗證：情境尺度 − 全域 RPS（λ=1000，95% CI） | 評測 2024-25 | 評測 2025-26 | 選擇 |
|---|---|---|---|---|
| 分差 | **+0.0028 [+0.0011, +0.0045]（顯著較差）** | −0.0005 [−0.0018, +0.0009] | −0.0016 [−0.0039, +0.0008] | 全域 |
| 總分 | −0.0053 [−0.0121, +0.0016] | +0.0042 [−0.0026, +0.0116] | −0.0030 [−0.0083, +0.0024] | 全域 |
| 上半場分差 | −0.0002 [−0.0020, +0.0018] | −0.0002 [−0.0018, +0.0014] | +0.0009 [−0.0005, +0.0024] | 全域 |
| 上半場總分 | −0.0005 [−0.0031, +0.0020] | +0.0012 [−0.0019, +0.0045] | +0.0010 [−0.0012, +0.0031] | 全域 |

empirical vs Gaussian（驗證）：分差 −0.0011 [−0.0035, +0.0014]、總分 +0.0051 [−0.0055, +0.0150]、上半場分差 −0.0004 [−0.0035, +0.0026]、上半場總分 **+0.0087 [+0.0030, +0.0141]（empirical 顯著較差）** → 全部 Gaussian。評測期也一致（總分 / 上半場總分 empirical 顯著較差，分差 / 上半場分差不顯著）。

**結論：情境尺度沒有穩定的樣本外改善 → production 用全域尺度。** 資料品質旗標（season_opener、low_sample、injury_unknown、missing_box_recent、pending_prior_game、stale_results…）**不改變分佈寬度**，只作 UI 警示；C.5D 的啟發式 `confidence_factor` 也不拿來縮放標準差。

## 8. Early vs final

| Target | sd early / final（評測） | MSE 比 early / final，95% CI（pre-eval） | （評測） |
|---|---|---|---|
| 分差 | 14.20 / 14.10 | 1.016 [1.006, 1.026] | 1.015 [1.006, 1.024] |
| 總分 | 18.70 / 18.70 | 0.999 [0.997, 1.000] | 1.001 [1.000, 1.002] |
| 上半場分差 | 11.19 / 11.18 | 1.003 [1.000, 1.006] | 1.001 [0.998, 1.005] |
| 上半場總分 | 12.08 / 12.08 | 1.000 [0.999, 1.001] | 1.001 [0.999, 1.003] |

- final 的傷病資訊**確實**降低分差的殘差變異，但只有約 1.6% 的 MSE（σ 約 0.1 分）；總分類完全沒有差別（傷病不影響總分模型）。
- 依預先規則：分差 / 總分 / 上半場分差的「分開擬合」沒有顯著較好 → **共用**（合併兩個 profile 的殘差）。
- 上半場總分：final 分開顯著較好（RPS −0.0004），但 early 分開反而顯著較差（+0.0004）——規則寫的是「任一 profile 顯著較好就分開」，所以**分開**；兩個 σ 為 12.205 / 12.203，實務上相同。這條規則的不對稱是設計上的瑕疵，記錄在限制。
- early 預測的區間不需要另外加寬：評測覆蓋率 early 與 final 幾乎相同（分差 95%：93.1% vs 93.5%）。

## 9. 位置參數 μ 與評測後的 production 修正

**原始設定（預先登記，dist-v1）**：μ = 擬合集 OOS 殘差的平均，代表「點預測的系統偏差」。但它每季翻號（總分 −2.73、−1.71、+0.94、−0.80；分差 +0.80、+0.29、−0.30、−0.47）——得分環境的季間漂移不是前幾季的平均能預測的。驗證季剛好與前一季同號，所以驗證時 μ 有幫助；評測季翻號，μ 反而造成 §6 的系統偏差。

評測後的敏感度分析（同一分佈，只把 μ 固定為 0；**評測季看過之後才做**）：

| Target | RPS（μ=0 − 原始，95% CI） | 0.30–0.70 ECE：原始 → μ=0 | 80% / 95% 覆蓋：原始 → μ=0 |
|---|---|---|---|
| 分差 | −0.0064 [−0.0140, +0.0008] | 1.29% → 0.73% | 79.5 / 93.3% → 79.7 / 93.4% |
| 總分 | −0.0330 [−0.0625, −0.0018] | 1.98% → 0.74% | 79.8 / 94.8% → 80.1 / 95.1% |
| 上半場分差 | −0.0052 [−0.0104, −0.0002] | 1.31% → 0.67% | 80.4 / 94.4% → 80.2 / 94.5% |
| 上半場總分 | −0.0257 [−0.0390, −0.0125] | 3.06% → 1.29% | 80.9 / 94.9% → 81.0 / 95.0% |

**決定（2026-10-04，使用者選擇 option (a)）：production 改為 μ = 0，並自此凍結。**

- 性質：這是**評測後的 production 修正（post-evaluation production correction）**，不是預先登記的選擇。上表在 2024-25 / 2025-26 的改善是「發現問題的過程」，**不能**當作 μ = 0 的確認性證據。
- 前瞻驗證：**2026-27 賽季**是這個選擇的驗證期。之後不再依 2024-25 / 2025-26 調整分佈；2026-27 結束後以同一套指標（覆蓋率、RPS、合成盤口線 ECE、μ = 0 vs OOS 平均的成對比較）檢視。
- 實作：`spec.DISTRIBUTION_SPEC[*]["location"] = "zero"`、`spec.LOCATION_FROZEN_AT = "2026-10-04"`；分佈版本升為 **dist-v2**（σ = 以 0 為中心的 OOS 殘差均方根），所以 dist-v1 artifact 會被明確拒絕、不會被默默沿用。原始的 `"oos_mean"` 仍可重現（測試涵蓋）。
- 其他設定（全域高斯、不分情境、profile 共用規則）完全沒有改動；**沒有**依評測季做任何其他調整。
- 對 production 的影響：μ 原本為 分差 +0.09、總分 −1.09、上半場分差 +0.06、上半場總分 −0.39 / −0.34；改為 0 後總分類機率不再被往「小分」偏約 2 個百分點（例：BOS@DET 總分 223.5 大分 0.4684 → 0.4916）。

## 9b. 獨贏機率：分差分佈導出 vs 專用邏輯迴歸（Phase D 定價用）

比較方式（不調任何模型）：同一批 OOS 比賽（每 profile、每季），分差分佈（每週重擬合、兩 profile 共用的全域高斯）導出的 P(margin > 0) vs OOS 邏輯迴歸勝率（C.5C 勝負模型、相同 walk-forward fold）。
**非劣性界線在執行比較之前就固定**：每場損失差（分差導出 − 邏輯迴歸）的比賽日區塊 bootstrap 95% CI 上界 < δ；log loss δ = 0.0025（約 C.5C 相對 Phase C 改善 0.0124 的 1/5）、Brier δ = 0.0010。

**μ = 0（production）**

| 範圍 | n | log loss 分差導出 / 邏輯 | Brier | ECE | calibration slope | accuracy | Δ log loss（95% CI） | Δ Brier（95% CI） | 非劣 |
|---|---|---|---|---|---|---|---|---|---|
| 2023-24（驗證季，評測前） | 2,626 | 0.6035 / 0.6044 | 0.2085 / 0.2088 | 0.023 / 0.026 | 1.10 / 1.07 | 0.668 / 0.677 | −0.0009 [−0.0045, +0.0029] | −0.0003 [−0.0019, +0.0015] | 否（CI 太寬） |
| 2024-25 | 2,630 | 0.5957 / 0.6011 | 0.2054 / 0.2077 | 0.026 / 0.030 | 1.05 / 0.90 | 0.672 / 0.666 | −0.0054 [−0.0095, −0.0015] | −0.0023 [−0.0039, −0.0007] | 是 |
| 2025-26 | 2,632 | 0.5846 / 0.5863 | 0.1998 / 0.2007 | 0.029 / 0.028 | 1.20 / 1.08 | 0.693 / 0.686 | −0.0017 [−0.0044, +0.0012] | −0.0009 [−0.0021, +0.0003] | 是 |
| 評測合併 | 5,262 | 0.5901 / 0.5937 | 0.2026 / 0.2042 | 0.016 / 0.015 | 1.12 / 0.98 | 0.683 / 0.676 | −0.0035 [−0.0061, −0.0009] | −0.0016 [−0.0026, −0.0005] | 是 |
| 評測合併 early / final | 2,631 / 2,631 | 0.5919 / 0.5953；0.5884 / 0.5921 | — | — | — | — | −0.0034 [−0.0059, −0.0008]；−0.0037 [−0.0062, −0.0009] | −0.0016 [−0.0026, −0.0005]；−0.0016 [−0.0027, −0.0005] | 是 / 是 |
| 三季合併 | 7,888 | — | — | — | 1.11 / 1.00 | — | −0.0027 [−0.0046, −0.0005] | −0.0012 [−0.0020, −0.0003] | 是 |

**μ = OOS 平均（預先登記的中心）**：評測合併 Δ log loss −0.0030 [−0.0057, −0.0003]、Δ Brier −0.0014 [−0.0025, −0.0002]；2024-25 / 2025-26 / early / final 各自皆非劣；三季合併 −0.0018 [−0.0041, +0.0007] / −0.0008 [−0.0018, +0.0003]，非劣。2023-24 單季同樣因 CI 太寬未能證明非劣（Δ log loss +0.0008 [−0.0037, +0.0056]）。
→ **結論不依賴 μ 的評測後修正**：兩種中心都得到非劣。

兩者一致性：相關 0.99、平均差 2.3 個百分點、90% 分位差約 5 個百分點；同一個熱門隊 96.4%。

**結論與建議（Phase D）**：依使用者規則「非劣則優先用分差導出機率（與讓分機率跨市場一致）」——分差導出的 P(主勝) 在預先固定的 δ 下非劣（評測合併、每個評測季、每個 profile、三季合併皆通過；只有單一驗證季樣本不足以下結論）。**Phase D 獨贏優先使用 `predict_margin_probability(game, 0)` 的 probability_above**。注意事項：
- 分差導出機率的 calibration slope 約 1.1（比邏輯迴歸的 1.0 略偏保守：機率略往 0.5 壓縮）；**不**依評測季另外校正（那會是新的調參），列入 2026-27 前瞻監控。
- 邏輯迴歸勝率保留在 `predictions.home_win_prob`（前端既有欄位不變），Phase D 可作一致性監控：兩者差 > 5 個百分點的比賽應標示檢查。

## 10. Discrete / push treatment

- 整數線：win / push / loss 三分（例：PHI@NYK 預測分差 +7.05，線 +7 → 主隊 0.4996 / push 0.0296 / 客隊 0.4707）。
- 半分線：push = 0；`P(Y > k) + P(Y = k) = P(Y > k − 0.5)` 有測試。
- 全場分差 0 不可能：線 0 與線 +0.5 的 above 相同（例：OKC@SAS 兩者皆 0.5433），push = 0。
- 上半場分差可平手：線 0 push ≈ 3.3–3.6%（實際 3.5%）。
- 四分之一線：push = 0；拆解交給未來的 bookmaker adapter。
- 連續近似在常用線上足夠：整數線 push 預測 vs 實際誤差 ≤ 0.6 個百分點（多數 ≤ 0.3）；分差 ±1 略高估（2.6% vs 1.9–2.4%），影響 ±0.5 / ±1.5 這類極小讓分線約 0.5 個百分點。

## 11. Selected production distribution & artifact integration

`spec.DISTRIBUTION_SPEC`（**`dist-v2`**）：四個 target 皆 `gaussian`、不分情境、**location = `zero`（評測後修正，凍結）**；分差 / 總分 / 上半場分差兩個 profile 共用，上半場總分分開。（原始預先登記版本為 dist-v1：location = `oos_mean`。）

artifact schema **v2**（`spec.ARTIFACT_SCHEMA_VERSION = 2`，只接受 v2）：

```
profiles.<early|final>.distributions.<margin|total|h1_margin|h1_total> = {
  state: {schema, target, kind, mu, sigma, scale_model, z_cdf(empirical), bandwidth, n_fit, fit_start_utc, fit_end_utc},
  distribution_version: "dist-v2", spec, shared_profiles, oos_seasons,
  fit_set_summary: {n, coverage, rps}       # 擬合集上的覆蓋率（點預測 OOS、分佈參數為擬合值）
}
metadata.distributions = {version, spec, oos_rows, oos_seasons, sigma, mu}
```

- 重訓流程：建訓練表一次 → **walk-forward OOS**（`production/oos.py`，每季只用更早賽季訓練）→ 擬合分佈（`production/distribution_fit.py`）→ 再訓練 full-fit 點模型。分佈從不碰 full-fit 模型的殘差（測試以 spy 驗證被擬合的殘差 = OOS 殘差，且 in-sample 殘差較小）。
- Gates 新增：每個分佈 σ 有限且在 (2, 60)、μ 有限、OOS 樣本數 ≥ 100；存檔 → 重新載入後的線機率逐位元相同。OOS 不足 → 重訓失敗、不上線。
- 不相容（明確拒絕）：schema v1（C.5D，沒有分佈）、dist-v1（μ = OOS 平均的舊版分佈）、缺 profile / target 分佈、分佈狀態 schema / 類型 / 格點 / 單調性不合法、target 不符。

目前 production：`ml-v2.0+20261004T0339Z.20261004T033948Z`（cutoff 2026-10-04 03:39 UTC，dist-v2），OOS 42,064 列（2022-23 ~ 2025-26），μ = 0，σ：分差 13.80、總分 18.67、上半場分差 11.15、上半場總分 12.21。先前兩個版本（C.5D schema 1、C.5E dist-v1 `…20261004T0306Z…`）保留在磁碟（不刪除），但目前程式會拒絕載入——回滾只能在 dist-v2 版本之間進行。

## 12. Production inference interface（`core/production/probability.py`）

```python
predict_margin_probability(game_prediction, line, art)       # 也有 total / h1_margin / h1_total
predict_line_probability(art, profile, target, pred, line, context=None, flags=None)
line_probability_from_prediction_row(row, target, line)       # predictions 表的一列 → 用當時的 artifact 版本重算
```

輸出：`line`、`probability_above`、`probability_push`、`probability_below`、`point_prediction`、`distribution`、`distribution_version`、`center`、`scale`、`artifact_version`、`model_version`、`profile`、`fit{n_fit, fit 期間, oos_seasons, shared_profiles, location_rule, fit_set_coverage}`、`data_quality{flags, affects_distribution=false, note}`。決定性（同輸入逐位元相同，有測試）。不含 edge / EV。

`features_json` 新增（前端 contract 不變；新 key 由既有 key-value 表顯示）：`distribution_context`（min_gp、傷病是否已知、季後賽）與 `predictive_distributions.<target>`（distribution、版本、center、scale、80% / 95% 整數預測區間）。

範例（2026-27 開幕週真實賽程，dry-run、不寫 DB；production artifact dist-v2，μ = 0；**線是合成的，不是盤口**）：

| 比賽 | 點預測 | 線 | 結果 |
|---|---|---|---|
| BOS@DET | 分差 +1.61 | +1.5 / +2 / +2.5 | 主隊蓋盤 0.5181 / 0.4884（push 0.0297）/ 0.4884 |
| BOS@DET | 總分 223.1 | 217.5 / 223 / 223.5 | 大 0.6180 / 0.4916（push 0.0214）/ 0.4916 |
| PHI@NYK | 分差 +7.05 | 0（主勝）/ +6.5 / +7 | 0.7001（邏輯迴歸勝率 0.714）/ 0.5293 / 0.4996（push 0.0296） |
| PHI@NYK | 上半場分差 +4.35 | 0 | 主 0.6350 / 平 0.0332 / 客 0.3319 |
| OKC@SAS | 分差 +1.46 | 0 / +0.5 | 皆為 0.5433（全場分差不會是 0） |

（這三場都是開季第一場：`season_opener / low_sample / early_season / continuity_unknown` 旗標存在，但不改變分佈寬度，見 §7。）

## 13. Tests

| 要求 | 測試 |
|---|---|
| 殘差擬合只用過去 / OOS | `test_walk_forward_fit_uses_only_earlier_weeks`、`test_distribution_fit_uses_only_walk_forward_oos_residuals`、`test_future_games_do_not_change_distributions` |
| 機率隨線單調 | `test_probability_monotone_in_line_and_sums_to_one`、`test_line_probability_interface` |
| 機率總和 | 同上、`test_integer_line_splits_win_push_loss_consistently` |
| push 行為 | `test_push_only_on_integer_lines_and_margin_never_pushes_at_zero`、`test_line_probability_interface` |
| empirical / Gaussian 可重現 | `test_fit_is_reproducible_and_state_roundtrips`、`test_gaussian_probability_matches_closed_form`、`test_empirical_captures_skew_that_gaussian_misses` |
| 區間覆蓋率計算 | `test_coverage_and_pit_on_calibrated_simulation`、`test_coverage_counts_exactly`、`test_central_interval_matches_coverage_definition`、`test_rps_prefers_sharper_correct_distribution` |
| artifact 存取一致 | `test_bundle_contains_distributions_and_roundtrips`、`test_prediction_row_probability_reloads_artifact_by_version` |
| 分佈 schema 不相容 | `test_incompatible_distribution_schema_fails`（7 種）、`test_c5d_schema1_artifact_on_disk_is_rejected`、`test_invalid_states_are_rejected` |
| early / final | `test_profile_sharing_follows_spec`、`test_probability_uses_the_games_profile` |
| 資料品質情境尺度 | `test_scale_model_learns_abundant_signal_and_shrinks_rare_context`、`test_scale_is_clipped`、`test_data_quality_flags_do_not_change_distribution` |
| 不使用 in-sample 殘差 | `test_distribution_fit_uses_only_walk_forward_oos_residuals`、`test_too_few_oos_residuals_fail_retrain` |
| 決定性 | `test_line_probability_is_deterministic`、`test_line_probability_interface`、`test_line_probability_from_stored_prediction_row`（DB） |
| μ = 0 凍結 / 舊版拒絕 | `test_production_location_is_frozen_zero_and_oos_mean_still_reproducible`、`test_dist_v1_artifact_is_rejected` |
| 其他 | `test_quarter_line_components`、`test_probability_matrix_matches_single_game_function`、`test_reliability_bins`、`test_features_json_has_distribution_summary` |

**完整 `cd pipeline && pytest`：305 passed（既有 260 + 新增 45；541 秒，含暫存 schema DB 測試；μ = 0 修正後重跑）。** Node smoke tests 未重跑：未修改 API / 前端 / DB schema（只在 `features_json` 新增 key）。

## 14. Limitations

1. **production 的 μ = 0 是評測後修正**（§9）：沒有評測前的確認性證據，2026-27 是前瞻驗證期；若得分環境出現持續性的單向漂移，μ = 0 也會產生偏差（屆時以 2026-27 結果檢視，不在季中調整）。
2. 只有一個驗證賽季（2023-24，擬合集只有 2022-23 一季 + 同季較早週）；2022-23 的 OOS 來自只用一季訓練的 fold（沒有上季先驗可擬合），殘差特性與後面幾季不同。
3. 分差尾巴比高斯厚（95% 區間覆蓋 93.3%）；極端線（± 15 分以上）的機率會略高估把握。
4. 情境尺度只檢驗了預先指定的 4 個特徵；季後賽分差較寬、總分 σ 隨水準上升是一致但小的效應，未進 production。
5. pending_prior_game / stale_results / missing_box_recent 沒有歷史樣本，無法評估是否該加寬分佈。
6. 上半場總分的 profile 分開是不對稱規則造成（兩個 σ 實務上相同）。
7. production 分佈參數來自「較少賽季訓練的 fold」的 OOS 殘差，而 production 模型用全部賽季訓練、預期略準 → 分佈可能略寬（偏保守）。
8. 合成盤口線不是市場線；真實盤口在 Phase D 才會檢驗。
9. 全場分差的平手質量以重新正規化處理；分差 ±1 略高估。
10. 分差導出的獨贏機率 calibration slope 約 1.1（略保守），未校正；單一驗證季（2023-24）不足以證明非劣（CI 太寬）。

## 15. 是否可以開始 Phase D

**可以。**
- 位置參數已決定並凍結（μ = 0，dist-v2；評測後修正，2026-27 前瞻驗證）。
- 獨贏：優先使用分差分佈導出的 P(主勝)（§9b 非劣），邏輯迴歸勝率作一致性監控。
- Phase D 的 edge 一律以 `probability.py` 的輸出計算，並記錄 `distribution_version` / `artifact_version`；資料品質旗標只作提示（不放大或縮小機率）。
- 不再依 2024-25 / 2025-26 做任何模型或分佈調整。

## 附錄 A. 檔案

| 檔案 | 內容 |
|---|---|
| `pipeline/core/models/distributions.py`（新） | 分佈擬合（Gaussian / empirical / 情境尺度）、離散化、盤口線機率、區間、RPS / NLL / PIT / 覆蓋率 / reliability |
| `pipeline/core/production/oos.py`（新） | walk-forward OOS 點預測（production 相同流程） |
| `pipeline/core/production/distribution_fit.py`（新） | 重訓時的分佈擬合（只用 OOS） |
| `pipeline/core/production/probability.py`（新） | 盤口線機率介面 |
| `pipeline/core/jobs/c5e_evaluate.py`（新） | 本報告的完整評估（`python -m core.jobs.c5e_evaluate`，約 2.5 分鐘） |
| `pipeline/core/production/spec.py` / `retrain.py` / `artifact.py` / `inference.py` / `features.py` | schema v2、分佈規格、重訓整合、驗證、features_json 摘要、`build_training_frames` |
| `pipeline/tests/test_distributions.py`、`test_production_distributions.py`（新）、`test_production.py`、`test_production_db.py` | 新增 45 項測試 |

輸出（gitignore）：`artifacts/c5e_results.json`、`c5e_oos_predictions.csv.gz`、`c5e_eval_scores.csv.gz`。
