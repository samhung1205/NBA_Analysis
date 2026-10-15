# 文件索引（Docs Index）

使用說明請看根目錄 [README.md](../README.md)。本資料夾收錄維運手冊與開發報告。

## 維運（Operations）

| 文件 | 用途 |
|---|---|
| [production-runbook.md](production-runbook.md) | **正式環境操作手冊**：Railway / Cloudflare 設定、每日維運、狀態判讀、台彩 HAR 匯入、回滾 |
| [../DEPLOY.md](../DEPLOY.md) | 部署平台選型評估（2026-10-05；部分數字如 job 數、PBKDF2 迭代數已被 runbook 更新） |

## 現況與稽核（Status）

| 文件 | 用途 |
|---|---|
| [project-audit-2026-10-15.md](project-audit-2026-10-15.md) | 恢復工作前的全專案稽核：各階段驗證結果、已知缺陷（P1/P2）、建議推進順序 |
| [phase-d5-fixes-2026-10-15.md](phase-d5-fixes-2026-10-15.md) | P1/P2 本機修正與回歸驗證，部署與真實賽事流程仍待完成 |
| [../AGENTS.md](../AGENTS.md) | 跨模型共用的專案工作規則 |
| [agent-instructions-rationale.md](agent-instructions-rationale.md) | OpenAI／Anthropic 最新官方指南與指令設計取捨 |
| [stage2-phase-summary.md](stage2-phase-summary.md) | 階段二 Phase A ~ D.5 進度總覽、關鍵數字與指令 |

## 開發報告（Development Reports）

| 文件 | 內容 |
|---|---|
| [stage1-website-report.md](stage1-website-report.md) | 階段一網站：頁面、API、資料架構、DB adapter、seed、驗收紀錄 |
| [phase-c5a-report.md](phase-c5a-report.md) | 資料來源 fallback、排程、傷病硬化 |
| [phase-c5b-report.md](phase-c5b-report.md) | 五季歷史資料與賽前特徵底座 |
| [phase-c5c-report.md](phase-c5c-report.md) | 時間尺度特徵與 walk-forward 模型選擇 |
| [phase-c5d-report.md](phase-c5d-report.md) | Production inference 與模型生命週期 |
| [phase-c5e-report.md](phase-c5e-report.md) | 預測分佈（任意盤口線機率） |
| [phase-d1-report.md](phase-d1-report.md) | 盤口擷取與市場正規化 |
| [phase-d2-report.md](phase-d2-report.md) | 去水、edge、EV |
| [phase-d3-report.md](phase-d3-report.md) | Qualification、Kelly sizing、risk-v1 |
| [phase-d4-report.md](phase-d4-report.md) | 策略執行、結算、paper ledger |
| [phase-d5-report.md](phase-d5-report.md) | 決策中心、實際下注 exposure、bankroll |
| [phase-e1-telegram-report.md](phase-e1-telegram-report.md) | Telegram 推播（notify-v1）：ACTIONABLE、每日摘要、系統告警 |

## 參考（Reference）

| 文件 | 用途 |
|---|---|
| [dev-pitfalls.md](dev-pitfalls.md) | 開發踩坑紀錄：連線池、資料來源封鎖、APScheduler、PBKDF2 等 |
| [../NBA預測平台規格書_兩階段.md](../NBA預測平台規格書_兩階段.md) | 原始規格書 v2.0 |

> 各 Phase 報告是完成當時的快照；其中「尚未套用 / 尚未部署」等字樣不代表今日狀態。
