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

## 正式重現後的補強

acb42d3 正式部署後，實際 79,771 bytes 上傳在 store 階段仍連續兩次 CONNECTION_CLOSED。重連能保留安全失敗與冪等性，但不足以讓這筆內容完成寫入。相近大小的普通 Node／Postgres 隔離路徑可成功；本次不把這個差異推論成已證實的平台固定大小限制。

新增內部 `twsport-inbox-gzip-v1` 格式：網站驗證原始盤口後，用 Workers 原生 CompressionStream gzip，再存為 ASCII base64 envelope。每次儲存的 envelope 上限 48 KiB，超限明確拒絕，不截斷盤口；原始上傳與 Python 解壓結果都維持 2 MiB 上限。Python 先向下相容部署，支援原 JSON 與壓縮 envelope；拒絕毀損、截斷、尾隨資料及超限展開。擷取時間、匹配、原始盤口 provenance、定價與 risk-v1 契約均不改。

驗證：同步測試 27 passed；12 場公開 fixture 約 77 KiB 壓為 2.2 KiB 且 lossless；實際 TypeScript 編碼 → Python 解碼保留中文與毫秒時間。Python 純邏輯 23 passed；新舊格式實際 consumer 匯入／更新決策 2 passed（隨機隔離 schema）。網站 build 通過。先等待 decoder 部署成功，再啟用網站寫入壓縮格式；真實 Chrome 結果另記。

另外以本機 workerd（與正式站相同 compatibility date）實測：77,145 bytes 的 12 場公開 fixture 編為 2,265 bytes，交給實際 Python decoder 後內容與時間完全一致。向下相容 decoder 的 Railway 部署 a24607ec-13fd-4bde-9e0f-962f2748593d（6307437）已 SUCCESS，之後才推送網站端編碼修正。


## 第一筆真實入庫與處理期限

4964116 正式部署（Pages 66660617、Railway 4c863e26）後，使用者 Chrome 的 79,770 bytes 盤口成功壓成 8,045 bytes，capture 回應 202。工作 d950e6bc-9357-4d30-a76e-e624c77f44c3 已寫入 12 場、72 個真實市場；後續定價／注碼／決策尚未結束時，舊的共用五分鐘工作期限先將收據標為 expired。保留該失敗收據，不以人工改成成功。

補強將上傳與處理期限分開：capturing／queued 仍是原五分鐘期限，worker 交易領取後才設定十分鐘 processing lease。Python 初次驗證仍拒絕超過五分鐘的 capture，不改 risk-v1 的 quote freshness 或額度有效性。逐場與定價／注碼／決策階段會保留 summary 進度及更新處理器心跳；使用持鎖連線寫進度，不增加連線池占用。前端持續輪詢並顯示階段，最後仍以真實結果判定 succeeded／partial／failed。

驗證：同步前端與契約 28 passed、Python 純邏輯 23 passed、隔離 DB 6 passed（包含 processing lease 跨過原上傳期限仍有效，但六分鐘舊 capture 仍被拒絕）；網站 build 通過。

## 小型連線池的實際互等

後續驗收發現 browser_sync 會持有 advisory-lock 連線，再從共用四條連線池借第二條做清理／領取／匯入。正式日誌在這些階段出現 `psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec`，導致工作停在 queued。已確認 Railway 與本機都是 5432、相同 DB 目標，並非 Transaction pooler 設定。

新增 consumer 專用的 connection-bound DB adapter：清理、交易領取、逐場匯入、pricing、sizing、decision、進度與結案都重用已保留的同一連線；cursor 與 transaction 仍各自建立，既有交易原子性／隔離層級保留，不修改全域 pool、不加大正式連線上限。該 serialized consumer 才使用此 adapter，其他排程不變。

隔離 DB 六項再次全部通過；其中壓縮格式的真實 consumer 案例將 pool max 設為 1，成功匯入六種市場、更新決策、清除 inbox，證明不再等待第二條連線。Python 純邏輯 23 passed。

## 完整匯入後的更新診斷與等待時間（2026-10-29）

e9e5510 的真實工作 57abc6b4-624a-48a9-9bb5-c816aaa49320 已匯入 12 場／72 個市場，過舊與拒絕皆為 0；處理耗時 8 分 52 秒，最後停在 decision 階段、decision_refreshed=false。舊收據無各階段結果，不能直接斷言是哪一階段或鎖衝突造成，亦不人工改成成功。

補強收據新增 pricing 錯誤數、sizing 缺漏數、decision 是否 busy／完成與嘗試次數，不記原始錯誤字串。僅在既有 decision_board_job 明確回報物化鎖被占用時，每兩秒重試、最多十五次，且不能跨過 processing lease；其他失敗維持 partial。前端區分盤口完整匯入與後續更新尚未確認。

pricing／sizing 的 persist 改用既有 Psycopg 3.2.10 executemany：沿用相同順序、欄位、ON CONFLICT DO NOTHING、呼叫端交易與完整候選集合，減少逐列網路往返。模型、EV、Kelly、風控比例、quote TTL、盤口時間與稽核資料均不變。[Psycopg 官方契約](https://www.psycopg.org/psycopg3/docs/api/cursors.html)規定未使用 returning 時 rowcount 是累計影響列數，據此維持新增／既有計數。

純 Python 同步測試 27 passed（鎖衝突後成功、持續 busy 上限、非 busy 不重試、lease 邊界）；網站同步測試 29 passed（含完整匯入但決策未確認的文案），build 通過。隔離 Postgres 的同步、pricing 與 sizing 共 15 passed，包含與純計算值一致、版本／組合改變保留舊列、重跑冪等、過期不可操作及 as-of 邊界。最新正式實測結果另記。

bb95099 已正式部署（Pages f7088a23、Railway 49f5bce6），工作 7caf6b6c-06fd-4b60-9507-a2aab418b628 的收據證實：12 場／72 市場、stale/rejected=0、pricing errors=0、sizing pricing_missing=0；decision 連續十五次 busy，因此保留 partial 並清除 inbox。由此才能確認最後是物化鎖占用，非盤口部分匯入。唯讀 pg_stat_activity 分類顯示持鎖交易正逐列 INSERT decision_opportunities，交易已超過 180 秒。

接著將同一快照的 decision_opportunities 改為 executemany；原有新 snapshot 判定、day-start 凍結、bets 補填、欄位順序、完整選項集合與 REPEATABLE READ 原子交易均不變。失敗仍整筆快照回滾，不以放寬額度有效期或忽略鎖取得成功。

決策批次寫入的三項隔離 Postgres 回歸全部通過：完整物化與重跑冪等、已記錄下注建立新快照與補填基準、日初資金凍結與 dry-run 不寫入。正式部署後另做真實 Chrome 驗收。
