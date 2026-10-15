# AGENTS.md 設計依據

查閱日期：2026-10-15。這是官方指南的專案化取捨，不是模型能力排行榜；根目錄 `AGENTS.md` 才是日常指令。

## 官方指南與取捨

| 官方來源 | 本專案採用方式 |
|---|---|
| [OpenAI：Astra 的 skills / prompts / AGENTS.md](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra/) | 僅保留長期契約與情境入口，避免每次重讀整個文件庫、重複驗證或不必要的確認。 |
| [OpenAI：GPT-6 指南（含 Astra、GPT-6.1 Sol）](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6.1-sol) | 使用可觀察的完成條件與明確範圍；依任務選擇檢查，不把模型或 effort 寫死。API 與桌面工具的設定值需各自確認。 |
| [OpenAI：AGENTS.md 載入與作用域](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | 共用規則放 repo root，細部指南按需連結；未為這個規模新增多層互相重複的指令。 |
| [Anthropic：Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) | 清楚表達期望結果與專案約束，保留實作判斷空間；Skills 依相關性取用。 |
| [Anthropic：Sonnet 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5) | 對授權內的例行工作持續完成；交付必須有實際檢查，無法執行時揭露限制；不自動擴張任務。 |
| [Anthropic：Opus 5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5) 與 [Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5) | 不加「無條件再驗證一輪」或自動找子代理複查的指令；不把不同模型的 effort 名稱視為相同運算量。 |
| [Anthropic：Claude Code 專案記憶與 AGENTS.md](https://code.claude.com/docs/en/memory#agentsmd) | 以一份共用檔維護規則。近期 Claude Code 可直接讀取 AGENTS.md，實際載入仍受版本、設定與既有 CLAUDE.md 影響。 |

## 模型差異怎麼處理

- Astra / Sol：官方 GPT-6 指南可作共同起點，但仍須以本專案任務驗證效果。共用檔不指定某個模型一定負責某種工作，也不自行切換模型。
- Sonnet：對具體任務保留清楚的完成條件，尤其避免「沒有執行檢查卻報完成」。
- Opus：控制範圍與回覆長度，避免額外研究、重複測試與自動委派。
- 所有模型：金額正確性由程式契約、回歸案例與 DB 約束證明，不能靠更多提示或更高 effort 保證。

因此 AGENTS.md 沒有逐步思考模板、模型 API 參數表、每次必讀的全專案清單、固定全套測試、定時百分比播報或自動平行代理規則。模型版本資訊留在本說明，避免快速過時的資訊佔用每次工作上下文。

## Claude Code 相容性

查閱時官方文件說明，v2.1.277 起可在符合條件的環境讀取 AGENTS.md；祖先目錄或專案的 CLAUDE.md / CLAUDE.local.md 可能改變預設載入選擇。舊版或特定設定若未讀取此檔，可在根目錄 CLAUDE.md 使用 `@AGENTS.md` 匯入，並於 `/memory` 確認。此任務未改動使用者的 Claude 設定，也未額外建立重複指令檔。

## 專案特有的規則

Python 負責風控數學、Postgres / D1 隔離、append-only 稽核、UTC / ET / 台灣日期、凍結模型與策略，以及四種證據分離，均來自本專案既有實作與決策，並非模型廠商提出的 NBA 策略。模型指南用於決定「如何寫清楚這些規則」，不取代專案契約。
