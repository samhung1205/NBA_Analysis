# Phase C.5D — Production Inference & Model Lifecycle 報告

日期：2026-10-03　範圍：把 C.5C 選定模型變成「每天對未開賽比賽產生預測」的 production pipeline（artifact、重訓、未開賽特徵、預測工作、排程、版本/回滾）。
**未進 Phase D、未做 odds / Kelly / ROI / 分差總分機率分佈、未重新選模型或調參、未動前端 / API / DB schema、未 commit、未寫入正式資料庫的 predictions。**

## 0. 結論摘要

| 項目 | 結果 |
|---|---|
| Production artifact | 已建立並上線（`CURRENT`）：`ml-v2.0+20261002T1736Z.20261002T173617Z`，cutoff 2026-10-02 17:36 UTC，6,605 場歷史 / 每個 target 6,575 個訓練樣本，重訓 26 秒 |
| 訓練-推論一致性 | production 路徑在歷史比賽上重建的模型輸入與訓練表**完全相同**（合成資料 early/final 兩個 profile、真實 2025-26 抽樣 13 場，容差 1e-9） |
| 未來資料防護 | 竄改預測時間戳之後的比分、box、球員分鐘、傷病報告、排定比賽列上的假比分 → 特徵 / input_hash 完全不變（有測試） |
| 真實賽程 smoke test | CDN 的 2026-27 開幕週 15 場（只讀、不寫 DB）全部產生預測，全部正確標示 `season_opener` |
| 測試 | 新增 50 項；完整 `pytest` **260 passed** |
| **可否開始 C.5E** | **可以**（§15） |

---

## 1. Training → inference gap audit（開工前現況）

| 項目 | C.5D 前的現況 | 問題 |
|---|---|---|
| Training entry point | `run_train.py` → `core/jobs/train.py`（Phase C ml-v1.0：XGBoost + 5 特徵邏輯迴歸）；C.5C 模型**只存在於** `core/jobs/c5c_evaluate.py` 的評測流程內 | C.5C 選定模型沒有任何訓練入口或存檔 |
| Feature builder | `temporal_features.build_temporal_features()`（pregame-v2）+ `c5c_evaluate.assemble()`（併 Phase C 特徵 + Elo） | 只接受**已結束**的比賽（每場算完特徵後立刻 `absorb()` 該場結果）；`assemble` 需要 `inp.phase_c`（`ml_features.load_games` 只撈 `status='final'`）與 `elo_ratings` |
| Artifact format | `artifacts/ml-v1.0.joblib`：裸 pickle（模型 + 特徵清單），無版本、無 metadata、無檢查；C.5C 只有 `c5c_inputs.pkl`（原始資料快取） | 不自足：收縮參數、補值常數、傷病校準狀態都沒存 |
| Model version | `settings.model_version`（環境變數，預設 elo-v1.0）/ `ML_VERSION` 常數 | 無 artifact 版本、無訓練 cutoff |
| predictions schema | 無唯一鍵；API 取每場 `created_at` 最新一筆；`train.py` 以「先 DELETE 再 INSERT」避免重複 | 直接 INSERT 的預測工作會每次重跑都新增 |
| Scheduler | 賽程 / 即時比分 / 傷病 4 個 job | 沒有預測、沒有重訓 |
| 未開賽資料 | `games` 只有每日同步視窗（ET 昨日 ~ 台灣明日）內的 `scheduled` 列；目前休賽期 0 場；`elo_ratings` 只在手動跑 backtest 時寫入 | — |

**不能直接套用到排定比賽的歷史特徵與原因**

| 特徵 / 欄位 | 原因 | C.5D 處理 |
|---|---|---|
| pregame-v2 全部 | builder 的流程是「features → absorb 該場結果」；排定比賽沒有結果 | 只 `absorb()` 預測時間戳前已結束的比賽，再對排定比賽呼叫 `features()`（它本來就只讀狀態） |
| Phase C 的 `margin10/form10/rest/b2b/total_est/h1_total_est/games_7d` | `ml_features.build_features` 是 pandas `shift(1).rolling()`，需要該場在表內；`load_games` 只撈 final | `PhaseCState`：逐隊已完賽序列，重現同樣語意（含 NaN 視窗、min_periods=3、rest clip 0~7、附加賽不在集合內） |
| `elo_diff` | `elo_ratings` 只有已結束比賽、且只在跑 backtest 時更新；**實測已過期**：C.5B 修正了 1 場比分（POR–GSW 2024-10-24，ESPN 104-140 → box 104-139）之後未重算，2024-10-26 起 2,571 場的 rating_before 與正確值差最多 0.26 Elo 點 | `models/elo_state.py` 以 backtest 相同規則重播（新賽季迴歸 25%、主場 +100、MOV 乘數）；訓練與推論都用重播值 |
| `y_{side}_{m}` | 訓練專用（收縮參數擬合用當場實際值） | 只在重訓時加（`features.side_targets`） |
| `elo_p` / `elo_p_calc` / `et_day` | 評測專用 | production 不使用 |
| 補值常數（`add_model_columns` 的 fills） | 未給 fills 時會用**當下輸入表**的平均 → 推論時會用推論批次的平均 | 存進 artifact；推論缺任何一個 → `FillsError` |
| 傷病校準器 | 訓練時 walk-forward 邊看結果邊學 | artifact 存凍結狀態；推論用 `frozen=True`（`observe()` 直接拋例外） |
| 名單延續性 / 交易 | `players.team_id` 是「現在」的球隊（用了會洩漏）；v2 只看本隊近 10 場的上場名單 → 交易走的球員仍被算進舊隊預期先發 | 任何特徵都不讀 `players.team_id`；見 §9 |

Production inference 不依賴：該場比分 / box score（排定比賽的列即使出現比分，只要不是 final 就不會進入歷史，有測試）、開賽後的傷病報告（報告必須嚴格早於預測時間戳）、評測專用欄位、未來名單（只用預測時間戳前的出賽紀錄）。

## 2. Artifact format（`core/production/artifact.py`）

```
pipeline/artifacts/production/            （gitignore；MODEL_ARTIFACT_DIR 可改）
  versions/<artifact_version>/bundle.joblib   全部推論需要的東西
  versions/<artifact_version>/manifest.json   metadata + sha256 + gates（上線前檢查結果）
  CURRENT.json                               目前上線版本（os.replace 原子替換）
  history.jsonl                              promote / rollback 紀錄（只追加）
  .lock                                      重訓 / promote / rollback 互斥（fcntl）
```

`bundle.joblib` 內容（兩個 profile：`early` = ET 賽事日 00:00 傷病 cutoff、`final` = T-60，各自完整一份）：

| 內容 | 欄位 |
|---|---|
| 五個模型 | `profiles.<p>.models.<target>.estimator`（sklearn Pipeline：StandardScaler + LogisticRegression / Ridge — **scaler 狀態在 pipeline 內**） |
| 特徵清單與順序 | `models.<target>.features`（推論只用這份；不重新選特徵） |
| 收縮 / 先驗參數 | `blend_params`（每個 metric × mode 的 k、a、b、e、c̄、L fallback） |
| 補值常數 | `fills`（訓練集算出） |
| 傷病校準狀態 | `calibrator`（狀態 / 狀態×時距計數、先驗常數、收縮強度；版本不同 → 拒絕） |
| 版本 | `schema_version=1`、`model_version=ml-v2.0`、`artifact_version=ml-v2.0+<cutoff>.<trained_at>`、`feature_version=pregame-v2.1` |
| 訓練 metadata | `training_cutoff_utc`、`training_seasons`、`trained_at_utc`、`spec`（C.5C 選定設定）、各 target 樣本數 / 訓練賽季 / in-sample 指標、最後一場比賽時間、資料指紋、傷病報告數、校準觀測數、函式庫版本 |

**不相容一律明確失敗**（`IncompatibleArtifactError`，有測試）：schema 版本不支援、特徵版本 ≠ 程式、model_version 不同、scikit-learn minor 版本不同（**在 unpickle 之前**由 manifest 擋下）、bundle 雜湊不符、缺 profile / 模型 / 特徵清單、模型輸入維度 ≠ 特徵清單、校準常數不同。沒有 `CURRENT` → `NoProductionArtifact`（訊息指向 `run_retrain.py`）。

## 3. Retraining strategy（`core/production/retrain.py`、`python run_retrain.py`）

1. 取得 exclusive lock（另一個重訓進行中 → `LockBusy`，排程略過）；清掉中斷留下的 `.tmp-*`。
2. 資料：`status='final'` 且**開賽早於 cutoff − 4 小時**的比賽（確定在 cutoff 前已結束），與 cutoff 前的傷病報告；`train_bundle` 對未截斷的輸入直接拒絕。
3. 模型規格固定為 C.5C §14（勝負 logistic E C=0.01；分差 Ridge E α=1000；總分 Ridge E_count α=1；上半場分差 Ridge E_count α=1000；上半場總分 Ridge E_count α=1；訓練起始 2021-22）。**不重新選模型、不調參、不跑 XGBoost**。收縮/先驗參數、補值常數、傷病校準屬於模型擬合，每次用全部訓練資料重擬合（與 C.5C 每個 fold 相同）。
4. Gates：所有預測有限值；勝負 in-sample log loss < ln 2；各回歸 MAE < 常數模型。
5. `save_version`：寫到 `versions/.tmp-<uuid>/` → fsync → **從暫存目錄重新載入並驗證「載入後預測與記憶體內模型逐位元相同」** → rename 成正式目錄。
6. promote：`CURRENT.json` 原子替換 + `history.jsonl` 追加。

失敗保護（有測試）：資料載入例外、寫完 bundle 後失敗、gates 未過、round-trip 不一致 → `CURRENT` 不變、沒有半成品目錄。決定性：同一 cutoff 重訓兩次，預測 / 補值 / 校準狀態完全相同。不會在每次預測時重訓。

輸出（本次實跑）：

```
model_version=ml-v2.0
artifact_version=ml-v2.0+20261002T1736Z.20261002T173617Z
training_cutoff_utc=2026-10-02T17:36:02Z
n_games_history=6605
n_train={"win": 6575, "margin": 6575, "total": 6575, "h1_margin": 6575, "h1_total": 6575}
```

In-sample（final profile）：勝負 log loss 0.6054 / Brier 0.2093；MAE 分差 10.77、總分 14.69、上半場分差 8.81、上半場總分 9.73（常數模型 12.30 / 16.11 / 9.38 / 10.29）。這只是 gate，不是表現估計——預期表現以 C.5C walk-forward 評測為準。
傷病校準（全部歷史）：final profile P(absent) Out 1.00 / Doubtful 0.98 / Questionable 0.42 / Probable 0.05 / Not Listed 0.07；early profile Doubtful 0.93 / Probable 0.09（早報告較不可靠，與 C.5C 一致）。

## 4. Upcoming-game feature strategy（`core/production/features.py`）

```
prepare_context(history < as_of)          只 absorb 已結束比賽（v2 builder、PhaseCState、EloState），不算歷史特徵、不學校準
upcoming_rows(ctx, games, as_of, frozen calibrator, pending)
    v2:      builder.features(排定比賽（無比分）, cutoff=as_of, schedule_prior=…)
    Phase C: PhaseCState.team(tid, tip, prior)
    Elo:     EloState.pregame()（新賽季第一場回傳迴歸後的值，不改狀態）
model_frame → finalize（artifact 的收縮參數 + 補值常數）→ 模型輸入（artifact 的特徵順序，NaN → 0 與訓練相同）
```

- **不是「塞假結果再跑歷史 builder」**：排定比賽以無比分的 `GameRecord` 呼叫 `features()`；`features()` 本來就只讀狀態。
- 傷病 cutoff = 預測時間戳（報告嚴格早於它）；profile 依距開賽時間選：≥ 3 小時 → early、< 3 小時 → final（3 小時 = 校準器時距分桶界線）。
- **已排定但尚未完賽的前一場**（背靠背第一場還沒打）：賽程賽前已知 → 休息 / 背靠背 / 7 日場數照算（v2 含附加賽、Phase C 不含，與各自訓練集合一致）；結果未知 → 近況停在最後一場已完賽，旗標 `pending_prior_game`。
- **Training-serving parity**（`tests/test_production.py`）：
  - 合成聯盟：對歷史比賽 G，以 production 路徑（G 不給比分、只用 G 決策時點之前的資料、校準器 = 訓練時 G 當下的 walk-forward 狀態）重建 → 5 個 target 全部模型輸入與訓練表一致（final：T-60；early：ET 00:00）。
  - 真實五季資料：2025-26 隨機 12 場 + 開季第一場，所有 production 特徵（41 欄）一致（以固定傷病常數排除校準器時點差異）。
  - `PhaseCState` vs `ml_features.build_features`：所有 Phase C 欄位逐場一致；`EloState` vs backtest 規則逐場一致。
- **pregame-v2.1**：與 v2 唯一差別是交易感知名單對應（§9）。C.5C 評估改以 `trade_aware=False` 呼叫以保持可重現。凍結設定下的對照（不做任何選擇）：

  | 評測 2024-25 + 2025-26 | 勝負 LL | 分差 | 總分 | 上半場分差 | 上半場總分 |
  |---|---|---|---|---|---|
  | v2（C.5C） | 0.5922 | 10.978 | 14.925 | 8.866 | 9.637 |
  | v2.1（C.5D） | 0.5921 | 10.977 | 14.925 | 8.866 | 9.637 |

  有離隊球員被排除的比賽 953 / 6,575 列；其他列只有校準器母體的微小變化。

## 5. Season opener / early-season fallback

| 情境 | 特徵處理（與訓練時相同的規則，已在 C.5C 季初分桶驗證） | features_json / 旗標 |
|---|---|---|
| 本季第一場（gp=0） | 本季窗口 NaN → 用收縮估計；收縮估計 = L + ρ(c̄)(P − L)，**c̄ = 訓練集季初延續性平均（本次 0.76）**，不是 0；名單延續性 / 名單先驗 = NaN（不是 0），差值特徵 0（中性，不偏向任一方），和類特徵用訓練集平均；`cont_known=0`、`early_season=1` | `season_opener`、`low_sample`、`early_season`、`continuity_unknown:<side>`、`continuity_known=false`；信心度 × 0.6 |
| 第 1–5 場 | w = n/(n+k) 隨場次增加；延續性從第 2 場起由實際上場分鐘計算 | `low_sample`、`early_season`；× 0.75 |
| 第 6–9 場 | 同上 | `early_season`；× 0.9 |
| 上季沒有資料（新球隊） | prior = 聯盟平均 | `no_prev_season:<side>` |
| 上季回歸分鐘未知 | 不補造：延續性只在本季已上場後計算 | 同上 |

開季第一場的延續性仍是「未知」（沒有可信的開季名單歷史）。用 CDN / stats roster 在開季當下補值**無法用歷史回測驗證**，C.5D 不做，列為風險（§14）。

## 6. Production prediction job（`core/production/predict.py`、`python run_predict.py`）

`predict_upcoming_games_job(kind, now, …)`：找候選 → 預測時間戳 = now → 載入歷史（開賽 < now 的 final 比賽、近 5 天傷病快照）→ 建特徵 → 載入 CURRENT artifact → 預測 `home_win_prob`、`pred_margin`、`pred_total`、`pred_home_h1/away_h1`（= (h1 總分 ± h1 分差)/2）、`pred_home_h2/away_h2`（= 全場得分 − 上半場；全場得分 = (總分 ± 分差)/2）→ 信心度 → features_json → 寫入。

**predictions schema 不改（最少破壞）**：

- 同一場比賽的寫入以 `pg_advisory_xact_lock(58405, game_id)` 序列化。
- 與該場（`model_version=ml-v2.0`）最新一筆比較 `input_hash` = sha256（artifact 版本、profile、賽事階段、5 個模型的輸入向量、資料品質旗標）：
  - 相同 → 不寫（重跑不會新增；`unchanged`）。唯一例外：第一次 `final`（最新一筆不是 final）寫一筆，之後不再寫。
  - 不同 → 新增一筆；`features_json.supersedes` 指向上一筆、帶 `prediction_kind` / `prediction_as_of_utc` / `input_hash` / `artifact_version`，歷史版本全部保留可追蹤。
  - `refresh` 另外要求實質變化（|Δ勝率| ≥ 0.01、|Δ分差|/上半場 ≥ 0.5、|Δ總分| ≥ 1.0、或資料品質旗標改變；換 artifact 一律記錄）。
- 開賽前 5 分鐘內 / 已開賽：不產生預測。
- 心跳 `data_sources.model_predict`（ok / warn：有略過 / error：artifact 無法載入）。

**features_json**（前端既有 `contributions: [{label, value}]` 不變，其餘為新增 key，前端以原始特徵表顯示）：
`model_version`、`artifact_version`、`feature_version`、`training_cutoff_utc`、`prediction_kind`、`prediction_as_of_utc`、`profile`、`input_hash`、`supersedes`、`contributions`（log-odds）、`win_logit_intercept`、`target_contributions`、`elo_home/away`、`rest_days_home/away`、`form_home/away_last10`、`games_played_home/away`、`early_season`、`low_sample`、`continuity_known`、`season_stage`、`injuries.{home,away}`（known、報告距開賽、Out/Doubtful/Questionable 人數、角色分鐘損失、預期先發可出賽）、`data_quality.{flags, details, confidence_factor, prediction_allowed}`、`confidence_raw`。

## 7. Prediction timing（台灣時間）

| 時點 | 實作 | 依據 |
|---|---|---|
| A. Early | `predict_early` 每日 12:20（12:00 賽程同步後；≈ ET 00:20 / 冬令 23:20）：未來 36 小時內比賽；profile early | C.5C：前一晚報告已取得約 60% 傷病收益 |
| B. Final pregame | `predict_final` 每 5 分鐘：開賽前 5~75 分鐘、目前 artifact 尚未有 final 版本者 → 開賽前 60~75 分鐘重算一次；profile final | C.5C：T-60 有實際幫助（主要在分差） |
| C. Injury refresh | `predict_injury_refresh` 每 15 分鐘（:07/:22/:37/:52，傷病抓取之後）：開賽 75 分鐘 ~ 36 小時內、最後一次預測之後出現新傷病報告的比賽 → 重算，實質變化才寫 | 以可測試的 periodic check 實作 event trigger |
| 不做 | 15 分鐘級的開賽前輪詢 | C.5C：T-60 → T-15 無可測得差異 |

## 8. Missing-data / source degradation policy

| 狀況 | fallback | 旗標 | 允許預測 | 信心度係數 |
|---|---|---|---|---|
| stats.nba.com 不可用 | 不需要：production 特徵只用 CDN box score 衍生指標 | — | 是 | — |
| NBA CDN 部分失敗（近期比賽缺 box / 衍生指標） | 只用現有比賽（缺的不進平均，不補造） | `missing_box_recent:<side>`（近 10 場缺 ≥ 3 場） | 是 | 0.85 |
| 應已結束但結果未同步 | 近況停在最後一場已完賽；賽程照算 | `stale_results:<side>`（+ `pending_prior_game`） | 是 | 0.8 |
| 前一場已排定未完賽（背靠背） | 同上 | `pending_prior_game:<side>` | 是 | 0.9 |
| 傷病報告缺（沒有涵蓋該隊的已申報報告） | `inj_known=0`，傷病差值中性、和類用訓練集平均（**不當成全員健康，也不是 0 人缺陣**：原始欄位為 NaN） | `injury_unknown:<side>` | 是 | 0.8 |
| 傷病來源 24 小時無新報告 | 同上 | `injury_feed_stale` | 是 | 0.9 |
| 新球員（無出賽紀錄）被列入傷病 | 不計入分鐘損失（沒有可用的使用量） | `injury_player_no_history:<side>` | 是 | — |
| 傷病名單姓名對不到球員 | 略過該列（原文保留在快照） | `injury_unmatched_players:<side>` | 是 | — |
| 交易 / 離隊球員 | §9 | `roster_departed:<side>` | 是 | — |
| 開季 / 樣本少 / 延續性未知 | §5 | §5 | 是 | §5 |
| 附加賽（不在訓練集合） | 照算，`is_playoffs=0` | `stage_out_of_training` | 是 | 0.9 |
| 沒有任何歷史、缺球隊、距開賽 < 5 分鐘 / 已開賽 | — | 寫入 `skipped` | **否** | — |
| artifact 不存在 / 不相容 | — | 心跳 error | **否**（整個 job 失敗，不寫任何預測） | — |

同組旗標（開季 ⊂ 樣本 < 5 ⊂ 樣本 < 10；傷病未知 / 來源過期；結果未同步 / 前一場未完賽）只取最嚴重者，組間相乘，下限 0.25。係數是**啟發式**（不改變預測值，只影響顯示用 `confidence`；`confidence_raw = |p − 0.5| × 2` 另存）。任何缺值都不用未來資料補。

## 9. Player trades / team changes（pregame-v2.1）

- **重要性跟著球員走**：`role` = 球員自己最近 10 次出賽的分鐘（不論替哪一隊）。
- **可用性屬於新隊**：傷病報告列在哪一隊，就計入哪一隊的損失。
- **舊隊不再把他算入目前名單**：球員最近一次出賽是替別隊（預測時間戳前的 box score 事實）→ 從舊隊的預期先發、近期輪替前 8 名、前 3 名、Not Listed 校準母體排除；`roster_departed_n` 記錄人數。
- 本季已打分鐘類（`ret_min_pct` 等）是歷史事實，不排除。
- `players.team_id`（現況）不被任何特徵讀取。
- 測試：X 在 A 打 26 場後改替 B 出賽 → A 的預期先發不含 X、A 可出賽先發 5/5、`roster_departed_n=1`；B 把 X 列 Out → B 的角色分鐘損失 = X 過去的上場分鐘；交易前 A 列 X Out → 計入 A。舊定義（v2）會把 X 繼續當 A 的預期先發（對照斷言）。
- 限制：已交易但**尚未替新隊出賽**的球員，在舊隊仍被視為名單內（沒有賽前可驗證的異動資料來源）；見 §14。

## 10. Explanation / feature contributions

- 勝負（邏輯迴歸）：`contributions` = 標準化係數 × 標準化特徵值（log-odds；正 = 有利主隊；前 8 名），`win_logit_intercept` + Σ = 預測 logit（測試驗證精確相等）。
- 分差 / 總分 / 上半場（Ridge）：`target_contributions.<target>` = {unit、method、intercept、prediction、top}，同樣是精確線性分解，**明確標示「非 SHAP」**。
- 前端 contract 不變：`contributions` 仍是 `[{label, value}]`（多一個 `feature` key）；新增 key 由前端既有的「原始特徵值」表格顯示。

## 11. Scheduler integration（`core/scheduling.py`）

| job | 觸發（Asia/Taipei） | 啟動補跑 | CLI |
|---|---|---|---|
| daily_schedule_scores | 每日 12:00 | 是 | （既有） |
| predict_early | 每日 12:20 | 是 | `python run_predict.py --kind early` |
| predict_final | 每 5 分鐘 | 是 | `python run_predict.py --kind final` |
| predict_injury_refresh | :07/:22/:37/:52 | 否 | `python run_predict.py --kind refresh` |
| weekly_retrain | 週一 16:00（美東凌晨無比賽） | 否 | `python run_retrain.py` |

全部：`guarded`（例外只記 log）、`max_instances=1`、心跳（`model_predict` / `model_retrain`）、可 `--dry-run`、`python scheduler.py --print-next-runs` 可看下次觸發。重訓與推論互不破壞：版本目錄不可變、`CURRENT` 原子替換，推論每次載入一個完整版本；重訓之間以檔案鎖互斥。

## 12. Model versioning and rollback

- 每筆預測 features_json 帶 `model_version`、`artifact_version`、`feature_version`、`training_cutoff_utc`。
- 版本目錄**永不自動刪除**；`python run_retrain.py --list` 列出版本與 CURRENT。
- `python run_retrain.py --rollback` → 退回上一個曾上線的 known-good 版本；`--rollback --to <v>` / `--to <v>` 指定版本。只接受通過 gates 的版本。
- 換 artifact 後，下一次 refresh / final 會寫入新的一筆（artifact_version 不同），舊預測保留。

## 13. Production smoke test & tests

**Deterministic fixture（`tests/test_production_db.py`，暫存 schema）**：合成三季歷史寫入 DB → 加一場 `scheduled` 比賽 → `retrain()`（由 DB 載入）→ `predict_upcoming_games_job("early")` → `predictions` 一筆 → 以 **API 相同 SQL**（`getLatestPredictionForGame` / `getLatestPredictions`）讀回，欄位完整、半場/全場一致、`contributions` 符合前端 contract → 重跑不新增 → T-60 寫入 final（supersedes 早期版本）→ 再跑不新增 → 開賽後不寫。排定比賽列上的假比分、之後已 final 的比賽、之後的傷病報告都不改變預測。

**額外真實賽程 smoke test（不寫 DB、不污染評測）**：CDN 2026-27 開幕週 15 場 + 正式 DB 歷史 + 本次 artifact → 15/15 產生預測（例：PHI@NYK P(主勝)=0.714、分差 +7.0、總分 225.9；BOS@DET 0.545），全部標示 `season_opener / low_sample / early_season / continuity_unknown / injury_unknown / injury_feed_stale`（休賽期沒有傷病報告）。`python run_predict.py --kind early --dry-run` 對正式 DB：0 候選（DB 尚無 2026-27 賽程，預期）。

**新增測試（50 項）**

| 要求 | 測試 |
|---|---|
| artifact save/load parity | `test_artifact_save_load_parity`（預測逐位元、特徵順序、補值、收縮、校準機率） |
| incompatible version | `test_incompatible_bundle_fails_clearly`（8 種）、`test_incompatible_files_fail_on_load`（sklearn 版本、雜湊）、`test_missing_artifact_fails_clearly` |
| historical vs production parity | `test_training_serving_parity_on_historical_games[final/early]`、`test_training_serving_parity_on_real_history`、`test_phase_c_state_matches_pandas_build_features`、`test_elo_replay_matches_backtest_rules`、`test_schedule_features_count_pending_prior_game` |
| future-game leakage guard | `test_future_leakage_guard`、`test_fake_result_on_scheduled_game_is_never_used`、`test_predict_games_skips_started_or_imminent_games`、`test_inference_cannot_recompute_fills`、`test_frozen_calibrator_cannot_learn` |
| first-game / early-season | `test_season_opener_and_early_season_fallback`、`test_games_one_to_five_are_low_sample` |
| traded player | `test_traded_player_maps_to_new_team`、`test_traded_player_on_old_team_report_before_trade` |
| missing injury | `test_missing_injury_report_fallback`、`test_new_player_without_history_is_flagged` |
| partial source failure | `test_partial_source_failure_flags_and_no_fabrication`、`test_playin_game_is_predicted_but_flagged` |
| idempotency | `test_prediction_idempotency_under_reruns`、e2e 重跑段落 |
| refresh after injury change | `test_refresh_after_injury_change`、`test_injury_report_changes_prediction` |
| retrain atomicity | `test_retrain_failure_keeps_current_and_leaves_no_partial_version`、`test_retrain_gate_failure_does_not_write`、`test_roundtrip_mismatch_aborts_before_rename`、`test_concurrent_retrain_is_rejected`、`test_interrupted_tmp_dir_is_ignored_and_cleaned`、`test_version_dirs_are_immutable`、`test_retrain_only_uses_games_before_cutoff_buffer`、`test_retrain_is_deterministic` |
| rollback | `test_rollback_and_previous_versions_are_kept`、`test_promote_rejects_not_known_good`、`test_artifact_switch_produces_new_tracked_version` |
| scheduler timing | `test_prediction_and_retrain_jobs_timing`、`test_final_window_covers_t_minus_60_with_5_minute_polling` |
| end-to-end | `test_end_to_end_scheduled_game_inference` |
| 其他 | `test_contributions_are_exact_linear_decomposition`、`test_profile_selection_by_lead` |

**完整 `cd pipeline && pytest`：260 passed（既有 210 + 新增 50；504 秒，含暫存 schema DB 測試）。** 最後一次修改 `predict.py`（心跳）後重跑 production / scheduler 測試：62 passed。

Node smoke tests **未重跑**：沒有修改 API（`src/`）、前端（`public/`）或 DB schema / migrations；新增的只是 `features_json` 內的 key（前端以既有的通用 key-value 表格顯示）。

## 14. Operational risks

1. **還沒有對正式 DB 寫入任何 ml-v2.0 預測**：DB 目前沒有 2026-27 賽程（每日同步只抓 ET 昨日 ~ 台灣明日）。開季前一天（台灣 10/20 12:00 起）排程會開始產生；第一週請看 `/status` 的 `model_predict` 心跳與 `features_json.data_quality`。排程器需部署到可連 CDN 的主機並保留 `pipeline/artifacts/production/`（Docker 映像需掛 volume，否則重啟會遺失 artifact → 啟動後先跑 `python run_retrain.py`）。
2. **開季第一場延續性未知**：用 c̄（訓練集季初平均）與中性差值處理，信心度 × 0.6；季初模型層改善未被證實（C.5C §11），請以低信心看待開季前兩週的預測。
3. **開季總分依賴上季末段**：Phase C 的 `total_est` 跨季（上季最後 10 場），擺爛球隊季末高總分會帶到開季（真實 smoke test 中 UTA@MEM 預測總分 250.8）；屬 C.5C 已驗證的模型行為，未修改。
4. **已交易但尚未替新隊出賽**的球員仍被舊隊視為名單內；沒有賽前可驗證的異動來源。
5. **elo_ratings 表已過期**（§1）：production 不再讀它；若其他地方（Phase C ml-v1.0、backtest 報表）仍要用，需重跑 `run_backtest.py`。
6. 信心度係數是啟發式、未校準；`confidence_raw` 保留原始值。上線後應每週監控 log loss / Brier / calibration slope（C.5C 兩季 slope 0.88 / 1.08）與傷病校準表漂移。
7. 推論每次從 DB 載入全部歷史（~6,600 場、17 萬筆球員分鐘，約數秒）；final job 只有在有候選比賽時才載入。若 DB 變慢可加狀態快取。
8. predictions 表沒有唯一鍵：冪等靠 advisory lock + input_hash（同一 Postgres 內正確）；若有人用舊流程（`train.py` 的 DELETE + INSERT）寫入 `ml-v1.0`，不影響 `ml-v2.0`，但 API 只看 created_at 最新者——**不要再對未來比賽跑 ml-v1.0**。
9. 檔案鎖是單機鎖；多台主機同時跑重訓需改用共享鎖（目前設計只有一個排程器行程）。
10. scikit-learn 升級會讓 artifact 被拒絕（設計如此），升級後需先重訓。

## 15. 是否可以開始 C.5E（probability distribution）

**可以。** C.5E 需要的基礎都已就位：
- 分差 / 總分 / 上半場的點預測已在 production 路徑產生，且有固定的 artifact 版本與訓練 cutoff，可在其上加殘差分佈（例如依季初/傷病未知/總分水準分層的 walk-forward 殘差尺度）。
- `features_json.data_quality` 已有可用於分層不確定度的旗標（`season_opener`、`low_sample`、`injury_unknown`…）。
- artifact 格式可直接擴充（新增 `profiles.<p>.distributions`，`schema_version` 升級時舊版會被明確拒絕）。

C.5E 開工前建議：用 C.5C 的 walk-forward 評測預測（`c5c_eval_predictions.csv.gz`）擬合殘差分佈並在 2024-25 / 2025-26 檢查 coverage，**不要**用本次 in-sample 殘差。

## 附錄 A. 檔案

| 檔案 | 內容 |
|---|---|
| `pipeline/core/production/spec.py`（新） | C.5C 選定設定、版本常數、profile、特徵標籤 |
| `pipeline/core/production/features.py`（新） | 未開賽特徵（prepare_context / upcoming_rows）、PhaseCState、模型輸入表組裝（訓練推論共用） |
| `pipeline/core/production/artifact.py`（新） | 版本化 artifact、原子寫入、驗證、promote / rollback、鎖 |
| `pipeline/core/production/retrain.py`（新） | 重訓、gates、round-trip 驗證、CLI、排程入口 |
| `pipeline/core/production/inference.py`（新） | 套用 artifact、精確線性貢獻、資料品質 / 信心度、features_json |
| `pipeline/core/production/predict.py`（新） | 候選選擇、冪等寫入、預測 job、CLI |
| `pipeline/core/production/inputs.py`（新） | DB 載入（歷史 / 未開賽 / pending） |
| `pipeline/core/models/elo_state.py`（新） | Elo 重播 |
| `pipeline/core/models/temporal_features.py` | pregame-v2.1：校準器序列化 / 凍結、per-call cutoff、排定前一場、交易感知名單（`trade_aware`） |
| `pipeline/core/jobs/c5c_evaluate.py` | 以 `trade_aware=False` 呼叫，保持 C.5C 可重現（唯一改動） |
| `pipeline/core/scheduling.py`、`scheduler.py` | 4 個新 job |
| `pipeline/run_retrain.py`、`pipeline/run_predict.py`（新） | CLI |
| `pipeline/tests/test_production.py`、`test_production_db.py`、`prod_fixtures.py`（新）、`test_scheduler.py` | 50 項新測試 |
