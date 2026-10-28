# 免費 Chrome 台彩同步（browser-sync-v1）

2026-10-28：使用者已授權正式啟用；0013 已以單一交易套用並確認 RLS／API 權限，網站與排程器已正式部署並啟用。正式入口為 `https://webapp-e3e.pages.dev`。沒有新增資料 API 訂閱費；現有雲端服務仍有用量。真實台彩讀取與全流程仍需使用者在一般桌面 Chrome 安裝助手後實測，不能以 fixture 測試代替。

後續：使用者已安裝助手並嘗試真實同步，發現正式上傳 500；已確認連線中斷並修正，詳見[上傳修復紀錄](browser-sync-upload-fix-2026-10-28.md)。下方原始啟用紀錄保留當時狀態。

## 使用者只需完成這幾步

1. 到 [同步助手設定頁](https://webapp-e3e.pages.dev/static/twsport-sync-help.html)下載 ZIP，解壓縮成 `NBA-Twsport-Sync` 資料夾並保留它。
2. 桌面 Chrome 網址列輸入 `chrome://extensions`，開啟「開發人員模式」→「載入未封裝項目」→ 選該資料夾。
3. 確認助手 ID 為 `kijmgoejmindnnhhkbpognmpciahnana`，只授予台彩兩個官方網域權限。助手未上架商店，請使用者自行審閱並安裝。
4. 用相同 Chrome 設定檔正常開啟台彩 NBA 頁，登入本平台並重新整理決策中心，選日期、按同步。

安裝、登入及第一次真實擷取由使用者完成；不需申請資料 API key 或設定環境變數。首次若顯示阻擋／格式變更，停止並回報畫面錯誤，不輸出 HAR 或憑證。

## 日常操作

完成下方一次性設定後，用桌面 Chrome 正常開啟台彩 NBA 盤口，登入本平台的決策中心，選擇台灣比賽日期，按「同步台彩盤口」。不必匯出 HAR 或在本機啟動 Python。處理完成會顯示市場數、場次與實際擷取時間，並重新讀取決策表。

如果台彩尚未開啟，助手只會開啟一般台彩頁面；正常載入 NBA 後，回到決策中心再試。沒有可用盤、缺少助手、Python 未連線、擷取受阻、格式改變與匯入不完整都會分別顯示。403、429、challenge 立即停止，不重試或繞過。手機不提供此流程，HAR 保留作備援。

一次只接受全站一個工作，兩次建立工作至少間隔 30 秒。工作有效 5 分鐘；頁面重整會找回原工作結果，不重新上傳。關閉頁面前應等待結果；已排入 Python 的工作不因關閉頁面而取消。失敗或逾時可能已處理部分市場，查看結果與來源狀態後再重試。

## 維運設定（管理者參考）

1. 確認目前網站的**確切 origin**，例如 `https://你的網站`，不含路徑。打包：

   ```bash
   node scripts/build-twsport-extension.mjs --site https://你的網站
   ```

   預設輸出至被 Git 忽略的 `.browser-extension/`。同一資料夾重新打包會沿用公開 manifest key；全新資料夾則讀取版本控制內的 `extensions/twsport-sync/manifest-key.txt`，維持正式擴充套件 ID。保存此資料夾；換網站或 ID 都必須重新核對兩側設定。輸出的 `TWSPORT_EXTENSION_ID` 是公開識別碼，不是密鑰。不要把不同環境的套件混用。

2. 在 Chrome 的擴充功能頁開啟開發人員模式，選「載入未封裝項目」，選擇輸出資料夾。確認權限只有 `scripting`、`webNavigation`，與台彩的兩個網域；沒有 cookies、debugger、帳戶或全網站權限。安裝需由使用者審閱並完成。

3. 核對目標 DB 與 migration 紀錄，正式環境只套用尚缺的 `migrations/postgres/0013_browser_sync.sql`，連同紀錄在單一交易提交。本次已依使用者明確授權執行，既有業務資料未變動。新表啟用 RLS，撤銷 PUBLIC／anon／authenticated 存取；沿用後端直接連 Postgres。

4. 部署 Python 與網站程式，再設定：

   | 環境 | 設定 |
   |---|---|
   | Cloudflare 網站 | `TWSPORT_SYNC_ENABLED=true`、打包輸出的 `TWSPORT_EXTENSION_ID` |
   | Railway Python | `TWSPORT_SYNC_ENABLED=true`、保留 `TWSPORT_ENABLED=false` |

   `TWSPORT_SYNC_ENABLED` 預設停用；Python 設定為 true 時，原有台彩自動擷取 job 會略過，不覆蓋實際匯入心跳。新增第 19 個排程 `browser_sync` 每 15 秒檢查 DB 佇列並更新處理器心跳，不輪詢台彩。網站只有在處理器最近兩分鐘健康時才接受新工作。

5. 重新整理決策中心，確認助手連線、處理器健康，正常開啟台彩後執行第一筆。核對真實對戰、所選台灣日期、六種市場（來源實際有提供者）、擷取時間、匯入結果及決策更新。沒有正 EV、預測或有效資金時，成功匯入仍可沒有可操作機會；不可造資料取得綠燈。

本機 D1 只能驗證網站佇列與 UI，Python 無法消費 D1 工作。完整流程必須共用 Postgres，測試時使用隨機隔離 schema。

## 資料與處理契約

- Chrome 在已正常開啟的台彩 sportsbook frame 內呼叫同源資料端點；最多 50 次請求，每次至少間隔 1.25 秒，15 秒逾時，整體約兩分鐘上限。只處理今日至後日所選單一日期、未開賽 NBA 正規賽，最多 20 場。
- 只傳必要賽事與市場白名單欄位，不讀取或傳送 cookie、Authorization、登入、帳戶、下注紀錄、HAR 或整頁 HTML。瀏覽器同源請求沿用網站正常的 session，不抽取憑證。每個 event detail 回應完成時間分別保留，伺服器另記接收時間。
- 網站要求有效登入、相同 Origin、JSON、2 MiB 上限，先建立冪等工作，再接受一次 capture。其他使用者不能讀取或上傳該工作。全站單一 active 工作由 DB unique index 保護；Python 另有 schema 範圍的 advisory lock 與交易領取。
- Python 再驗證白名單、時間、日期、重複賽事與合法市場，沿用台彩 parser、對戰匹配、快照冪等及過期保護。每場使用自己的 capture time，新增快照保留 job ID、擷取／接收時間。未變動報價仍沿用既有 last-seen 契約，工作與抓取紀錄保留稽核。
- 接續執行既有 Python 定價、sizing 與該使用者的決策物化。部分匹配失敗或決策未完成時顯示 partial，不宣稱額度最新。未改模型、risk-v1、下注或結算策略；不回填歷史 paper 證據、不自動下注。
- inbox payload 以有上限的 gzip envelope 儲存，Python 向下相容讀取舊 JSON；完成、失敗或逾時清除；保留工作狀態、計數、時間、run IDs 與安全錯誤碼。正常盤口快照依既有稽核規則保存。

## 使用範圍與停用

免費瀏覽不是資料轉載授權。先前[免費方案評估](twsport-api-integration-plan.md)記錄了台彩權利聲明與待確認事項；本實作不表示已取得供應商授權。啟用後 `/api/games*`、`/api/odds/*` 與 `/api/sizing` 須登入，避免透過既有匿名介面公開盤口。此專案預設供個人帳號使用；多位登入者仍會共用底層盤口，不能把它當多人資料授權系統。

要停止同步，先將網站 `TWSPORT_SYNC_ENABLED=false` 阻止新工作，再停用 Python 同名設定並移除 Chrome 助手。保留收據與快照。網站會依處理器曾經留下的 `data_sources.browser_sync` 心跳持續要求盤口 API 登入，停用功能不會自動重新公開盤口；不要刪除這筆心跳、刪除表、停用 RLS 或開放 Data API 作為回滾。

Chrome 官方能力參考：[指定 frame 執行](https://developer.chrome.com/docs/extensions/reference/api/scripting)、[網頁至助手通訊](https://developer.chrome.com/docs/extensions/develop/concepts/messaging)、[載入未封裝項目](https://developer.chrome.com/docs/extensions/get-started/tutorial/hello-world#load-unpacked)。這些文件證明平台能力，不證明台彩讀取已成功。

## 驗證入口

```bash
npm run build
npm run test:twsport-sync
cd pipeline
.venv/bin/python -m pytest tests/test_browser_sync.py tests/test_readiness.py -m 'not db' -q
.venv/bin/python -m pytest tests/test_browser_sync.py -m db -q
```

DB 測試先核對 `tests/conftest.py` 的隨機 schema／search_path 隔離。沒有可連 DB 而 skipped 不算通過。上述 collector 與 extension 測試使用公開 fixture／mock；真實 Chrome 安裝、台彩讀取、正式端到端仍待驗證。

2026-10-28 本機實測：

- `npm run build` 通過；同步契約／collector／extension／前端流程 21 passed。
- browser sync、排程、readiness 純邏輯 76 passed；既有台彩 parser／odds 排程／store 純邏輯 25 passed。
- 隨機 Postgres schema 4 passed：實際匯入六種市場並更新決策、錯誤 payload、逾時、兩個 worker 只領取一次；測後 schema 清理完成。
- 隔離 D1 API smoke 162 passed／0 failed；auth 15、decision guard 19、來源狀態 18 passed。
- 決策中心 1280px 與 390px 無橫向溢出、無 runtime error；未登入、功能未啟用、未安裝助手與設定說明入口已檢查。缺助手時按鈕保持停用，未以測試資料冒充真實匯入成功。
- 已為 `http://127.0.0.1:3187` 打包本機測試套件（未安裝）；該項為前一階段的本機驗證。後續已另為正式 origin 打包並提供 ZIP；沒有對台彩發出測試請求。

正式套件：`public/static/downloads/nba-twsport-sync.zip`（v1.0.0），SHA-256 `7518967e809dbebf920c286238baff26965b47434316c42a8da9274867481006`。只含 manifest、background、collector、公開 origin／schema，沒有密鑰、cookie 或 HAR。正式 0013 SHA-256：`80727193b89b62d8b7613e85130b8ae1e068570d7f02ed30be532f42e78fcafd`。

## 正式啟用紀錄（2026-10-28 20:46 TPE）

- 程式 commit `987ea94907c4db60ca501dd1fd35e3f070724d03` 已推送 main。
- Cloudflare production 部署 `038acace-066e-45f6-b63a-af9851d5c6b3` 成功，網站旗標 true、ID 與發布 ZIP 一致；保留 DATABASE_URL／SESSION_SECRET。
- Railway production 部署 `2fa84597-6a9a-4c69-8b5e-00c37f8c2859` SUCCESS；`TWSPORT_SYNC_ENABLED=true`、原自動爬蟲 `TWSPORT_ENABLED=false`。啟動日誌確認註冊 browser_sync 並沿用持久模型 artifact。
- 正式 DB 0013 已提交，新連線確認成功；完整唯讀權限檢查 PASS：36 表、1 view、8 次角色讀取遭拒，problems=[]。
- 部署後唯讀心跳：browser_sync／decision_board／market_pricing／bet_sizing 均 ok，browser_sync 檢查時 9 秒內；佇列 0、台彩快照 0、bets／bet_events 0、bankroll_ledger 1。沒有為驗收寫入假盤口或測試投注。
- 一般瀏覽器實際開啟正式安裝頁、healthz（ok:true）；匿名同步與賽事盤口端點均回傳 unauthorized。正式 ZIP 已經瀏覽器下載，SHA-256 與發布檔一致，已解壓縮至本機 Downloads/NBA-Twsport-Sync。
- 終端 urllib 的公開頁探測收到 403，一般瀏覽器可正常使用；未繞過任何防護，未據此宣稱終端 HTTP smoke 通過。未在正式站執行會寫入的 API smoke。

仍需使用者手動：桌面 Chrome 載入解壓縮資料夾、使用同一設定檔正常載入台彩 NBA、登入本平台，按一次同步。尚未安裝／授權助手，也沒有對台彩執行真實擷取；因此首次真實盤口的內容覆蓋、來源可讀性與端到端收據仍未驗證。遇到阻擋或格式改變會停止，不能保證本次部署已解決來源限制。
