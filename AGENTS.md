# NBA Analysis — Agent Instructions

## 協作與範圍

- 使用繁體中文回覆；程式識別字沿用既有英文命名。
- 每次任務評估可用且相關的 Skills，僅在能顯著提升準確性或效率時使用；首次使用時簡短告知。
- 在使用者授權範圍內完成實作與必要驗證。例行讀取、局部修正、隔離測試不必逐步詢問；有影響結果的歧義才提出具體問題。
- 保留使用者與其他工作留下的未提交變更；不要覆寫、清除或提交不屬於本次的內容。
- 預設沿用目前模型與設定，不自行切換模型、提高 effort、啟動子代理或擴張任務。
- 回報已確認的結果、測試與仍未驗證的限制；不輸出內部推理過程，不將計畫當作完成證據。

## 按任務取用文件

不必每次重讀整個專案。以程式、schema、實際檢查為準，文件時間點不同時要註明。

| 任務 | 入口 |
|---|---|
| 使用流程、專案導覽 | `README.md`、`docs/README.md` |
| 部署、正式環境、HAR、回滾 | `docs/production-runbook.md` |
| 模型規格與評估 | `pipeline/core/production/spec.py`、相應 `docs/phase-c5*-report.md` |
| 定價、風控、結算與決策 | 對應 `pipeline/core/{pricing,sizing,execution,decision}/`、`docs/phase-d*-report.md` |
| 已知缺口與恢復工作 | `docs/project-audit-2026-10-15.md`，先確認是否已有後續修正 |
| 環境異常 | `docs/dev-pitfalls.md` |

## 架構與資料契約

- `src/` 是 Hono / TypeScript API 與頁面，`public/static/js/` 是瀏覽器腳本；`pipeline/` 是 Python 資料、模型與決策引擎。
- 預測、機率、去水、EV、Kelly、exposure、剩餘額度、bankroll 彙總由 Python 計算並物化。TypeScript / JS 只顯示、記錄事實、比較已物化上限；不得另寫一套風控數學。
- 正式使用 Supabase Postgres（Session pooler，5432）；D1 僅供本機網站開發。修改 SQL 時維持 adapter 與兩種方言契約。
- DB 存 UTC；betting day 取開賽的 Asia/Taipei 日期；NBA 賽事日及傷病報告的 ET 日期另行換算。沿用集中時間工具。
- 新增、作廢、更正、結算都保留稽核。注單執行欄位不可原地覆寫或硬刪；更正建立新列並 supersede 舊列，相關寫入須原子化。
- 風險判斷須使用仍有效且版本一致的 Python 物化狀態；過期、缺漏或重算中不能宣稱合規或視為有效額度。更正不得繼承未重新驗證的合規標記。
- 保留冪等性、watermark、risk version claim 與併發保護；缺少上下文時明確回報，不能猜測餘額或默默截斷金額。

## 模型與證據邊界

- 現行 `ml-v2.0`、`dist-v2`、`proportional-v1`、`risk-v1`、`execution-v1` 是既有基準；一般修正不得暗中調參或改策略。
- `risk-v1`：¼ Kelly、單筆 2%、同場 3%、單日 8%，分母為凍結的 day-start bankroll。
- 研究新策略須獨立版本與按時間切分的評估；不得以已看過的測試季或近期 ROI 反覆調現行設定。
- as-of 只能使用當時可取得的資料、預測及 artifact；不可用未來盤口、傷病、賽果或最新模型回填歷史。
- 歷史模型評估、歷史投注證據、前瞻 paper、使用者實際下注分開保存與呈現。沒有觀測盤口時，ROI 證據為 unavailable，不是 0 或獲利證明。
- 台彩為可操作市場，國際盤僅供診斷；不得用國際盤替代台彩證據。平台不會自動下注。
- 台彩自動化被阻擋時沿用手動 HAR，不繞過防護。HAR 是敏感檔案，不可輸出內容或提交。

## 安全的工作環境

- 不印出或提交 `.env`、`.dev.vars`、連線字串、API key、session secret、HAR、真實帳號資料。
- 使用既有依賴與 lockfile；升級套件需與任務有關，不為繞過錯誤任意重裝或更換版本。
- `.dev.vars` / `pipeline/.env` 可能指向正式 DB。測試寫入前確認目標，不能把本機 URL 或 `--dry-run` 一概當成無正式存取。
- API smoke 會寫入：只對獨立 D1 seed 或明確隔離的測試 DB 執行。建議使用暫存目錄的 Wrangler 設定與 state，排除正式 `DATABASE_URL`。
- Python DB 測試使用隨機暫存 schema，套用 migrations 並清理；執行前確認 fixture 的 search_path 隔離仍有效，不在 public 灌 seed 或 truncate。
- 不以稽核／一般修正推定部署、正式 migration、資料刪除或模型 promotion 已獲授權；只有工作需要且使用者授權涵蓋時才操作。已明確授權的操作不用反覆確認。

## 驗證與交付

按變更影響選擇能重現行為的檢查；通過後，除非有新變更、失敗或未解疑慮，不重複或擴大測試。

| 影響範圍 | 既有檢查 |
|---|---|
| 網站打包 | 根目錄 `npm run build`（不是 TypeScript 型別檢查） |
| 登入 / session | `npm run test:auth` |
| D.5 更正 / 出金 guard | `npm run test:decision`（記憶體 SQLite + Hono，不連網；Node 24） |
| API / D.5 寫入流程 | 隔離網站的 `BASE=http://127.0.0.1:<port> npm run test:api`，加上相關回歸案例 |
| Python 邏輯 | `cd pipeline` 後 `.venv/bin/python -m pytest -m 'not db' -q`，或指定受影響測試 |
| Python DB / schema / ledger | `cd pipeline` 後 `.venv/bin/python -m pytest -m db -q`，或指定相關 DB cases |
| 瀏覽器互動 | 隔離站台的受影響操作，檢查 API 結果、頁面狀態與 runtime error |

- 金額、狀態失效、併發與稽核變更須有能區分修正前後的回歸案例；不要只測實作細節或為文件修改新增測試。
- DB 測試 skipped 不等於通過；建置成功不等於線上可用；離線重現不等於前瞻驗證。
- 沒有真實賽程／盤口時記錄 WAITING 與未驗證範圍，不造正式資料以取得綠燈。
- 更新當前使用文件與本次修正紀錄；既有 Phase 報告保留為歷史快照，可加後續連結，不改寫成當前驗收。
- 交付簡述：改了什麼、實測結果、未驗證事項。不得把本機修正說成已部署。

本文件採跨模型通用規則；官方指南評估與來源見 `docs/agent-instructions-rationale.md`（2026-10-15）。
