# Phase D.5 — Decision Dashboard, Bets-Aware Exposure & Production Readiness 報告

日期：2026-10-05　範圍：把 Prediction → Odds → No-vig / EV → Kelly sizing → T-60 paper decision → Settlement 整合成每天可操作的
personal decision-support platform；**使用者真的已下注的 bets 納入同一個 risk-v1 預算**；決策中心 / 記錄下注 / strategy bankroll /
evidence / system readiness。
**未做**：修改 C.5 模型、dist-v2、proportional-v1、risk-v1、execution-v1；以 ROI 調任何參數；最低 EV 門檻；best-book / consensus；
以國際盤替代台彩；自動下注 / 登入 bookmaker；把 paper bet 寫入 bets 或把 actual bets 當 paper 證據；造 production odds / predictions；
**未對 production Supabase 套用 0008**；未 commit。Phase D 到此結束。

## 0. 結論摘要

| 項目 | 結果 |
|---|---|
| 三個概念 | model opportunity（`decision_opportunities`，台彩 primary）／paper decision（D.4 `paper_strategy_*`，只對照）／actual bet（`bets`，使用者手動記錄）——表、欄位、API、UI 全部分開 |
| Actual exposure | Python `core/decision/exposure.py`：Σ 有效 stake ÷ 當日凍結 day_start_bankroll；已結算當日注單仍占用；legacy / 手動 / 缺 game 的保守處理 |
| Rescale | 台彩-only D.3 `single_bet_capped_fraction` → remaining_game = max(0, 3% − actual_game) → remaining_day = max(0, 8% − actual_day) → 等比例縮放；**無實際下注時與 D.3 final 逐位元相同**（測試） |
| risk-v1 | **未修改**；沒有 risk-v1.1；decision-v1 只是把 actual bets 放進同一預算 |
| Already recorded | 同 game × market 已有有效實際下注 → 新增額度 0（不 top-up，看到更好賠率也不補）；actual > theoretical → `over_theoretical_target` 警示 |
| Override policy | **B. explicit override**：超出 risk-v1 / decision-v1 額度、重複機會、無法驗證 → 409 要求明確確認（+ 原因），記錄 `user_override` / `missing_context` / `outside_model`；**從不自動截斷 stake** |
| Bankroll | migration 0008：`bankroll_accounts` / append-only `bankroll_ledger` / 凍結 `bankroll_day_snapshots`；沒有可覆寫的 balance 欄位；金額合計只在 Python |
| Bet 稽核 | bets 執行欄位不可改、不可 DELETE（trigger）；void / correction（新列 supersedes 舊列）；`bet_events` append-only；idempotency key |
| 併發 | `risk_state_claims` PK (account, version)：兩個分頁用同一版本同時記錄 → 第二筆整個交易 rollback（DB 測試 + D1 smoke） |
| 物化 | `decision_board` 排程每分鐘：settle-v1（canonical 注單）→ ledger 損益 → `decision_snapshots`（內容指紋；不同 → 新列，舊列保留）；Node 以版本號判斷 `risk_recalculation_pending` |
| TS / JS 數學 | 0 處（新增掃描測試涵蓋 exposure / capacity / bankroll / user_adjusted 等字詞的 + − × ÷ 與 Math.min/max/floor/round） |
| Evidence | historical model = available；historical betting = **unavailable**；prospective paper 累積中、< 30 graded days → insufficient；badge 固定「Prospective validation / 尚在前瞻驗證」 |
| Production | 0001–0007 已套用；**0008 未套用**（需要 `npm run db:migrate:pg`）；未來賽程 / 盤口 / ml-v2.0 預測目前為 0 → 正確狀態為 waiting / no_schedule / no_odds |
| 測試 | 新增 57 項（純邏輯 47、DB 10）；完整 `pytest` **682 passed**（既有 625 + 57）；`npm run build` 通過；Node smoke（本機 D1，0001–0008 + seed）**160 / 160**（原 106 + D.5 54） |

---

## 1. Audit（先 audit，再實作）

### 1.1 既有 bets / bets API / bets UI（D.5 以前）

| 項目 | 現況 |
|---|---|
| 欄位 | `id, user_id, game_id (NOT NULL FK), market, selection, line, odds, stake, result, payout, note, placed_at (default NOW)` |
| game 連結 | **有**（NOT NULL）→ 任何舊注單都能算 game / betting-day exposure |
| bookmaker / source | **沒有**（UI 文案要求填「台彩實際賠率」，但未儲存來源） |
| market / side | 有（`market` 代碼 ml/spread/…，`selection` home/away/over/under）；**沒有和局**（台彩上半場三向無法記錄） |
| placed_at | 有，但是 DB 寫入時間（使用者無法填實際下注時間），沒有獨立的伺服器記錄時間 |
| 可追溯 odds / pricing / sizing | **沒有** |
| bankroll 概念 | **沒有**（D.3 只有比例、D.4 是 normalized units） |
| 可修改 / 刪除 | `PATCH` 只改 result / payout（無稽核）；`DELETE` **硬刪除**（稽核軌跡消失） |
| 金額計算 | `/api/bets` 摘要與 `PATCH` 的 payout 在 TS（`stake × odds`）——結算帳務，不是 sizing（D.3 掃描已明確豁免） |
| 狀態語意 | result：pending / win / lose / push / void；payout = 返還總額 |
| UI | `/bets` 表單 + 表格 + 結算下拉 + 垃圾桶（刪除）；總覽卡片「記錄投注」連到 `/bets?game_id=` 無任何預填 |
| Dashboard | `/` 每場卡片：台彩 vs 國際盤混在同一表、「最佳 edge」標示；`/games/:id` 有 D.2 定價與 D.3 理論注碼表；**沒有 actual exposure、沒有決策狀態** |
| Paper ledger vs bets | D.4 完全分離（normalized、strategy_id、無 user_id），D.4 不讀不寫 bets |
| Workers / Python 分界 | Workers 不能跑 Python；D.2 / D.3 已採「Python 計算 → 寫表 → Node 只讀」；`src/lib/sizing.ts` / `pricing.ts` 只序列化（有掃描測試） |
| Auth | HMAC cookie session，`getCurrentUser`；bets 端點 401 保護 |
| Scheduler / health | APScheduler（`core/scheduling.py`）、`data_sources` 心跳（nba_cdn / nba_api / espn / nbainjuries / model_predict / twsport / oddsapi / market_pricing / bet_sizing / paper_strategy） |

### 1.2 分類

**A. 可直接沿用**：bets.game_id / stake / odds / market / selection；auth；D.3 `select_candidates` / `size_portfolio` / risk-v1；D.4 `StrategyScope`（twsport:twsport）、`quote_is_stale`、settle-v1 `settle` / `profit`、evidence 與 day-block bootstrap（≥ 30 天）、`load_ledger`；`data_sources` 心跳；seed 產生器模式；D.3 TS 掃描測試。

**B. 必須擴充**：bets provenance（source / bookmaker / canonical 市場 / 參考盤口 / 定價 / sizing / decision 列 / paper decision）、origin、strategy_compliance、override、idempotency、record_status（void / superseded）、betting_day、bankroll basis、和局選項；bets 稽核（不可改、不可刪、event log）；bankroll（完全沒有）；actual-bet exposure；使用者決策物化；Db adapter 交易；決策 dashboard；system readiness。

**C. 不應混用**：`paper_strategy_*`（策略證據、normalized）vs `bets`（真實金額）；`bet_sizing_snapshots.final_stake_fraction`（跨來源、無實際下注的理論組合）vs D.5 使用者額度；Odds API sizing vs 台彩可操作額度；`model_metrics.sim_roi`（seed）vs 投注證據；`/api/bets` 舊摘要 ROI vs 策略績效。

### 1.3 環境 audit 發現（與程式無關，但影響驗證）

repo 位於 iCloud 同步的 Desktop，且啟用「最佳化 Mac 儲存空間」：約 1.4 萬個檔案（`node_modules`、`pipeline/.venv`、`.git/objects/pack`、`dist`、部分 artifacts）被移出本機成為 *dataless* placeholder。
症狀：`git log` 回報 pack「far too short」、`vite build` 讀到空的 undici 檔案而失敗、Python 匯入 scipy / sklearn 卡住。
依使用者指示由使用者在 Finder 恢復（Keep Downloaded）；本階段沒有重新安裝任何套件、沒有修改 `.git`。驗證結果見 §19。

## 2. Final architecture

```
Python（唯一數學）                                                          Node / Workers（只讀 + 記錄事實）
pricing_job ─┐                                                            GET /api/decision-board ─ 讀 decision_snapshots
sizing_job ──┤ market_pricing（每 5 分）                                    POST /api/bets ──────── 比較 stake 與 Python 物化上限
paper_strategy（每 5 分；D.4 T-60）                                                                  → claim 版本 + INSERT（同一交易）
decision_board（每分鐘；D.5）                                               POST /bets/:id/void|correction、PATCH（手動結算）
  1. settle_actual_bets      canonical 注單 → settle-v1                      GET/POST /api/bankroll(/entries) ─ ledger append
  2. sync_settlement_ledger  bets 結算 → ledger 損益（append-only）
  3. materialize             users × betting days → board.build_board
       台彩-only D.3（as_of = now）→ actual exposure → rescale
       → decision_snapshots / decision_opportunities / bankroll_day_snapshots
```

新增 Python 套件 `pipeline/core/decision/`：`policy.py`（decision-v1 凍結）、`exposure.py`、`bankroll.py`、`board.py`、`evidence.py`、`job.py`、`seed_fixture.py`；CLI `pipeline/run_decision.py`。
Node：`src/routes/decision.ts`、`src/lib/decision.ts`（序列化 / 版本比較 / 上限比較）、`src/db/decision-queries.ts`、`src/db/index.ts`（`atomic()`）；頁面 `/decision`、`/bankroll`。

## 3. Opportunity vs paper vs actual

| | Model opportunity | Paper strategy decision | Actual user bet |
|---|---|---|---|
| 產生者 | Python（D.2 / D.3 + D.5 rescale） | Python（D.4 execution-v1，T-60 一次） | 使用者（外部下注後手動記錄） |
| 儲存 | `decision_opportunities` | `paper_strategy_decisions / _bets` | `bets`（+ `bet_events`） |
| 單位 | bankroll 比例 + 由 day-start 換算的金額 | normalized units | 真實金額（使用者幣別） |
| 證據 | 不是證據（理論） | prospective paper（台彩策略證據） | User actual betting record（不是策略績效） |
| UI | 決策中心 game card | 決策中心卡片上的「Paper（T-60）」一行 + Evidence 面板 | RECORDED 狀態、/bets、/bankroll |

規則：看到 opportunity 不會建立 bet；paper bet 永遠不寫 bets；actual bets 永遠不進 paper metrics（DB 測試確認 decision job 後 paper 表仍為 0）。

## 4. Evidence-aware language

- 狀態文字：Qualified opportunity / Positive EV / Theoretical sizing / Risk-adjusted capacity / Paper decision / Actual bet logged / Blocked。
- 不使用 proven edge、guaranteed、high-confidence winner、必買、穩贏、強推；按鈕為「記錄下注」（不是「立即下注」）。
- 固定 badge「Prospective validation / 尚在前瞻驗證」（decision-v1 不會自動升級）。
- 排序（display_rank：actionable → EV 高 → 開賽早）只供閱讀，不影響 qualification / 額度；所有 EV > 0 且 eligible 的都顯示（無 3% / 5% / 10% 門檻；測試：EV +0.1% 仍 qualified）。

## 5. Bankroll design（bankroll-v1）

| 表 | 內容 / 規則 |
|---|---|
| `bankroll_accounts` | 每位使用者一個；currency；`risk_state_version`（每次使用者異動 bets / ledger 前進一號）；不可刪、身分欄位不可改 |
| `risk_state_claims` | PK (account_id, version)：每次異動在同一交易內認領下一號（併發保護）；不可改 / 刪 |
| `bankroll_ledger` | append-only：initial_funding（僅一次）/ deposit / withdrawal（正數，方向由類型決定）/ adjustment（有號、≠ 0、必須原因）/ reversal（沖銷某一筆使用者輸入，金額由 Python 計算、每筆最多沖銷一次）/ bet_settlement / bet_settlement_reversal（pipeline）；CHECK 約束 + trigger |
| `bankroll_day_snapshots` | 每 account × betting day 一列，凍結（不可改 / 刪） |

- current = Σ 有號 ledger；committed / open stake = 有效且尚未有結算紀錄的注單 stake；available = current − open。
- 結算損益：settle-v1 同一公式（win = stake·(odds − 1)、lose = −stake、push / void = 0）；結算改變 / 作廢 → 先沖銷再入帳（測試：audit sum = 初始資金 + 最終損益）。
- 不連銀行 / bookmaker；UI 明示「手動維護的 strategy bankroll」。

## 6. Day-start bankroll（execution-v1 語意）

- 凍結時點 `basis_as_of`：該 betting day 第一筆實際注單的記錄時間；沒有注單 → 第一次出現台彩 participant 的物化時點（= 第一個 decision 之前）。
- `day_start = ledger_balance(basis_as_of) − 其他 betting day 在 basis_as_of 時仍未結算的 stake`（與 D.4 `prior_unresolved_stake` 相同保守規則）。
- 當日所有比例以 day_start 為分母；當日中途存入 / 結算損益不改變基準（測試：中途 +5,000 存入與 +900 結算 → 比例仍 / 10,000）；次日重新凍結。
- ≤ 0 或沒有 bankroll → `bankroll_unavailable`：仍顯示理論比例，但**不產生任何可操作金額**；有實際下注卻沒有基準 → 新增比例也不計算（不猜）。

## 7. Actual-bet exposure

- 只計 `record_status = active`（void / superseded 不計）；已結算的同日注單仍計入（測試：win / lose / pending 三種情況新增額度相同）。
- betting day：新紀錄寫入時凍結；legacy（NULL）由比賽開賽日期導出。
- 不完整：stake 不合法 → 該日 `actual_exposure_incomplete`（全部新額度 0）；betting day 無法確定 → 所有日；有 day 沒有 game → 計入 daily，並保守地占用**每一場**的同場額度；legacy 照常計入並警示 `legacy_unlinked_bet`。
- 結果寫入 `decision_snapshots.actual_exposure`（day / per-game stake 與比例、remaining、over-limit 旗標、每筆注單的計入狀態）；`bets.stake_fraction_at_placement` / `bankroll_day_snapshot_id` 由 Python 補一次（之後不可改）。

## 8. User-adjusted risk calculation（decision-v1）

```
candidates = 台彩（twsport:twsport）D.3 size_portfolio(as_of = now) 中 eligible / exposure_scaled 的 outcome，排除已記錄的 game × market
Step 1  single_bet_capped_fraction（≤ 2%；D.3）
Step 2  remaining_game = max(0, 3% − actual_game)；Σ_game > remaining_game → × remaining_game / Σ_game
Step 3  remaining_day  = max(0, 8% − actual_day) ；Σ_day  > remaining_day  → × remaining_day / Σ_day
→ user_adjusted_fraction；max_additional_stake_amount = × day_start；suggested = 向下取整到 1 個貨幣單位
```

- 不從 D.3 `final_stake_fraction` 開始（那是「沒有實際下注」且跨來源的組合）；台彩-only 與 D.4 scope 先過濾一致。
- 等比例、不依 EV 排名、不挑最佳。
- 例（測試）：4 場 × (ML 2% + 大分 2%)：無實際下注 → 每筆 1%（= D.3）；其他場已下 5% → 當日剩 3%、每筆 × 0.25；同場其他玩法已下 2% → 該場剩 1%；當日已下 8% → `risk_cap_reached`；10% → `actual_exposure_over_limit`（剩 0%，不用負數，不改歷史注單）。

## 9. Already-recorded handling

- 同 game × market 有效實際下注（任何來源，含手動）→ 該市場所有 outcome `already_recorded`、新增 0、`no_top_up:decision-v1`；同一邊：`linked_actual_fraction`，> 理論 → `over_theoretical_target`（不要求撤單），< 理論 → `below_theoretical_target_no_top_up`。
- 之後出現更好賠率的新快照 → 仍 already_recorded（測試）。
- 已記錄注單的 stake 照樣計入 actual exposure。

## 10. Override policy（§26 決定：B. explicit override）

理由：這是個人紀錄工具，注單是**已在外部發生的事實**；hard block 會讓 actual exposure 少算（更危險）。因此：

| 檢查（server-side，以最新 DB 狀態） | 需要 | 記錄的 compliance |
|---|---|---|
| stake > 單筆 2% / 同場剩餘 / 當日剩餘（Python 物化金額） | 明確確認 + 原因 | `user_override` |
| stake > decision-v1 建議新增額度 | 明確確認 + 原因 | `user_override` |
| 機會已記錄（top-up） | 明確確認 + 原因 | `user_override` |
| 連結的機會目前不是 qualified | 明確確認 | `outside_model` |
| 沒有 bankroll / 尚未物化 / 版本不一致（重新計算中） / day-start 不可用 | 明確確認 | `missing_context` |
| 平台 qualified 機會且在額度內 | — | `compliant` |
| 手動輸入、在額度內 | — | `manual_unlinked` |

- 回 409 `confirmation_required`（checks、compliance_if_confirmed、limits）；確認後保存 `override_reason`、`override_confirmed_at`、完整 `risk_check`。
- **從不**自動截斷 stake；user_override / outside_model / missing_context 不視為策略合規。
- 國際盤 diagnostic 機會不能當平台機會記錄（400）；若真的在其他管道下注，用手動記錄。

## 11. Decision dashboard（`/decision`）

- 上方：betting day、day-start、目前 / 可用 bankroll、當日實際 exposure（進度條：Python 物化 `day_cap_used_share`）、risk-v1 上限、剩餘額度、機會 / 實際下注數、無台彩盤口 / 過舊場數、evidence badge。
- 狀態 banner：0008 未套用、尚未物化、risk recalculation pending、物化超過 10 分未確認（降級）、bankroll 未設定、exposure 不完整、當日超限、系統降級。
- Game card：開賽、對戰、ACTIONABLE / REVIEW / RECORDED / BLOCKED / NO EDGE（icon + 英文 + 中文，不只靠顏色）、預測摘要（分差 / 總分 / 上半場）、主力缺陣 / 資料品質警示、同場實際 / 剩餘、Paper（T-60）、台彩 outcomes、國際盤（收合）。
- Progressive disclosure：第一層 = 玩法 / 方向 / 賠率 / EV / 新增額度 / 狀態；展開 = 原始隱含、去水、模型 P(勝/退/輸)、edge（診斷）、Full / ¼ Kelly、單筆上限後、D.3 理論、同場 / 當日剩餘、縮放因子、盤口時間、artifact / pricing 版本、已記錄注單、paper decision。
- 右側：Evidence 面板、System readiness（schedule / predictions / injuries / Taiwan odds / Odds API / pricing / sizing / paper / decision 物化 / bankroll / actual exposure：healthy / waiting / stale / blocked / not_configured / error + 最後成功時間）。
- Mobile：卡片優先；outcome 用 grid 換行（狀態 / 按鈕換到下一列），沒有需要水平捲動的關鍵資訊；國際盤小表格在收合區內。
- 每分鐘自動重新載入（modal 開啟時暫停）。

## 12. Taiwan vs international

- 可操作額度只來自 `twsport:twsport`；Odds API 只輸出 diagnostic（bookmaker、線、賠率、EV），`status_group = diagnostic`、沒有額度、標「International diagnostic · Not Taiwan Sports Lottery evidence」。
- 台彩無新鮮盤口 → 台彩 actionable = unavailable，**不 fallback 國際盤**；原因分類：`not_yet_published` / `ingestion_unavailable`（Cloudflare 擋、需 HAR 匯入，UI 附指令）/ `stale` / `market_closed` / `not_configured` / `outside_window`（> 48h 定價視窗）/ `game_started`。

## 13. Persistence / migration（0008）

`migrations/postgres/0008_phase_d5.sql`、`migrations/d1/0008_phase_d5.sql`（`npm run db:migrate:local` 已加入）：

- 新表：`bankroll_accounts`、`risk_state_claims`、`bankroll_ledger`、`bankroll_day_snapshots`、`bet_events`、`decision_snapshots`、`decision_opportunities`。
- `bets`：只新增欄位（全部 nullable 或有預設 `record_status = 'active'`）；舊列與舊 INSERT 語句不受影響（DB 測試）；`recorded_at` 舊列保持 NULL（不回填假時間）。
- Trigger：bets 不可 DELETE、執行欄位不可改、basis 欄位只能補一次、void / superseded 後凍結；ledger / day snapshot / events / claims / opportunities 不可改不可刪；decision snapshot 只有 `last_confirmed_at` 可前進；account 身分不可改、版本只增。
- FK：bets → `odds_snapshots`（參考快照）與 `decision_opportunities` 為 RESTRICT（稽核參照不會因清 cache 消失）；pricing / sizing / paper 只存 id（cache 表可清理，數字已複製進 `reference_context`）。
- 冪等：`decision_snapshots` UNIQUE (user, betting_day, input_fingerprint)；bets / ledger UNIQUE (…, client_request_id)；ledger settlement_key UNIQUE。
- seed 的 DELETE 在有真實資料的 DB 上會被 trigger 擋下（刻意保護；seed 只用於全新開發 DB）。

## 14. API changes

| 端點 | 說明 |
|---|---|
| `GET /api/decision-board?date=` | 需登入；summary / games[]（含 taiwan / international outcomes、paper、預測、缺陣）/ actual_exposure / risk_limits / bankroll / evidence / system_health / risk_state；前端不需自己 join |
| `GET /api/bets[?date=&include_inactive=1]` | 新增 provenance 欄位、`legacy`、`actual_performance`（Python）；舊 `summary` / `pnl_curve` 形狀保留 |
| `GET /api/bets/:id` | 單筆 + bet_events |
| `POST /api/bets` | 記錄下注（平台機會或手動）；server-side 檢查、409 確認、idempotency、版本認領 |
| `PATCH /api/bets/:id` | 手動結算（稽核事件；作廢後 409） |
| `POST /api/bets/:id/void`、`DELETE /api/bets/:id` | 作廢（不刪除） |
| `POST /api/bets/:id/correction` | 新列 supersedes 舊列 |
| `GET /api/bankroll`、`POST /api/bankroll/entries` | bankroll 摘要（Python）/ ledger / day-start；新增異動（出金只與 Python 物化的可用金額比較，物化過期 → 409 不猜） |

0008 未套用時：bets 端點退回 D.5 以前行為；decision-board 回 `unavailable` 並仍列出賽程；bankroll 回 `unavailable`。

## 15. UI changes

新增 `/decision`（導覽列第一個「決策中心」）、`/bankroll`（「資金」）；`/bets`：手動記錄有確認流程、和局選項、下注管道、策略合規 / 來源 badge、平台觀察賠率 vs 實際賠率、作廢 / 更正（不再有刪除）、顯示已作廢切換、文案改為 User actual betting record / 實際 yield。

## 16. Concurrency

- Node：每次異動在同一交易（D1 `batch` / Postgres `BEGIN … COMMIT`）內 `INSERT risk_state_claims (account, expected + 1)` → `UPDATE … WHERE version = expected` → INSERT bet → INSERT event。兩個分頁讀到同一版本 → 第二個違反 PK → 整個交易 rollback → 409 `risk_state_changed`（重新整理後重試）。
- 檢查使用最新 DB：同一請求內讀 account 版本、ledger / event watermark 與最新 snapshot；版本不一致 → 不採用舊額度（`missing_context` 確認）。
- 物化：REPEATABLE READ、先讀版本再讀 bets / ledger（物化結果的版本永遠不會新於它看到的資料）；`pg_try_advisory_xact_lock` 避免重疊執行。
- 重複請求：`client_request_id` 唯一 → replay 回同一筆。

## 17. Privacy / authentication

- decision-board / bankroll / bets 全部需登入（401）；所有查詢以 `user_id` 限定（他人注單 404，smoke 驗證）。
- API 不回傳 `DATABASE_URL`、`ODDS_API_KEY` 或任何設定；新程式沒有記錄 secrets（只記錄衝突錯誤訊息）。

## 18. Scheduler / materialization / system health

- 新 job `decision_board`：`CronTrigger(minute="*", second=30, Asia/Taipei)`，心跳 `data_sources.decision_board`（expected 1 分）；與 `market_pricing`（:01/:06…）、`paper_strategy`（:02/:07…）分開。
- 內容不變 → 只推進 `last_confirmed_at`；> 10 分未確認 → API `materialization_stale`（額度不視為有效）。
- 剛記錄注單 → API 立即顯示 `risk_recalculation_pending`（≤ 1 分鐘後由 Python 重算），不在 Node 重算。
- CLI：`python run_decision.py [--dry-run] [--summary] [--user N] [--date YYYY-MM-DD] [--now ISO]`。

## 19. Tests / results

新增 `pipeline/tests/test_decision.py`（47，純邏輯）、`pipeline/tests/test_decision_db.py`（10，暫存 schema `c5a_test_*`，結束 DROP；不碰 production `public`）；`conftest.py` TRUNCATE 清單加入 D.5 表。

| 要求 | 測試 |
|---|---|
| §41 actual exposure | `test_no_actual_bets_matches_theoretical_risk_v1_exactly`（逐位元 = D.3）、`test_actual_daily_5pct_leaves_at_most_3pct`、`test_actual_game_2pct_leaves_at_most_1pct_in_that_game`、`test_actual_day_at_or_above_8pct_means_no_new_stake`（8% → risk_cap_reached；10% → over_limit、剩 0、無負數）、`test_actual_game_at_or_above_3pct_blocks_game`、`test_settled_same_day_bet_still_consumes_budget`、`test_next_betting_day_resets_daily_exposure`、`test_stake_fraction_uses_frozen_day_start_and_midday_changes_do_not_alter_basis`、`test_day_start_frozen_on_first_need_with_basis_at_first_bet`、`test_legacy_unlinked_bet_counted_with_warning`、`test_manual_unlinked_bet_counted_in_exposure`、`test_incomplete_bet_data_is_conservative`、`test_bet_without_game_counts_against_every_game`、`test_voided_and_superseded_bets_do_not_count`、`test_day_start_frozen_mid_day_and_dry_run_writes_nothing`（DB） |
| §42 linked | `test_linked_actual_bet_is_already_recorded_and_no_top_up`（低於理論也不補）、`test_actual_stake_above_theoretical_gets_warning_not_cancellation`、`test_better_later_odds_do_not_create_duplicate_opportunity`、`test_recorded_bet_creates_new_snapshot_already_recorded_and_fills_basis`（DB：參考賠率 / 實際賠率都保存、參考快照 FK RESTRICT） |
| §43 bankroll | `test_ledger_signed_amounts_and_audit_sum`、`test_ledger_rejects_invalid_entries`（7 組）、`test_settlement_ledger_actions_win_change_void_and_idempotency`、`test_open_stake_and_day_start_exclude_other_days_open_bets`、`test_normalized_fractions_unaffected_by_currency_amount`、`test_bankroll_summary_views`、`test_audit_tables_are_append_only`（DB）、`test_actual_bet_settlement_flows_to_ledger_append_only`（DB：settle-v1 → ledger → 手動改結算 → 沖銷 + 新入帳；audit sum） |
| §44 concurrency | `test_two_simultaneous_submissions_cannot_both_use_same_capacity`（DB，兩執行緒 + barrier：一成功一 UniqueViolation、只有一筆）、`test_repeated_request_id_is_not_duplicated`（DB）；smoke：兩個並行 POST → 201 + 409、replay |
| §45 evidence | `test_evidence_no_historical_betting_and_bootstrap_threshold`（29 天 insufficient、30 天輸出 CI）、`test_evidence_state_does_not_alter_selection_or_stake`、`test_paper_and_actual_records_are_separate`、`test_paper_decision_attached_for_reference_only`、`test_international_odds_never_become_taiwan_actionable` |
| 狀態 / 台彩 vs 國際盤 | `test_missing_taiwan_odds_reasons`（4 組）、`test_taiwan_odds_stale_closed_window_started`、`test_stale_taiwan_quote_is_blocked_not_actionable`、`test_bankroll_unavailable_has_fractions_but_no_amounts`、`test_data_quality_flags_are_review_not_stake_changes`、`test_no_minimum_ev_threshold_and_ranking_is_display_only`、`test_fingerprint_changes_with_bets_bankroll_and_version_only` |
| 稽核 / migration | `test_legacy_bet_insert_still_works_and_gets_defaults`、`test_bet_execution_fields_immutable_and_never_deleted`、`test_correction_supersedes_without_overwriting_history`、`test_decision_job_materializes_freezes_day_start_and_is_idempotent`（DB） |
| policy / 無 TS 數學 | `test_decision_policy_binds_frozen_risk_v1_without_new_version`、`test_no_exposure_capacity_or_bankroll_math_in_typescript_or_frontend`（新掃描；D.3 掃描 `test_no_kelly_or_stake_math_in_typescript_or_frontend` 也仍通過） |

結果：

- `pytest tests/test_decision.py`：**47 passed**；`pytest tests/test_decision_db.py`：**10 passed**。
- 完整 `cd pipeline && pytest`：**682 passed**（950 秒）。
- `npm run build`：通過（`dist/_worker.js` 171.75 kB）。（repo 沒有安裝 TypeScript compiler，型別檢查只靠 vite / esbuild 轉譯，與先前各階段相同。）
- Node smoke（`npm run test:api`）：**160 / 160**，在兩次全新 `npm run db:reset:local` 上重跑結果相同。環境：`wrangler pages dev dist --local --env-file <只含 SESSION_SECRET> --binding DATABASE_URL=`，先確認 `/api/system/status` 的 `db_driver = d1`（不連 Supabase）。新增 54 項涵蓋：兩個新頁面、401 保護、決策中心契約（risk-v1 參數、Σ 實際下注 = Python day_stake、3% / 剩 5%、legacy 警示、qualified ≤ 上限、already_recorded、國際盤 diagnostic、evidence、system health）、今日未物化狀態、超出 risk-v1 → 409 + 需原因、重複機會 → 409、國際盤不可記錄、並行記錄 → 201 + 409、replay、記錄後 recalculation_pending、再記錄 → missing_context、legacy / provenance、他人資料 404、更正 supersedes、bankroll ledger（initial / 負數 / 無原因 / replay / reversal / 出金在物化前 409）、void 取代 delete、稽核事件。
- 瀏覽器檢查（本機 D1 seed，demo 帳號）：`/decision` 今日（未物化）與明日（8 qualified / 4 recorded / 實際 3% / 剩 5%）、記錄下注 modal、超額 stake → Exceeds risk-v1 確認框（未送出）、375 px 寬無水平捲動（修正了數字被截斷的問題）。

## 20. Production dry-run（唯讀；未寫入任何資料）

| 查詢（READ ONLY 交易） | 結果 |
|---|---|
| `schema_migrations` | 0001–0007（**0008 未套用**；D.5 表不存在） |
| 未來比賽 / odds_snapshots / ml-v2.0 預測 | 0 / 0 / 0 |
| market_pricing / bet_sizing / paper decisions / paper bets | 0 / 0 / 0 / 0 |
| bets / users | 0 / 2 |
| `data_sources` | 最新心跳停在 2026-10-02（排程器尚未部署）；`twsport` / `oddsapi` 為 2026-08-01 的 seed 遺留值 |
| `python run_decision.py --dry-run --summary` | `資料庫尚未套用 0008 … 不物化 decision board`（正確：不寫入、不猜） |

結論：production 目前正確狀態是 **waiting / no_schedule / no_odds**；沒有造任何資料。API 在 0008 未套用時：bets 端點維持 D.5 以前行為、`/api/decision-board` 回 `unavailable`（仍列出賽程）。

## 21. Deployment checklist

| 項目 | 狀態 | 說明 / 動作 |
|---|---|---|
| **Database** 0001–0007 | ready | 正式 DB 已套用 |
| Database 0008 | **manual action required** | `DATABASE_URL=… npm run db:migrate:pg`（純新增表 + bets 新欄位 + trigger；先備份） |
| Secret `DATABASE_URL` | ready | Pages secret 與 pipeline `.env` 已設（Session pooler 5432） |
| Secret `SESSION_SECRET` | manual action required | 確認 Pages secret 是正式隨機值（不是 dev fallback） |
| Secret `ODDS_API_KEY` | ready | 已設定（D.1）；免費 500 credits/月 |
| Scheduler 部署 | **manual action required** | `pipeline/scheduler.py` 尚未部署到常駐主機（Railway / Fly；Dockerfile 已有）；`data_sources` 最後心跳 2026-10-02 |
| Scheduler：schedule / injuries / predictions | waiting | 部署後由 12:00 賽程、傷病 15/30 分、12:20 early / 每 5 分 final 預測產生 |
| Scheduler：odds | blocked（台彩）/ ready（Odds API） | 台彩自動擷取被 Cloudflare 擋 → HAR 匯入（`run_odds.py --source twsport --from-file`）；Odds API 每日 4 次 |
| Scheduler：pricing / sizing | waiting | `market_pricing` 每 5 分（需要盤口 + ml-v2.0 預測） |
| Scheduler：paper strategy | waiting | `paper_strategy` 每 5 分（0007 已套用）；等第一場 2026-27 比賽 T-60 |
| Scheduler：decision materialization | waiting → 需 0008 | `decision_board` 每分鐘；0008 前回報 warn |
| Source：NBA data | waiting | nba_cdn → stats.nba.com → ESPN fallback；部署主機 IP 不能被 stats.nba.com 擋 |
| Source：Taiwan odds | **blocked** | 合規備援 = HAR 匯入；匯入後 60 分即 stale（必須在 T-60 前 60 分內） |
| Source：Odds API | ready | 只作國際盤 diagnostic |
| Artifacts：CURRENT / immutable version / dist-v2 | ready（本機） | `pipeline/artifacts/production/CURRENT.json` → `ml-v2.0+20261004T0339Z.20261004T033948Z`；部署排程器時必須一併帶上 `artifacts/production/`（或部署後 `python run_retrain.py`）；版本目錄不可變、sha256 驗證 |
| Health：heartbeat timestamps | waiting | 部署後 `/status` 與決策中心 System readiness 應在數分鐘內全部 healthy；`decision_board` > 2 分未更新 = stale |
| UI：auth | ready | 所有私人端點 401；**刪除 seed demo 帳號**（正式 DB 有 2 位使用者，確認是否含 demo） |
| UI：decision board | ready（程式）/ waiting（資料） | 0008 + 排程器後才會有物化結果 |
| UI：record bet | ready | 需先在 `/bankroll` 建立 initial_funding，否則每筆都需 missing_context 確認 |
| UI：bankroll | manual action required | 使用者在 `/bankroll` 設定初始資金（不連銀行） |
| Prospective：paper ledger | ready（已套用 0007） / waiting | 需要排程器部署 + 台彩快照 |
| Cloudflare Pages 部署 | manual action required | `npm run deploy:prod`（尚未部署）；**順序：0008 → 排程器 → API** |
| Backup / recovery | manual action required | 套 0008 前在 Supabase Dashboard 建立備份（或 `pg_dump`）。0008 純新增：回滾 = 部署舊版 API + 排程器（新表 / 欄位可保留不影響舊程式）；不要 DROP 有實際資料的稽核表（trigger 會擋 DELETE）。真正需要撤銷時：`DROP TABLE decision_opportunities, decision_snapshots, bankroll_day_snapshots, bankroll_ledger, risk_state_claims, bet_events, bankroll_accounts CASCADE` + `ALTER TABLE bets DROP COLUMN …` + `DROP TRIGGER trg_bets_guard`，並刪除 `schema_migrations` 的 0008 列——只在沒有任何真實下注紀錄時才做 |
| Supabase 閒置暫停 | manual action required | 免費專案閒置會暫停（README 踩坑 #4）；開季前確認 |
| 本機開發環境 | 注意 | repo 在 iCloud Desktop：關閉「最佳化儲存空間」或對資料夾「保持下載」，否則 `node_modules` / `.venv` / `.git` 會再被移出（§1.3） |

## 22. Known limitations

1. **證據**：沒有歷史投注證據；prospective paper 剛開始；所有 qualified 機會都只是理論值。
2. **台彩資料**：自動擷取仍被 Cloudflare 擋；只有 HAR 匯入的盤口會出現，60 分鐘後即 stale → 實務上多數比賽會是 `ingestion_unavailable` / `stale`。
3. **物化延遲**：記錄注單後最長約 1 分鐘無法顯示新額度（刻意：不在 Node 重算）。排程器停止 → 10 分鐘後全部額度失效（降級）。
4. **相關性**：仍只靠 risk-v1 hard cap；手動注單在其他 bookmaker 的線與台彩不同時仍以 game × market 視為同一市場（保守）。
5. **day-start 凍結時點**：明日的 day-start 可能在今日比賽尚未結算時就凍結（未結算 stake 排除在外，保守）；bankroll 在當日第一筆注單之後才建立 → 該日 `bankroll_unavailable`（不回填）。
6. **手動注單結算**：沒有 canonical 身分的注單（手動 / legacy）只能手動結算；平台注單由 settle-v1 自動結算（取消 / 延期 → ungradable，不猜 void）。
7. **legacy `/api/bets` 摘要**：仍在 TS 以 `stake × odds` 計算返還（D.3 起即豁免的帳務欄位）；權威的 User actual betting record 改由 Python 物化。
8. **seed**：D.5 seed 的 JSON id 以全新 DB 插入順序為準（`npm run db:reset:local`）。
9. **單一 bankroll**：每位使用者一個 strategy bankroll、單一幣別。

## 23. Final project readiness

- **程式面**：Phase D 完成。Prediction → Odds → No-vig / EV → Kelly sizing → T-60 paper decision → Settlement → 決策中心 / 記錄下注 / actual exposure / bankroll 全部串起來，所有數學單一來源（Python），frozen 設定（C.5 模型、dist-v2、proportional-v1、risk-v1、execution-v1）一個都沒改。
- **營運面**：尚未 ready——需要 (1) 套用 0008、(2) 部署排程器（含 artifact）、(3) 部署 Pages、(4) 設定 bankroll、(5) 解決台彩盤口取得（目前只能 HAR 匯入）。在那之前決策中心會如實顯示 waiting / no odds。
- **證據面**：歷史投注證據 = 無；2026-27 prospective paper 需 ≥ 30 個 graded betting day 才有 bootstrap CI，且即使有也只作描述、不改策略。平台用語維持 Qualified opportunity / Theoretical sizing，不宣稱有 edge。
- 本階段到此停止；未 commit、未套用 production migration。

## 附錄 A. 檔案

| 檔案 | 內容 |
|---|---|
| `migrations/postgres/0008_phase_d5.sql`、`migrations/d1/0008_phase_d5.sql`（新）、`package.json` | D.5 schema、trigger；本機 migrate 加入 0008 |
| `pipeline/core/decision/`（新） | `policy` / `exposure` / `bankroll` / `board` / `evidence` / `job` / `seed_fixture` |
| `pipeline/run_decision.py`（新）、`pipeline/core/scheduling.py` | CLI；`decision_board` 每分鐘 |
| `pipeline/tests/test_decision.py`、`test_decision_db.py`（新）、`conftest.py` | 測試 |
| `src/routes/decision.ts`、`src/lib/decision.ts`、`src/db/decision-queries.ts`（新）、`src/db/index.ts`、`src/index.tsx`、`src/routes/api.ts` | API（舊 bets handlers 移入新模組，0008 未套用時退回舊行為） |
| `src/pages/index.tsx`、`src/renderer.tsx`、`public/static/js/decision.js`、`bankroll.js`（新）、`bets.js`、`style.css` | UI |
| `seed/seed.template.sql`、`scripts/smoke-test.mjs` | D.5 demo seed（Python 產生）、smoke +54 |
| `README.md`、`docs/phase-d5-report.md` | 文件 |
