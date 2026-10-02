# Phase C.5B — Historical Data Completeness & Feature Foundation 報告

日期：2026-10-02　範圍：歷史資料層（box score / 衍生進階指標 / 傷病快照 / 賽前特徵底座）。
**未重新調模型、未動 Phase D、未動前端與 API contract**（僅有一處必要 bug fix：`injuries` 的「球員消失」語意，見 §2）。

## 1. 結論

| 項目 | 結果 |
|---|---|
| 五季 box score（2021-22 ~ 2025-26，例行賽+附加賽+季後賽） | **6,604 / 6,605 場**（99.98%）；缺的 1 場是 CDN 資料問題（§3） |
| 衍生進階指標（pace / ORtg / DRtg / eFG / TS / TOV% / ORB% …） | 6,604 場、13,208 列，**完全由基本 box score 計算，不依賴 stats.nba.com** |
| 歷史傷病 | 9,382 份官方報告、900,066 列快照；賽前被報告涵蓋的場次 **99.85~100%** |
| 賽前特徵底座 | 6,605 場 × 108 個特徵欄，洩漏防護通過（§6） |
| Python 測試 | **173 passed**（既有 84 + 新增 89），見 §7 |
| **可進入 Phase C.5C** | **可以**（附帶的限制見 §8） |

## 2. 傷病語意修正（remaining injury semantics）

**問題**：舊 `injuries` 只存「狀態變化」，球員從後續報告消失後，舊的 Out/Doubtful 永遠是他的最新狀態；補抓較舊報告（亂序）也會讓事件序列錯亂。

**設計**
- 新增**完整快照**：`injury_reports`（每份報告一列，含涵蓋範圍 `coverage`）＋ `injury_report_entries`（報告每一列）。as-of 還原只讀快照，不受寫入順序影響。
- 狀態語意（`core/injury_history.py`、`core/injury_asof.py`）：

  | 狀態 | 意義 |
  |---|---|
  | Out / Doubtful / Questionable / Probable / Available | 官方列出的狀態 |
  | **Not Listed** | 該隊該日比賽已被一份**已申報**的報告涵蓋，球員不在名單上 → 視同可出賽（衍生） |
  | **Unknown** | 賽前沒有任何報告涵蓋該隊該場 → 無資訊，**不等於健康**（`inj_known=0`，傷病特徵為 NaN） |
  | NOT YET SUBMITTED | 該隊尚未申報：這份報告對該隊沒有資訊，查詢時往前找較早的報告，不會清掉舊狀態 |

- **as-of 規則**：只看 `report_time_utc` **嚴格早於** cutoff（= 開賽 − `decision_offset_min`）的報告；由新到舊找涵蓋「該賽事日（ET）× 該隊」的報告，遇到 NOT YET SUBMITTED 繼續往前，找到已申報者即停。狀態以（賽事日, 球隊, 球員）為單位，今天的 Out 不會延續到明天的比賽。往前最多 4 天。
- **`injuries`（API 讀取的最新狀態）修正**：每隊只取報告中**最早的賽事日**（避免同一球員因多賽事日互相覆寫）；球員在**已申報**的隊伍中不再出現 → 補一列 `Available`（reason `Not listed on latest official report`，前端既有的「可出賽」樣式，不需改前端）；NOT YET SUBMITTED / 隊伍不在報告內 → 不動；已有**更新**的報告寫入過 → 補抓的舊報告只進快照、不動 `injuries`（older snapshot 不蓋過 newer state）。同時補上 `game_id`（球隊 + ET 日期對應賽事），讓單場詳情的傷病查得到。
- **唯一性 / 併發保護**（`migrations/postgres/0003_phase_c5b.sql`）
  - `injury_reports UNIQUE(source, report_time_utc)`：一份報告一個交易寫入（報告列 + 全部 entries），併發時輸家 `ON CONFLICT DO NOTHING` 不回傳 id → 跳過；中途失敗整份回滾（有測試）。
  - `injuries UNIQUE(source, report_time_utc, player_id)` + `ON CONFLICT DO NOTHING`：取代 C.5A 的 SELECT-then-INSERT，兩個行程同寫不會雙寫（有並行測試）。既有重複資料以 id 最小者保留。
  - 評估後**不**對 entries 加更多約束：`UNIQUE(report_id, game_date, team_id, player_name)` 已足夠；多餘索引（`report_id`、`player_id`）每個約 10 MB，已移除。
- **實證檢驗**（`python -m core.jobs.injury_validation`，離線診斷，**不回饋特徵**；以開賽前最後一份報告）：

  | 賽前官方狀態 | n | 實際未上場率 |
  |---|---|---|
  | Out | 53,499 | 99.98% |
  | Doubtful | 46 | 97.8% |
  | Questionable | 394 | 40.6% |
  | Probable | 129 | 8.5% |
  | Available | 12,114 | 14.3%（含 G League 雙向合約等「列為可出賽但未上場」的板凳） |

  「球員不在名單上 = 可出賽」的檢驗：以「過去 5 場先發 ≥2 次」的**常規先發**為對象，Not Listed 的實際缺陣率 **2.5%**（n=59,325）、整隊無人列入（implied-empty）**3.7%**（n=1,104）、Out 100%。推定成立（缺陣率遠低於 Questionable 的 32%）。
  `P_ABSENT` 先驗（Out 1.0 / Doubtful 0.75 / Questionable 0.5 / Probable 0.15）固定不變；實測 Doubtful 偏高（0.98）、Probable 偏低（0.085），C.5C 若要用請以 walk-forward 方式校準，不要用全樣本。

## 3. Historical coverage audit（`python -m core.jobs.coverage_audit`）

分母 = 已結束的例行賽 / 附加賽 / 季後賽。`--missing <項目>` 列出缺漏場次，`--json` 輸出含缺漏日期與場次清單（本次輸出：`pipeline/logs/audit_after.json`，不入版控）。

**Before（C.5B 開始前，DB 實況）**

| 項目 | 狀態 |
|---|---|
| games | 6,605 場 final，**全部是 ESPN 暫存 ID（`espn:<id>`，0 場有官方 gameId）** |
| 逐節 / 上半場 | 100% / 100%（ESPN） |
| team_game_stats / player_game_stats / 衍生指標 | **0 / 0 / 0** |
| 傷病 | 1 列（seed 假資料）、0 份快照 |

**After**

| 項目 | 2021-22 | 2022-23 | 2023-24 | 2024-25 | 2025-26 |
|---|---|---|---|---|---|
| games_final | 1,323 | 1,320 | 1,319 | 1,321 | 1,322 |
| 官方 gameId | 100% | 100% | 99.92% (−1) | 100% | 100% |
| 總分 / 逐節 / H1·H2 | 100% | 100% | 100% | 100% | 100% |
| 逐節加總 = 總分 | 100% | 100% | 100% | 100% | 100% |
| 球隊 box（含原始計數） | 100% | 100% | 99.92% (−1) | 100% | 100% |
| 球隊得分 = 賽事比分 | 100% | 100% | 99.92% | 100% | 100% |
| 球員統計 | 100% | 100% | 99.92% | 100% | 100% |
| 先發恰 5 人（雙方） | 100% | 100% | 99.92% | 100% | 100% |
| 球員分鐘加總 = 球隊分鐘（±1） | 100% | 100% | 99.92% | 100% | 100% |
| 衍生進階指標 | 100% | 100% | 99.92% | 100% | 100% |
| 賽前有報告涵蓋**雙方**球隊 | 100% | 99.85% (−2) | 99.92% (−1) | 99.92% (−1) | 99.85% (−2) |
| 賽前有報告涵蓋至少一方 | 100% | 99.92% (−1) | 99.92% (−1) | 99.92% (−1) | 99.85% (−2) |

賽前最後一份報告距開賽：中位數 60 分鐘（2025-12-22 起 15 分鐘制為 15 分鐘）。

**缺漏清單（全部列出，未補造）**
- **box score 1 場**：`espn:401585732`，2024-04-03（ET）DET@ATL，2023-24。ESPN 為 final，但 `cdn.nba.com` 的 `boxscore_0022301104` 仍顯示 `pregame`（CDN 資料問題）；stats.nba.com 被封鎖無法備援。保留 ESPN 的比分/逐節，不補造 box。`source_fetch_log` 記為 `not_final`。
- **傷病未涵蓋**（皆為賽前沒有任何「涵蓋該隊該日」的已取得報告）：2022-23 PHX@DEN 2023-04-29 / 05-01；2023-24 BOS@NYK 2023-10-25；2024-25 NYK@BOS 2025-05-05；2025-26 CLE@NYK 2026-05-21 / 05-23。
- **傷病報告 6 份缺**（`source_fetch_log`）：4 份官方檔案不存在（403：2021-11-08 19:00、2023-10-23 17:00、2023-10-24 10:00、2024-11-13 18:00 ET）；2 份套件無法解析（2023-05-02 17:00 ET PDF 欄位異常；2025-12-19 16:00 ET 套件拒絕該時間點）。
- 2 場 ESPN 比分與 CDN box score 不一致（2024-25、2025-26 各 1）：以 box score 為準覆寫（官方）。
- 傷病名單中 10,650 / 900,066 列（1.2%）姓名對不到球員表（五季都沒上場的球員，如雙向合約/新秀）；原文保留，球員表增加時 `reresolve_unmatched()` 可補。

## 4. 五季 basic box-score backfill（`python -m core.jobs.box_backfill`）

- 來源：`cdn.nba.com` `boxscore_<id>.json`（stats.nba.com 本機 IP 仍逾時，未使用）。
- 歷史賽事是 ESPN 回填的 `espn:` 暫存列，因此 job **枚舉候選 gameId**（例行賽 `002YY00001..01230`、Cup 決賽 `006YY00001`、季後賽 `004YY00RSG`、附加賽 `005YY00{101,111,121,131,201,211}`，皆實測），抓 box 後以「主客隊 + 開賽 ±12h 最接近的一列」認領並改寫官方 ID；DB 沒有才新增。
- **可續傳 / 冪等**：已完整場次（官方 ID + 原始計數 + 球員 + 逐節 + 衍生指標）跳過；`source_fetch_log` 記錄 missing / error / not_final。`--force` 可整批重寫。
- **單場錯誤不中止**：每場一個交易（中途失敗整場回滾，有測試），死結自動重試；失敗清單在結尾彙整，重跑接續。
- **進度與心跳**：每 100 場一行 log（寫入/認領/不存在/錯誤/秒數）；`data_sources.nba_cdn` 心跳（ok/warn/error）。
- **來源限流偵測**：cdn 對「不存在」與「被限流」都回 403。NotFound 先重試 2 次再算 missing；連續 12 個 missing 時用 canary（已知存在的場次）確認，canary 失敗則退避（60/180/600s）後優雅中止並清除被誤記的 missing。
- 執行結果：2021-22 ~ 2025-26 全部完成，錯誤 0（過程中發現並修正 2 個 bug，見 §9）。

## 5. 衍生球隊進階指標（`core/metrics.py`，`team_game_derived`，`formula_version = box-v1`）

來源：CDN 基本 box score 的球隊統計（原始計數存於 `team_game_stats` 新增欄位 `fgm/fga/fg3m/fg3a/ftm/fta/team_min`）。官方 `pace/off_rtg/def_rtg` 欄位語意不變（仍留給 stats.nba.com 來源）。符號：下標 o＝對手；MIN＝球隊總分鐘（240 + 25×OT 節數）。

| 指標 | 公式 |
|---|---|
| 估計控球數 poss_i | `FGA + 0.4·FTA − 1.07·OREB/(OREB + DREB_o)·(FGA − FGM) + TOV`（Basketball-Reference 球隊公式） |
| poss（兩隊相同） | `(poss_i + poss_o) / 2` |
| pace | `48 · poss / (MIN/5)` |
| ORtg / DRtg / net | `100·PTS/poss` / `100·PTS_o/poss` / `ORtg − DRtg` |
| eFG% | `(FGM + 0.5·3PM) / FGA` |
| TS% | `PTS / (2·(FGA + 0.44·FTA))` |
| TOV% | `TOV / (FGA + 0.44·FTA + TOV)` |
| ORB% / DRB% | `OREB/(OREB+DREB_o)` / `DREB/(DREB+OREB_o)` |
| FTr / 3PAr / AST% | `FTA/FGA` / `3PA/FGA` / `AST/FGM` |

欄位定義（對照官方）：球隊 REB/OREB/DREB 只含球員個人（CDN 的 `reboundsTotal` 含「球隊籃板」，2021-22 平均 52.4 次/隊/場，聯盟真實約 44；用它會把 pace 低估約 4%，已修正）；TOV 含球隊失誤（`turnoversTotal`）。原始計數不完整 → 衍生指標為 NULL，**不補造**。

**驗證**（stats.nba.com 不可用，改以下列方式；`python -m core.jobs.validate_derived --sample 120`）
- 隨機 120 場重抓 CDN：eFG / TS 與 CDN 官方欄位 `fieldGoalsEffectiveAdjusted` / `trueShootingPercentage` 最大絕對誤差 **1e-15**；入庫原始計數與 CDN 當下值 0 筆不符；球員分鐘/得分加總 = 球隊值 0 筆不符。
- BBR 公式 vs 簡化公式（`FGA+0.44FTA−OREB+TOV`）的控球數差：平均 −2.57、標準差 0.77。
- 聯盟平均（特徵底座 `home_pace_r10`）pace ≈ 98.5、ORtg ≈ 114；官方 advanced 無法對照，**pace/ORtg 為公式估計**（通常與官方差約 1%，此處未能驗證）。

## 6. ML-ready 賽前特徵底座（`core/models/pregame_features.py`、`python run_build_features.py`）

- 輸出 `pipeline/artifacts/pregame_features_pregame-v1_off{offset}.csv.gz`（gitignore）：6,605 場 × 108 特徵欄，另有 `y_*` 標籤欄（`feature_columns()` 會排除）。
- 特徵：滾動（過去 5/10 場，跨賽季連續，< 3 場為 NaN）pace、ORtg、DRtg、net、eFG、TS、TOV%、ORB%、DRB%、FTr、3PAr 與對手允許值（防守面）；`rest_days`、賽季場次；近期先發平均分鐘、輪替人數、前 8 名分鐘占比、板凳分鐘占比；傷病：`inj_known`、報告距 cutoff 分鐘、Out/Doubtful/Questionable 人數、**分鐘加權傷病衝擊**（Σ 缺陣機率 × 球員健康時平均分鐘，/240）、**預期先發**（過去 10 場先發最多的 5 人）的加權缺陣人數與分鐘損失；主−客差。
- **洩漏防護**：比賽依 `game_time_utc` 排序、同開賽時間分組，「先算這組特徵，再把這組結果併入狀態」，結構上無法看到同刻或之後的資料；每列帶 `{side}_asof_game_utc`，`assert_no_leakage()` 逐列檢查嚴格早於開賽，傷病報告時間必須早於 cutoff。`decision_offset_min`（0/60/120）可模擬「開賽前 X 分鐘做決策」。傷病：`inj_known=0` 時特徵為 NaN，不是 0。
- 品質：最大 NaN 比例 0.7%（各隊前 3 場）；傷病已知比例 99.85~100%。

## 7. 測試

| 項目 | 結果 |
|---|---|
| 新增測試 | formula（`test_metrics.py`）、as-of / 消失語意（`test_injury_asof.py`）、快照冪等/併發/原子性與回填續傳（`test_injury_history.py`）、box 回填冪等/續傳/死結/封鎖偵測（`test_backfill.py`）、洩漏防護（`test_pregame_features.py`）、稽核正確性（`test_coverage_audit.py`） |
| 完整 `pytest`（`cd pipeline && pytest`，含既有 84 項） | **173 passed**（426 秒；`-m "not db"` 純邏輯 106 項 < 2 秒） |

涵蓋：衍生指標公式（含 OT、缺值不補造、零分母）；as-of（嚴格早於 cutoff、報告恰在開賽時刻被排除、decision offset、NYS 往前找、未涵蓋 = Unknown、跨日不延續、older snapshot 後寫入不改結果）；球員消失（快照層 + `injuries` 層，含冪等、NYS、隊伍不在報告、補抓舊報告）；回填冪等 / 中斷續傳（`limit` 模擬中斷、單場失敗、寫入中途失敗回滾）；特徵洩漏（截斷未來資料、竄改未來資料、竄改當場結果、同開賽時間、輸入順序、報告在開賽後）；稽核（已知缺陷的合成資料，覆蓋率與缺漏定位完全吻合）。

## 8. 剩餘風險 / 限制

1. **pace / ORtg / DRtg 是公式估計**，stats.nba.com 仍被擋，無法與官方 advanced 對照（eFG/TS/原始計數已與 CDN 官方值驗證）。
2. **`P_ABSENT` 先驗是固定常數**，與實測有落差（Doubtful 0.75 vs 0.98）；要用於模型請以 walk-forward 校準。
3. **「整隊無人列入 = 已申報」是推定**（報告對未申報的隊伍會顯示 NOT YET SUBMITTED）。實證支持（缺陣率 3.7%），但仍是推定。
4. **傷病衝擊的「健康時平均分鐘」** 取球員自己過去最多 10 場，長期缺陣者用的是很久以前的分鐘（不隨時間衰減）；`inj_min_lost_share` 因此是「相對滿編陣容的分鐘損失」，球隊近期戰績已部分反映長期缺陣，C.5C 需注意共線性。
5. **資料庫容量**：目前 299 MB（Supabase 免費方案 500 MB），`injury_report_entries` 90 萬列約 100 MB 表 + 索引；若要再擴充（更密集的報告、更多季）需瘦身（例如只留對特徵有用的時間點、或壓縮 Available 的 G League 列）。
6. **Supabase session pooler 全站共 15 條連線**：回填用多執行緒/多行程時會與網站爭用；`PG_POOL_MAX` 預設 4（回填 job 設為 workers+1）。
7. 1 場缺 box（§3）、6 份傷病報告缺、1.2% 傷病姓名未對應。
8. 本機 D1 / Node 測試未重跑（本階段沒有碰前端或 API 程式碼；`injuries` 的 Postgres 變更是新增索引/約束與更完整的資料）。
9. 歷史傷病只用開賽前 0~2 小時的整點（2025-12-22 起加 15 分鐘）+ 前一日 17:00 + 當日 10:00 報告；若 C.5C 想用更早的決策時點需補抓。

## 9. 過程中發現並修正的問題

- **ESPN 認領 SQL（C.5A `adopt_espn_game`）**：同一對主客隊連續兩天主場對打，±1 天條件會同時命中兩列而違反 `UNIQUE(nba_game_id)`。改為只認領開賽時間最接近的一列（有回歸測試）。
- **CDN 球隊籃板定義**：見 §5（pace 低估 4%，改用球員個人籃板；全部重抓 `--force`）。
- 併發回填的死結（`players` 多列 UPSERT 鎖定順序）：依 `nba_player_id` 排序 + 死結重試。
- cdn.nba.com 的 403 同時代表「不存在」與「被限流」：改為多次重試 + canary。
- 回填的 `reresolve_unmatched` 逐列更新在遠端 DB 要數小時 → 改批次 UPDATE（4 萬秒 → 42 秒）。
- 附加賽 gameId 後綴的最初猜測錯誤（改為五季實測的 101/111/121/131/201/211）。

## 10. 是否具備進入 Phase C.5C（model re-evaluation）的條件

**具備。** 五季 box score、衍生指標與賽前傷病狀態的覆蓋率都 ≥ 99.85%，缺漏已逐一列出且未補造；特徵底座有結構性洩漏防護與測試；`decision_offset_min` 可讓 C.5C 比較不同決策時點。
C.5C 的建議做法：以 `build_pregame_features()` 的特徵做 walk-forward（評測賽季只跑一次）；傷病先驗與特徵選擇只用評測賽季之前的資料；注意 §8 第 2、4 點。

## 11. 測試結果

`cd pipeline && pytest` → **173 passed**（426 秒）。DB 測試在獨立暫存 schema 內套用全部 migrations（含 0003），不碰 public 資料。
C.5A 既有測試 1 項依新語意調整：`test_injury_two_games_same_report…` — 同一份報告對同一球員改為最多一列（`uq_injuries_report_player`），多賽事日改存於快照表。
Node 測試未重跑（未改前端 / API 程式碼）。
