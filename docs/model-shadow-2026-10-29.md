# 獨立自適應影子實驗與網站前瞻績效

2026-10-29：使用者已明確授權正式部署與啟用。**0014 已於台灣 17:25 以單一交易正式套用；網站與 Railway 已部署成功，影子實驗於 17:28 正式啟用。** 原始 68 場檢查保留於 [模型檢查報告](model-review-2026-10-29.md)。啟用時間來自收集器實際寫入的紀錄。

## 行為與證據邊界

- 正式 `ml-v2.0` / `dist-v2`、每週重訓、定價與 `risk-v1` 不變。獨立 `adaptive-shadow-v1` 不寫 `predictions`、盤口、注單或額度表。
- 新增 Python 計算並物化的網站報告，分開呈現最近 14 天的實際賽前預測、影子實驗、歷史回測與個人損益。API / JS 只讀取與顯示，不另算模型指標或風控。
- 正式啟用時間由 DB 實際時鐘記錄；不以 10/29 的文件日期假造啟用時間。已檢查的 68 場不會被補成影子樣本，也不供新校正的暖機訓練。
- 每場在 T-75～T-60 第一次觀測到有效 final 預測時保存同一筆 baseline 與其 candidate。這是影子擷取時點的成對比較，未必等於日後主報告選到的最後一筆 T-60 預測。超過 T-60 一律不補算。
- 保存原始 prediction ID、artifact/profile、當時點預測與區間、候選輸出、校正狀態 ID、DB 擷取時間；載入相同 artifact 重用既有機率與區間函式，不以四捨五入的摘要猜分佈。
- 賽果只有在實際觀測後才可進入訓練；保存 `observed_at`。比分更正、撤回或改期另新增觀測紀錄，過去校正狀態不變。改期配對列為失效，不沿用舊賽程評分。

## 固定規則

規則存於 `pipeline/core/production/shadow.py::SPEC`，首次啟用以同一份 JSON 保存。日後程式設定與已登記規則不一致會拒絕執行；改規則必須建立新實驗版本。

| 項目 | adaptive-shadow-v1 |
|---|---|
| baseline | ml-v2.0 / dist-v2 的實際 final profile |
| 校正目標 | 全場分差、全場總分、半場分差、半場總分，分別收縮平均偏差 |
| 平均偏差 | 已觀測賽果 − 當時 baseline 點預測 |
| 先驗 | 200 場零偏差；位移 = sum(residual) / (n + 200) |
| 上下限 | 各目標 −2 至 +2 分；不改分佈尺度 |
| 暖機 | 各目標至少 30 場、7 個 ET 比賽日；不足時位移為 0 |
| 訓練窗 | 最近 90 天內、截至擬合前已實際觀測的影子配對結果；缺半場資料只排除相應目標 |
| 更新 | 啟用時零位移；其後每週一台灣 16:10 起，收集器第一次執行時擬合。延遲執行以實際時間為準，不回填週一時間 |
| 勝率 | 網站 logistic 勝率保持正式值；分差衍生主勝機率另用相同 artifact 計算並評估 |
| 主指標 | 實際有套用校正的未來配對，總分 MAE 的成對差 |
| 次要診斷 | 其他 MAE、偏差、80%/95% 區間覆蓋，以及分差衍生主勝機率的 Log loss / Brier 成對差 |
| 開始人工檢視 | 啟用滿 42 天，且至少 300 場非零校正配對、28 個 ET 比賽日 |
| 自動 promotion | 無 |

這些是保守的實驗設計選擇，沒有用原 68 場尋找最佳參數。300 場 / 42 天是最低人工檢視條件，**不是統計檢定力保證或上線標準**；微小改善可能需要更多樣本。成對差以 ET 比賽日區塊 bootstrap 2,000 次提供診斷區間；負的 MAE 差代表誤差較小，不代表投注獲利。零位移樣本另計，不算校正有效的證據。

第一次達到檢視條件時，按已指定的主指標與穩定性人工審查；不因某次短期分箱或 ROI 較好就換指標或切換模型。若需修改參數，另立版本及新的前瞻驗證期。實際模型 promotion 仍需另外決定。

## 儲存與排程

0014 在 Postgres 與 D1 新增同名資料契約：

- `model_shadow_experiments`：啟用時間與規則。
- `model_shadow_states`：每期固定校正量、訓練結果觀測 ID、有效時間。
- `model_shadow_predictions`：每場一組原始 / 候選預測及狀態；實驗版本 × game 唯一。
- `model_shadow_results`：追加式賽果觀測及更正。
- `model_evaluation_reports`：可更新的公開彙總快取，不含帳號、真實注單或資金。

前四張表以 trigger 禁止 UPDATE / DELETE，觀測更正追加新列；所有新表啟用 RLS，撤銷 PUBLIC / anon / authenticated 權限。收集器以 transaction advisory lock 與唯一鍵防重複；報告更新防止較舊的執行覆蓋較新的快取。正式資料不會因測試被清除。

新增兩個獨立排程，來源定義共 21 個 jobs：

- `model_shadow` 每分鐘 :20，只有 `MODEL_SHADOW_ENABLED=true` 才執行；啟動時不補跑。關閉旗標停止收集並保留 ledger。
- `model_monitor` 每小時 :14 / :44，預設開啟；缺少 0014 時回報等待。Python 計算彙總後寫入報告表。

`GET /api/metrics/prospective` 只讀兩種已允許的彙總 key。缺 migration / 尚無報告顯示等待；超過 90 分鐘或無效更新時間顯示過期；真正 DB 錯誤維持錯誤，不偽裝成空資料。網站不會將未啟用、空樣本或零位移顯示成模型改善。

## 正式啟用順序（已完成）

1. 在正式庫確認既有 migrations，單一交易只套用 `0014_model_observability.sql` 並記錄 `schema_migrations`；確認 RLS、API 權限與新函式權限。
2. 部署包含新排程的 Python 程式至現有 Railway 服務；設定 `MODEL_MONITOR_ENABLED=true`、`MODEL_SHADOW_ENABLED=true`。啟用時間是收集器實際建立實驗的時間。
3. 部署網站，確認 `/api/metrics/prospective` 與 `/performance`；也可先部署網站，缺表時會顯示等待。
4. 檢查 `model_shadow` / `model_monitor` 心跳與報告更新時間；等待下一場真實賽事進入窗口。沒有真實新樣本時，記錄等待，不能灌假賽事來取得通過。

手動指令同樣會寫入指定 DB，不能將本機執行當作隔離測試：

```bash
cd pipeline
.venv/bin/python run_model_shadow.py --activate --collect --materialize
# 後續手動收集既有實驗，不重設啟用時間：
.venv/bin/python run_model_shadow.py --collect --materialize
# 既有唯讀檢查仍保留：
.venv/bin/python run_model_monitor.py --days 14
```

正式實驗由 Railway 收集器建立，沒有回填歷史影子配對。啟動時第一份報告稍早於實驗建立，另以與 Railway 一致的 `MODEL_SHADOW_ENABLED=true` 執行一次 `--materialize`，立即更新網站為已啟用；沒有手動製造預測、賽果或下注。模型 CURRENT 沿用，沒有發送額外外部訊息。

## 驗證

- Python 純邏輯、既有模型監測及排程測試：33 passed；排程效率回歸 25 passed。
- Postgres：影子流程 6 項（含真實併發）與既有 Data API 安全 5 項合計 11 passed（隨機 schema，清理完成）。
- API / D1 記憶體 SQLite：7 passed；涵蓋等待、資料原樣傳遞、過期 / 無效時間、損壞 JSON、允許的報告 key、不可修改 ledger 與缺表。
- `npm run build` 成功；這不是 TypeScript 型別檢查。
- 真實瀏覽器使用獨立記憶體 SQLite 與正式打包程式，檢查桌面、390px 手機、表格局部捲動、過期及等待狀態；runtime error 0。預覽顯示的啟用狀態為測試 fixture，**不是正式啟用證據**。
- Impeccable 機械檢查無 findings。

2026-10-29 發布前再驗證：Python 非 DB 58 passed、API/D1 7 passed、網站建置成功。部署後補強賽果觀測，將 1,000 場未變動結果的資料庫讀取固定為兩次，避免每分鐘逐場往返；新增離線回歸並重跑六項隔離 DB 案例。

## 正式啟用證據

- 0014 正式檔案 SHA-256：`67d9cad9f00d9cb97ed26ea7787f49c5a98328018c0f398d43865c536d6e2eb3`；提交後全新唯讀連線驗證五張表 RLS、API 角色無權限、四個唯增 trigger、固定函式 search_path。
- 全庫安全稽核：**PASS，41 張表、1 個 view、8 次 API 角色讀取全遭拒絕，problems=[]**。
- 首次部署版本：`d878f0d`。Cloudflare Pages `2ee3c658-4acd-4658-b65a-11620544db8e`；Railway `525cf2f3-8f3d-4a35-beb7-2d3990630794` SUCCESS。
- `adaptive-shadow-v1` 的 DB 啟用時間：**2026-10-29 09:28:21.212825 UTC = 台灣 17:28:21**。初始四項校正皆為 0。
- `model_shadow` 與 `model_monitor` 心跳為 ok；啟動日誌確認兩個新 job 已註冊，並保留持久化模型 artifact。正式預測仍使用 `ml-v2.0+20261026T0800Z.20261026T080025Z`。
- 正式瀏覽器已確認 [績效頁](https://webapp-e3e.pages.dev/performance) 載入 68 場前瞻成績、實驗「累積樣本中」、17:28 啟用時間與 0 場配對；runtime error 0，報告更新至 17:31。未登入時個人損益仍需登入。
- 一般 Python urllib HTTP 檢查收到 403；正式網站及其新 API 已由一般瀏覽器實際載入驗證，沒有將 urllib 檢查列為通過。
- 當下下一場正式賽事為台灣 **10/30 08:00**，第一次擷取窗口 **06:45–07:00**。目前 0 場為正常等待，不是完成前瞻效果驗收。

尚未驗證：真實未來比賽影子配對、非零校正的預測改善，以及台彩策略 ROI。
