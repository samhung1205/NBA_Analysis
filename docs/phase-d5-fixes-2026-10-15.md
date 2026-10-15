# D.5 稽核缺口修正（2026-10-15）

狀態：**本機已修正與驗證，尚未部署。** 原始發現與修正前證據保留於 [專案稽核](project-audit-2026-10-15.md)。

## 更正注單：明確確認與保守合規狀態

原問題：更正 stake / odds / side / line 時複製原注單的 `compliant`，並把風險檢查標為跳過。

現行處理：

- 更正資料通過格式驗證後，未明確確認時回 `409 confirmation_required`，不建立新紀錄、不修改舊紀錄或版本。
- 確認後新列標為 `missing_context`，保存 `correction_not_revalidated`、`replacement_capacity_not_materialized`、此次原因與確認時間。原機會與參考盤口保留作追溯用途，不代表更正後已通過策略驗證。
- 更正原因必填，並要求 `confirm_override: true`；減少 stake 也使用同一語意，避免誤以為降低金額便能證明當前仍合規。
- 不以包含舊注單的物化剩餘額度判斷替代單，也不在 TypeScript 重算 exposure。後續 Python 物化仍會排除 superseded 舊列、納入有效新列，更新實際 exposure 與剩餘額度。
- 方向／盤線改變時清除自動結算身分，保留既有手動結算流程；新增 line 有限值、draw 玩法與未來下注時間的檢查。
- 沿用 append-only、新列 supersedes 舊列、risk-state claim 與原子交易。更正支援 `client_request_id` 重送，已完成的相同請求回原 replacement；相同 key 用於不同舊注單時拒絕。
- 投注頁收到 409 後顯示確認訊息；取消不寫入，確認才重送，沿用同一 request id。

此取捨修正「未驗證卻宣稱合規」的缺口；**沒有新增替代注單的 Python 反事實額度計算**。若未來希望更正後仍能判為 `compliant`，需另設計能扣除舊單、處理歷史下注時點與當前狀態的 Python 驗證結果，不能恢復複製舊標記。

## 出金：只接受有效摘要

`POST /api/bankroll/entries` 的 withdrawal 以 `risk.capacity_valid` 為前提，不再只排除 `recalculation_pending`。過期但版本一致、account version 不一致、ledger / event watermark 不一致及尚未物化，均回 409，沒有 ledger 寫入。

有效摘要下的正常出金、餘額不足拒絕、相同 request id 重送及併發 claim 保持原有行為。此處只與 Python 已物化金額比較，未新增 TypeScript 帳務運算。

## 驗證

| 檢查 | 結果與範圍 |
|---|---|
| `npm run test:decision` | **19 passed**；Hono 路由＋記憶體 SQLite，套用真實 D1 migrations / seed，無正式存取 |
| 隔離 D1 的 `npm run test:api` | **162 passed / 0 failed**；實際 Cloudflare runtime，新增更正確認與 replay 驗收 |
| `npm run test:auth` | **14 passed** |
| `npm run build` | 通過 |
| 在 `pipeline` 下執行 `.venv/bin/python -m pytest tests/test_decision.py -q` | **47 passed**；既有 exposure / bankroll / evidence 與單一 Python 數學契約 |
| Playwright 本機投注頁 | 已登入 seed 帳號；更正取消後紀錄不變；確認後新增 50,000 的替代列，顯示「缺少風險背景」；原列被取代 |

19 項新回歸涵蓋未確認超額更正不寫入、確認後不繼承合規／舊確認、減額、方向與盤線、自動結算身分清除、非法輸入、冪等與併發；出金涵蓋過期但版本一致、各 watermark 變動、無物化、正常／超額／重送／併發。

瀏覽器中的兩次 409 是預期的更正確認預檢；確認請求為 201。新回歸的 SQLite adapter 並非完整 Workers 模擬，因此另外執行了真正 D1 runtime 的 smoke，沒有將兩者混為一談。

本次未重跑完整 741 項 Python／遠端 Postgres suite：未改 Python、schema 或 DB adapter；其完整通過結果來自本次較早的專案稽核，不能當成本次新跑結果。針對本次改動執行路由、交易、前端及相關 Python 契約驗證。

## AGENTS.md 與文件

- 新增根目錄 [AGENTS.md](../AGENTS.md)：精簡的跨模型工作規則，搭配按任務取用的文件入口。
- 官方指南、Astra / Sol / Sonnet / Opus 的取捨及 Claude Code 載入相容性見 [指令設計依據](agent-instructions-rationale.md)。
- 保留原本未提交的文件整理，只針對修正語意、驗收數字與目前狀態更新 README、文件索引與階段摘要。
- 不改寫既有 Phase D.5 完成報告，只在原稽核加上後續修正連結。

## 後續順序

1. 將已驗證的網站修正部署到預定環境後，確認實際部署版本；本次沒有執行 commit、push 或部署。
2. 真實賽事進入同步視窗、使用者提供有效台彩 HAR 後，驗收同步 → 預測 → 定價／sizing → T-60 paper → 賽果 → 結算／ledger。
3. 真實樣本尚未到位時維持 WAITING，不造 production 盤口、預測或下注。
4. 再依使用者選擇推進 Phase E。模型與風控基準維持凍結；line movement 需要先累積真實盤口序列。

所有寫入驗證都在記憶體 SQLite 或 `/private/tmp/nba-d5-fix-20261015/` 的獨立 D1，未寫入正式資料、套用 migration、變更模型 artifact 或消耗 Odds API 額度。
