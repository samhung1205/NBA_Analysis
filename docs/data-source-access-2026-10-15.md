# 免費資料來源與封鎖處理（2026-10-15）

使用者選擇：免費優先，台彩接受手動 HAR 匯入。這份文件區分「來源可取得」與「已接入 production 特徵」，不更動 `ml-v2.0` / `execution-v1` / `risk-v1`。

## 1. stats.nba.com 不是所有功能的必要條件

| 資料 | 免費路徑 | 目前實作／限制 |
|---|---|---|
| 球員基本數據、實際先發、分鐘、正負值 | NBA CDN boxscore | 既有 pipeline 已使用，歷史資料已回填；當場 box 是賽中／賽後事實，不能當成賽前已知先發 |
| 傷病、歷史輪替推估的預期先發 | 既有官方傷病 PDF + CDN 歷史 box | 現有模型已使用部分特徵，無需 stats.nba.com；推估名單不等於 confirmed lineup |
| 五人同場 lineup / on-off | CDN play-by-play + 歷史 box 的候選重建路徑 | 已實測能取得逐球與換人資料；**尚未完成五人陣容重建、覆蓋率與正確性驗收**，不能宣稱等同官方 lineup endpoint |
| 旅行距離／跨時區 | CDN 賽程的時間、場館／城市 + 有版本的場館座標／時區表 | 無需 stats.nba.com；尚未實作旅行特徵。只可稱場館間估計距離，不是實際航班或球隊移動；中立場、海外場、延期、缺場館與跨季首戰不能假定主隊主場 |
| 賽前裁判分派 | `official.nba.com/referee-assignments/` | 本次新增獨立 parser 與唯讀 CLI，可解析當日 NBA 分派，排除 WNBA；尚未接入 DB / scheduler / 模型 |
| 歷史裁判 | CDN 歷史 box 的 officials | 樣本可讀；賽後取得的名單不能當作歷史賽前快照，完整歷史覆蓋尚未驗證 |

NBA 官方裁判頁說明約於每個比賽日 **09:00 ET** 公布。collector 以實際回應完成時間記錄 `observed_at`，頁面的日期只當 `report_date_et`，不能偽造當天 09:00 已取得。[NBA 官方來源](https://official.nba.com/referee-assignments/)

還有獨立於封鎖的限制：確認先發通常約在開賽前 30 分鐘才發布。現行決策凍結在 T-60，不能使用之後才確認的名單；免費或付費來源都不能消除這個時間差。未來若評估 T-30，需另立策略／模型版本，不修改舊決策。[SportsDataIO 官方工作流程](https://sportsdata.io/developers/workflow-guide/nba)

五人 lineup 研究需要處理每節開場、同時換人順序、缺漏／更正事件、延長賽與無法確定陣容的區段，驗收五人守恆、球員分鐘及得分／正負值對帳；無法確定時保留 unknown。不能把個人 plus/minus 或 `personIdsFilter` 直接稱為五人同場 on/off。新特徵仍需獨立時序驗證與 2026-27 前瞻觀察。

因此 Phase E.1 舊報告「lineup 需要 stats.nba.com」應理解為當時尚未驗證其他路徑，不能推論球員、旅行與裁判全部被擋。本次沒有改用代理 IP、stealth、解 CAPTCHA 或付費供應商。

## 2. 本機來源實測與重現

2026-10-15 13:49 UTC，在目前電腦以專案既有 nba_api 標頭，四個唯讀請求均為 HTTP 200：

| 來源 | 實測內容 |
|---|---|
| CDN schedule | 1,274 場，其中 1,268 場有 arenaName；6 場缺值，不能造場館 |
| CDN boxscore `0022400001` | 35 筆球員列、3 位裁判、場館時區 `America/New_York`；含未上場者，不是 35 位都出賽 |
| CDN playbyplay 同場 | 538 個事件、106 個 substitution；不等於已重建 lineup |
| 官方裁判頁 | 當日 2 場 NBA 分派，3 位裁判／場 |

新增 `pipeline/run_source_check.py`，在 repo 根目錄執行：

```bash
cd pipeline
.venv/bin/python run_source_check.py
```

只發出四次請求、不讀 `.env` / DB、不連 stats.nba.com、不連台彩，也不消耗 Odds API 額度。可用 `--game-id <10位數字>` 指定已知存在的歷史 canary；尚未發布的未來 box 403/404 不能單憑這點認定 IP 被封鎖。

裁判空表為 `waiting`；日期不是當日 ET 為 `stale`；缺表、結構改變、缺少必要裁判或 HTTP 失敗為 `unavailable`，不把壞掉的 parser 說成零場。遇到失敗不重試／輪替 IP，其他來源仍完成檢查。exit 1 表示有 unavailable / stale；waiting 不算擷取失敗。

本機成功不能證明 Railway 成功；正式主機應在取得相同程式後執行同一唯讀命令確認。此任務未部署程式，也未改 scheduler 或讀寫正式 DB。

## 3. 台彩：保留手動擷取，新增真正離線的檢查

先用一般 Chrome 確認能正常載入台彩 NBA。DevTools → Network 開啟後重新載入 NBA 聯賽，並進入欲使用賽事的詳情，載入全場與上半場玩法；只看聯賽列表可能只有全場三種盤。

使用 **Export HAR (sanitized)**，需含回應內容，存到 `pipeline/captures/`。匯入器不重播瀏覽器 session，不需要 Cookie / Authorization；sanitized 匯出較適合這個用途，但檔案仍不應提交或分享。不要對 Network 套用會遺漏聯賽 eventGroup 的篩選。[Chrome 官方匯出說明](https://developer.chrome.com/docs/devtools/network/reference/#save-as-har)

### A. 完全離線檢查

```bash
cd pipeline
.venv/bin/python run_twsport_har.py captures/<file>.har --inspect
```

新模式**不讀 DB / `.env`、不連外、不寫入或刪除 HAR**。輸出包含各場玩法、缺少玩法、開盤玩法、原擷取時間，以及 T-60 對應的擷取窗：

- `timing_compatible`：擷取時點落在時間窗內，僅為時間條件相容。
- `capture_too_early`：到 T-60 時盤口將超過 60 分鐘。
- `captured_after_cutoff`：T-60 之後才取得，不能回填成當時已知。
- `decision_time_passed`：現在已超過 T-60；是否仍有排程寬限或已有不可變 paper decision，須由正式執行流程判斷。

缺少 h1 玩法時，先在一般瀏覽器載入單場上半場盤後重新匯出；不要自己補賠率。解析成功不保證可比對 NBA 對戰、符合策略賽事階段、已入庫或 actionable。

### B. 比對與匯入

確認預定的 `DATABASE_URL` 目標後，仍使用原流程：

```bash
# 在 pipeline 目錄；會讀 DB 做對戰比對，不寫入
.venv/bin/python run_twsport_har.py captures/<file>.har

# 核對比對結果後，寫入設定的 DB，成功後刪除 HAR
.venv/bin/python run_twsport_har.py captures/<file>.har --import --delete-after
```

HAR 的兩小時拒絕匯入門檻與策略的六十分鐘報價有效期不同。現行 T-60 + risk-v1 要求**在開賽前 120～60 分鐘內擷取**；建議接近 T-60 前擷取並立即匯入。多個開賽時段應分批，不是一份早上的 HAR 可以涵蓋整天。

例如台灣 08:00 開賽，T-60 = 07:00，擷取窗為 06:00～07:00；實務上可在 06:50 左右擷取並盡快匯入。`fetched_at` 沿用 HAR 回應完成時間，`--allow-stale` 不會解除 cutoff 或報價新鮮度限制，也不會讓早已 no-bet 的 immutable paper decision 變成下注。

若一般瀏覽器本身也無法載入，就無法產生有效 HAR；需先排除正常瀏覽器的連線問題或聯絡台彩支援，不能以空回應或國際盤替代台彩。現有自動擷取維持關閉／BLOCKED，不假裝已解除 Cloudflare。

## 4. 本次驗證與尚待完成

- 相關來源／HAR／readiness 回歸：58 passed；其中新增 19 項，涵蓋 NBA/WNBA 隔離、頁面改版、空表／過期、被擋不重試、不暴露回應、T-60 時間邊界、HAR 敏感標頭不輸出及 inspect 禁止 DB import／寫入旗標。
- 四個實際 HTTP 請求及官方裁判解析成功；無正式資料寫入，未操作使用者的台彩瀏覽器或送出通知。
- 尚需真實新鮮 HAR 完成當日對戰比對與匯入驗收；現有 fixture 只用於測試，沒有插入正式 DB。
- 本次完成的是來源路徑驗證與操作工具。五人 lineup 重建、旅行特徵與裁判資料的持久化／排程／研究模型，仍是後續 Phase E 工作；不能把 HTTP 200 當成這些功能已完成。

> 後續（2026-10-21）：裁判分派已接入 DB 與排程，見 [phase-e2-referees-report.md](phase-e2-referees-report.md)。
