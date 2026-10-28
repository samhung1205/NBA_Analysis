# Chrome 同步上傳連線中斷修正（2026-10-28）

使用者安裝 v1.0.0 助手後，擷取完成並送往 `/api/twsport-sync/:id/capture`，正式 API 回傳 500。工作收據沒有 received_at／started_at，未排入 Python；當時台彩快照仍為 0。公開 fixture 的 Node／Postgres 路徑能匯入 12 場，正式 Cloudflare 即時日誌則捕捉到 `write CONNECTION_CLOSED`。這個 driver 錯誤本身不能證明 SQL 是否曾提交，不能直接重建工作或盲目再寫。

## 修正

- 先讀取有上限的上傳內容，再開啟資料庫連線，避免等待 body 時占用閒置 socket。
- 僅對明確的連線中斷／逾時錯誤，使用新的 request-owned 連線重試一次。
- 重試先讀原工作：若第一次已提交，直接回傳既有收據；只有仍為 capturing、且未逾期者才條件式排入 queued。工作 ID、capture time 與冪等性不變。
- 所有網站工作查詢只讀收據欄位，不再為輪詢拉回整份 payload。
- 重試失敗回傳安全錯誤碼與可操作提示；日誌只記 stage、attempt 與白名單 code，不記原始錯誤、參數或憑證。
- 決策中心缺盤口文案改為先使用同步，HAR 保留備援；不改 Python 風控、額度或盤口有效性判定。

## 驗證

- 同步測試 25 passed：新增提交前斷線、提交後失去回應、取得收據時斷線、持續斷線與敏感錯誤不外洩；提交後恢復不能再寫一次。
- 決策 guard 19 passed，網站 build 通過。
- Node + 真實 Postgres adapter 的 12 場 fixture，上傳回應 202／queued；全程隨機暫存 schema，每次操作核對 search_path，測後清理完成。這不代表 Cloudflare 正式執行環境已驗收。

此修正只涉及網站；助手 v1.0.0 不需重新安裝，沒有新增 migration。正式部署與使用者 Chrome 的完整真實同步結果須另記實測證據。
