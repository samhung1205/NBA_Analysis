# 階段二：資料、模型與決策引擎進度總覽（Stage 2 Phase Summary）

> 由原 `README.md` 的「階段二進度」「Phase C ~ D.5」各節整理而來（2026-10-15 移出）。每個 Phase 的完整設計、評估與驗收見各自報告；本文只保留摘要、關鍵數字與指令。
> 各 Phase 報告中「尚未套用 / 尚未部署」的描述是**完成當時**的快照，今日狀態以 [project-audit-2026-10-15.md](project-audit-2026-10-15.md) 為準。

## 0. 總覽

| Phase | 內容 | 狀態 | 報告 |
|---|---|---|---|
| A | 資料基礎：fetcher、排程器、回填 | ✅ | — |
| B | Baseline：特徵工程、Elo walk-forward 回測 | ✅ | — |
| C | 勝負邏輯迴歸 ml-v1.0（優於 Elo），分差/總分/半場 XGBoost 持平 | ✅ | 本文 §2 |
| C.5A | 來源 fallback / 排程 / 傷病硬化 | ✅ | [phase-c5a-report.md](phase-c5a-report.md) |
| C.5B | 五季 box score / 衍生進階指標 / 歷史傷病快照 / 賽前特徵底座 | ✅ | [phase-c5b-report.md](phase-c5b-report.md) |
| C.5C | 時間尺度特徵 + walk-forward 重新評估，全指標優於 baseline | ✅ | [phase-c5c-report.md](phase-c5c-report.md) |
| C.5D | Production inference：ml-v2.0 版本化 artifact、每週重訓、預測排程 | ✅ | [phase-c5d-report.md](phase-c5d-report.md) |
| C.5E | 預測分佈：任意盤口線機率（含 push） | ✅ | [phase-c5e-report.md](phase-c5e-report.md) |
| D.1 | 盤口擷取與市場正規化 | ✅ | [phase-d1-report.md](phase-d1-report.md) |
| D.2 | 去水 / 模型機率 / edge / EV | ✅ | [phase-d2-report.md](phase-d2-report.md) |
| D.3 | qualification / push-aware Kelly / risk-v1 | ✅ | [phase-d3-report.md](phase-d3-report.md) |
| D.4 | 策略執行 / 結算 / paper ledger | ✅ | [phase-d4-report.md](phase-d4-report.md) |
| D.5 | 決策中心 / 實際下注 exposure / strategy bankroll | ✅（2 個缺口已本機修正、待部署） | [phase-d5-report.md](phase-d5-report.md)、[修正報告](phase-d5-fixes-2026-10-15.md) |
| 上線 | readiness 工具、HAR 匯入、auth fail-closed、scheduler-efficiency-v1 | ✅ | [production-runbook.md](production-runbook.md) |
| E.1 | Telegram 推播：ACTIONABLE / 每日摘要 / 系統告警（notify-v1） | ✅ 本機完成，未部署 | [phase-e1-telegram-report.md](phase-e1-telegram-report.md) |
| E.2 | 賽前裁判分派收集（referees-v1；只累積資料，不接模型） | ✅ 本機完成，未部署 | [phase-e2-referees-report.md](phase-e2-referees-report.md) |
| E（其餘） | 旅行距離、五人 lineup / on-off、line movement（選做） | 未開始 | — |

## 1. 資料來源與排程（C.5A 後）

| 資料 | 優先序（失敗自動往下） |
|---|---|
| 賽程 / 即時比分 / 逐節 | NBA CDN → stats.nba.com `ScoreboardV3` → ESPN |
| 基本 box score | NBA CDN `boxscore_<id>` → stats.nba.com `BoxScoreTraditionalV3` |
| 進階數據（pace/ORtg/DRtg） | 僅 stats.nba.com；失敗不中止，下次補 |
| 傷病 | `nbainjuries`（官方 PDF） |

每次嘗試與 fallback 狀態寫入 `data_sources`，系統狀態頁可見：被備援取代的來源標 `warn`、勝出的備援註記「備援 <primary>」、全部失敗標 `error`。

回填工具：C.5B 起歷史 box score 走 `python -m core.jobs.box_backfill`（NBA CDN）；舊的 `run_backfill.py`（stats.nba.com）在被封鎖的 IP 上不可用；`run_backfill_espn.py` 為 ESPN 備援（僅賽程 / 比分 / 逐節）。皆為 UPSERT、可中斷重跑；ESPN 暫存的 `espn:<id>` 賽事會在官方回填時被認領改寫。

排程細節（13 個 job + E.1 的 3 個推播 job、game-aware 閒置略過）見 [production-runbook.md](production-runbook.md) §2、§4b。

## 2. Phase C（2026-10）

walk-forward（以賽季為單位），評測 2024-25 + 2025-26 共 2,631 場，特徵皆為賽前可得（Elo、休息 / 背靠背、近況、交手、賽程）。

| 勝負（主隊勝） | accuracy | log loss | Brier |
|---|---|---|---|
| Elo baseline | 0.6697 | 0.6221 | 0.2150 |
| **ml-v1.0（邏輯迴歸）** | **0.6724** | **0.6046** | **0.2088** |

- 勝負用邏輯迴歸而非 XGBoost：驗證賽季上所有 XGBoost 設定（含校準）都輸給 5 特徵邏輯迴歸（log loss 0.638 vs 0.628）。特徵與 C 值只用評測賽季之前的資料選出，評測賽季只跑一次。
- accuracy 的優勢（+0.27 pt，約 7 場）不顯著，log loss / Brier 的進步才是穩的。
- 分差 / 總分 / 上半場（XGBoost）MAE 與 baseline 持平 → 由 C.5C 解決。

## 3. Phase C.5B — 歷史資料完整度與特徵底座

五季（2021-22 ~ 2025-26）box score、衍生進階指標、歷史傷病快照全數回填，並建立賽前特徵資料層（只建資料層，未重新調模型）。

```bash
cd pipeline
python -m core.jobs.box_backfill --workers 6      # 五季 box score（可續傳；--force 重寫）
python -m core.jobs.injury_backfill --shard 1/4   # 歷史傷病報告（可多 shard 並行）
python -m core.jobs.derive_metrics                # 補算/重算 team_game_derived
python -m core.jobs.coverage_audit                # 覆蓋率稽核（--missing team_stats、--json）
python -m core.jobs.injury_validation             # 傷病狀態 vs 實際出賽（診斷用）
python run_build_features.py --offset 0           # 賽前特徵 → pipeline/artifacts/*.csv.gz
```

- Migration `0003_phase_c5b.sql`：`team_game_derived`、`injury_reports` / `injury_report_entries`、`source_fetch_log` 等。
- 傷病語意（`core/injury_asof.py`）：球員不再被列出 = `Not Listed`（視同可出賽）；該隊 NOT YET SUBMITTED = 無資訊（往前找較早報告）；沒有任何報告涵蓋 = `Unknown`。

## 4. Phase C.5C — 時間尺度建模

以「本季這支球隊」為主體重建特徵（pregame-v2：本季 / l20 / l10 / l5、上季先驗 + 經驗收縮、名單延續性、傷病缺陣機率、分鐘加權可用性）。

| 2,631 場 | Elo / baseline | Phase C | **C.5C 選定模型** |
|---|---|---|---|
| 勝負 log loss / Brier / acc | 0.6221 / 0.2150 / 0.6697 | 0.6046 / 0.2088 / 0.6724 | **0.5922 / 0.2036 / 0.6792** |
| 分差 MAE | 11.34 | 11.39 | **10.98** |
| 總分 MAE | 15.22 | 15.25 | **14.93** |
| 上半場分差 / 總分 MAE | 8.96 / 9.89 | 8.95 / 9.87 | **8.87 / 9.64** |

勝負 / 分差改善主要來自傷病與球員可用性，總分類來自本季節奏 / 得失分；XGBoost 在驗證賽季全數輸給邏輯迴歸 / Ridge，未採用。重現：`python -m core.jobs.c5c_evaluate`（只評測，不寫 DB）。2026-10-15 稽核已完整重跑並與既有結果相同。

## 5. Phase C.5D — Production inference

C.5C 模型以 `model_version='ml-v2.0'`（特徵 pregame-v2.1）上線。

```bash
cd pipeline
python run_retrain.py                  # 重訓 → 上線前檢查 → 原子寫入 artifacts/production/ → promote
python run_retrain.py --list           # 版本清單；--rollback [--to <版本>]
python run_predict.py --kind early     # 未來 36 小時比賽（--dry-run 不寫入）
python run_predict.py --kind final     # 開賽前 5~75 分鐘、尚無 final 版本者
python run_predict.py --kind refresh   # 有新傷病報告才重算，實質變化才寫入
```

- 排程（台灣）：12:20 early、每 5 分鐘 final（≈ 開賽前 60 分鐘）、:07/:22/:37/:52 傷病觸發 refresh、週一 16:00 重訓。
- artifact 自足（模型、特徵順序、scaler、收縮參數、補值、傷病校準、cutoff / metadata），版本不相容會明確失敗。
- `predictions` 同一輸入重跑不新增（input_hash），實質變化才新增一筆（`features_json.supersedes` 可追蹤）。
- `elo_ratings` 表已過期，production 改用重播的 Elo（`core/models/elo_state.py`）。

## 6. Phase C.5E — 預測分佈

點預測 → 預測分佈 → 任意盤口線的 P(高於) / P(push) / P(低於)。

- 只用 walk-forward 樣本外殘差擬合（`core/production/oos.py`），每週隨重訓更新。
- 四個 target 皆全域高斯 + 連續性修正離散化；情境尺度沒有樣本外改善 → 資料品質旗標只作 UI 警示。
- 評測：80% / 95% 區間覆蓋 分差 79.5 / 93.3%、總分 79.8 / 94.8%；整數線 push 機率與實際相符。
- production 分佈中心 μ = 0（dist-v2）是**評測後的修正**，2026-10-04 起凍結，**2026-27 賽季為前瞻驗證期**。
- 獨贏優先使用分差分佈導出的 P(主勝)，邏輯迴歸勝率作一致性監控。
- 介面：`core/production/probability.py`；重現：`python -m core.jobs.c5e_evaluate`（約 2.5 分鐘）。

## 7. Phase D.1 — 盤口擷取與市場正規化

```bash
cd pipeline
python run_odds.py --source all --dry-run                       # 抓取 + 解析 + 比對，不寫入
python run_odds.py --source oddsapi                             # 需 ODDS_API_KEY
python run_odds.py --source twsport --from-file capture.har     # 台彩 HAR 匯入（現行建議用 run_twsport_har.py）
python run_odds.py --print-next-runs
```

- 正負號唯一定義：`pipeline/core/odds/canonical.py`。`line` = 主隊顯示讓分（主讓 5.5 → −5.5），`model_threshold` = +5.5，主隊過盤 ⇔ margin > threshold。
- 快照：每個 (game, source, bookmaker, market) 是時間序列；內容沒變只更新 `last_seen_at`，有變就新增一列，舊列永不改寫。
- 台彩自動擷取被 Cloudflare 擋（403），不繞過 → job 回報 `blocked`，改走 HAR。

## 8. Phase D.2 — 去水 / edge / EV

```bash
cd pipeline
python run_pricing.py --dry-run                                   # 未來 48 小時（不寫入）
python run_pricing.py                                             # 寫入 market_pricing_snapshots（冪等）
python run_pricing.py --game-id 123 --as-of 2026-10-21T22:00:00Z  # 歷史時點重建
```

- 唯一實作 `pipeline/core/pricing/`；Node API 只讀 `market_pricing_snapshots`。
- 定義（凍結）：`raw_implied_prob = 1/odds`、`market_overround = Σraw − 1`、`fair_no_vig_prob = raw/Σraw`、`edge_vs_fair = model − fair`、`ev_per_unit = P(win)·(odds−1) − P(loss)`（整數線 push 退本金）。
- 台彩上半場三向：主 / 和 / 客一起去水，和局是 outcome；兩向上半場獨贏結算不明 → 不算 edge / EV。
- 時間對齊：只用 `fetched_at ≤ T` 的盤口與 T 以前的預測。

## 9. Phase D.3 — Kelly sizing 與 risk-v1

```bash
cd pipeline
python run_sizing.py --dry-run                     # qualification + Kelly + exposure（不寫入）
python run_sizing.py --dry-run --bankroll 10000    # 另外顯示 stake_amount
python run_sizing.py                               # 寫入 bet_sizing_snapshots（排程已在定價後自動執行）
```

- Kelly（凍結）：f* = EV / (b·(1 − p_push))，b = 賠率 − 1；EV ≤ 0 → 0；不使用 edge_vs_fair。
- **risk-v1（凍結，非 ROI 調出）**：full × 0.25 → 單筆 ≤ 2% → 同場合計 ≤ 3% → 同一 betting day 合計 ≤ 8%；超過等比例縮放。
- Qualification 狀態：`eligible` / `exposure_scaled` / `no_positive_ev` / `stale_quote` / `mutually_exclusive_positive_kelly` / `unsupported_settlement` / `market_not_open` / `no_prediction` …；都不是「推薦」。
- 報價過期門檻：last_seen 超過 2 × 輪詢間隔（台彩 60 分、Odds API 12 小時）→ stale。

## 10. Phase D.4 — 策略執行、結算與 paper ledger

```bash
cd pipeline
python run_strategy_backtest.py --fixture --summary                                  # 合成驗證 slate（非 ROI 證據）
python run_strategy_backtest.py --source twsport --from 2026-10-20 --to 2026-12-31   # 只用真實觀測台彩快照
python run_paper.py --dry-run                                                        # T = 開賽 − 60 分重建決策
python run_paper.py --report                                                         # prospective paper 績效
```

- execution-v1（看到任何 ROI 之前凍結）：T = 開賽 − 60 分；betting day = Asia/Taipei；同日依 T 排序；stake = day_start_bankroll × D.3 final × 剩餘 8% 額度縮放。
- 每場都有 decision：`bet` 或 `no_bet`（`no_odds` / `stale_odds` / `no_positive_ev` / `portfolio_cap` / `decision_window_missed` …）。
- settle-v1：只有 final 才結算；缺比分 / 取消 / 延期 → `ungradable`（不猜 void）。
- 指標：`yield`、`bankroll_return`、max drawdown、log growth；bootstrap 以 betting day 為單位、≥ 30 天才輸出 CI。
- Evidence：歷史引擎只用 observed 盤口；正式 DB 2024-25 / 2025-26 盤口 0 筆 → **無歷史投注績效證據**。

## 11. Phase D.5 — 決策中心與實際下注

```bash
cd pipeline
python run_decision.py --dry-run --summary     # 結算 / ledger / 決策檢視（不寫入）
python run_decision.py                         # 寫入（排程 decision_board 每分鐘執行）
```

- 三個概念分開：model opportunity（台彩）／paper decision（D.4）／actual bet（使用者手動記錄；平台從不自動下注）。
- decision-v1：D.3 單筆上限 → 同場剩餘 max(0, 3% − 實際) → 當日剩餘 max(0, 8% − 實際) → 等比例縮放；分母 = 當日凍結的 day_start_bankroll。
- Override：超額 / 重複 / 無法驗證 → 409 要求明確確認並記錄原因；從不自動截斷金額。
- 稽核：bets 執行欄位不可改、不可刪（只能作廢 / 更正）；`bet_events`、append-only `bankroll_ledger`、凍結 `bankroll_day_snapshots`；`risk_state_claims` 防止兩個分頁同時用掉同一份額度。
- 所有 exposure / 額度 / bankroll 數學只在 Python（`pipeline/core/decision/`）。
- 2026-10-15 後續修正：更正須明確確認並標 `missing_context`；過期物化不可出金；原始報告保留為歷史快照，詳見 [修正驗收](phase-d5-fixes-2026-10-15.md)。本機已驗證、尚未部署。

## 12. 上線後強化（2026-10-05 ~ 10-06）

- `run_production_check.py` / `run_data_audit.py` / `run_twsport_har.py`：唯讀 readiness、帳號資料稽核、安全的 HAR 匯入。
- 網站 auth fail-closed（`SESSION_SECRET` 不合格即拒絕）、有使用者後預設關閉註冊。
- scheduler-efficiency-v1：高頻 job 先查 DB 是否有事可做，閒置直接略過（runbook §4b）。
- PBKDF2 100,000 次 + 新密碼 ≥ 12 字元（[dev-pitfalls.md](dev-pitfalls.md) §10）。

## 13. 路線圖：Phase E（選做）

- 球員 / lineup 層級模型、旅行 / 裁判特徵
- Line movement 特徵（需先累積真實盤口時間序列）
- ~~Telegram 推播~~：E.1 已完成（notify-v1，見 [phase-e1-telegram-report.md](phase-e1-telegram-report.md)）；LINE 未實作（LINE Notify 已停止服務）

新模型須保留版本與時間切分，避免與凍結的 ml-v2.0 / dist-v2 / risk-v1 / execution-v1 證據混用。建議順序見 [project-audit-2026-10-15.md](project-audit-2026-10-15.md)。
