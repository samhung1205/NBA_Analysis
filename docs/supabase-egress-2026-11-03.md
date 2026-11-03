# Supabase 免費額度診斷與 Egress 修正（2026-11-03）

## 已確認的限制

使用者選擇維持 Free，不升級。Dashboard 本期為 10/15–11/15，組織 Egress 約 85.983 GB / 5 GB，其中 NBA 約 85.962 GB。Dashboard 已顯示 Egress Exceeded、組織服務限制與 API 402；同組織的其他專案也受影響。這是流量配額，與 Railway 常駐 RAM、先前 RLS 修正不同。

NBA Database Size 約 567.5 MB，亦高於 Free 的 500 MB。資料表以實際保留資料及索引為主，未見足以解決容量的 dead tuples：傷病報告明細約 230 MB、決策機會約 119 MB、注碼快照約 87 MB（含索引、近似值）。不能靠例行 VACUUM 宣稱已釋放足夠空間。

直接 SQL 在診斷時仍可連線，排程心跳仍更新；不能因 Data API 402 就認為背景資料讀取已停止。本次未停用排程、未刪除資料、未放寬 RLS 或 API 權限。

官方文件：[Egress 計量](https://supabase.com/docs/guides/platform/manage-your-usage/egress)、[帳務與組織額度](https://supabase.com/docs/guides/platform/billing-faq)、[方案限制](https://supabase.com/pricing)。Session pooler 回傳資料同樣計入 Egress。

## 原因與修正

`pg_stat_statements` 累計統計顯示，歷史球員輸入查詢約 1,260 次、傳回約 1.79 億列；注碼 provenance 查詢約 3 萬次、傳回約 1.18 億列；整批定價快照查詢約 3.4 萬次、傳回約 4,392 萬列。這些是累計列數，統計區間未必等於帳期，不能換算成各查詢的精確帳單占比。它們與實際程式的反覆全量讀取一致。

1. `pricing/queries.py` 在 Postgres 選出各 series 最新盤口，再載入對應有效預測的定價列。保留時間與 id tie-break；最新 closed/suspended 不會退回舊 open。預測仍由既有 Python 驗證，可退回較早有效版本，不以 MAX(id) 代替。
2. 決策 provenance 只載入所選定價的最新注碼參照，不下載整個 betting day 的歷史注碼列。注碼與決策仍由既有 Python 規則計算，D.4 多時間點歷史重建保持原查詢。
3. 排程預測把歷史賽果、衍生指標與球員輸入存成 volume 中的 JSON 快取，**不在行程間常駐一份記憶體資料**。每次先由 DB 聚合實際欄位內容指紋，只傳回短摘要。未變動時不重傳歷史列；更正、刪除、新增或 as-of 可見集合改變會重讀。
4. 指紋與冷讀共用 read-only repeatable-read snapshot。呼叫者已有 transaction 時沿用直接讀取，不提交呼叫者交易。快取毀損／不可寫時使用完整 DB 輸入，不能用空資料或過期快取。傷病每輪照常讀取；研究、dry-run 與重訓保持直接讀取。

快取路徑：`MODEL_ARTIFACT_DIR/.runtime-cache/history`，一個 DB/schema scope 一個檔案；已排除 Git 與 Docker build context。格式／欄位語意變更時需更新 `history_cache.VERSION`。需保留 Railway 既有 artifact volume；沒有 volume 只會增加重建次數，不能影響輸入正確性。

## 驗證與部署狀態

- 離線測試：707 passed；連線指向無效本機端點，確認不存取正式 DB。
- 隔離 DB 回歸：28 passed、4 個非 DB 案例 deselected，約 309 秒；涵蓋新快取／查詢回歸、既有定價／注碼、決策物化／冪等／已下注、預測端到端／傷病 refresh。只用隨機測試 schema，未在 public seed / truncate。
- 正式唯讀量測：歷史指紋回應約 177 bytes、約 3.2 秒（含網路單次讀值）；當時未來 48 小時無候選比賽，因此沒有將空集合當成有賽事的節流證據。
- 另以最近一輪已保存定價的 1 場比賽、當輪時間為界，僅在 DB 內計數：歷史盤口 76 列 → 最新 series 27 列；歷史定價 272 列 → 對應最新盤口至多 114 列（尚未加有效預測篩選）；歷史注碼 1,308 列 → 每個所選定價至多 1 個 provenance。這是單一資料樣本的列數比較，不是流量或月費實測。
- **目前僅本機修正，尚未部署。** 未修改 schema、模型版本、風控參數或正式業務資料。

上線後應檢查自然預測工作的 `[history-cache] hit/miss`、既有定價／注碼／決策／預測心跳、Supabase 每日 Egress 與 DB 成長；不能將查詢列數縮減直接宣稱為整月低於 5 GB。冷讀、新賽果、傷病、重訓與網站仍會使用流量，內容指紋亦消耗 DB CPU。

## 維持免費的恢復邊界

- Dashboard 提供等待 11/15 下期重置或升級立即解除本期流量限制；使用者已排除升級。減少未來流量不會抵銷已累積的 85.983 GB，不保證立刻解除 402。
- 等待重置仍需處理容量：目前已超過免費 DB 大小，且快照資料會繼續成長。完整免費運作尚未證實。
- 下一步容量方案需先做完整可還原備份、找出歷史載入與外鍵／稽核相依、驗證冷資料可讀，再提出具體歸檔與保留界線供授權。不能直接刪舊傷病、定價、注碼或決策資料以湊額度；搬移與任何正式刪除／migration 都尚未執行。
- 其他專案共用組織額度；先降低 NBA 的背景流量，並分別檢查各專案，不應把故障都歸因於個別網站程式。
