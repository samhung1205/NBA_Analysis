# 傷病排版、來源狀態與免 HAR 匯入評估（2026-10-28）

範圍：修正傷病名單排版、唯讀診斷正式來源狀態、改善狀態頁判讀；依使用者後續選擇，台彩免 HAR 功能本次先評估。未部署、未套用 migration、未修改正式資料、模型或策略。

後續：曾先完成 API [授權詢問稿](twsport-api-authorization-request.md)與串接準備；使用者其後確認免費是必要條件，不推進付費 API，選擇 Chrome 同步並授權實作。已完成本機實作，見[同步指南](browser-sync-guide.md)。下文保留選案前的評估，不代表新來源已正式啟用或已取得資料使用許可。

## 傷病姓名

姓名與「主力」使用不換行的 inline-flex，標籤不縮小；隊伍卡片允許縮至可用寬度，超出內容在名單區內捲動。保留完整姓名、傷病狀態、原因及時間，捲動區可鍵盤聚焦並有可存取名稱。局部捲軸沿用深色介面。

## 六筆提示的唯讀診斷

2026-10-28 台灣約 18:38，以正式 Postgres 的 READ ONLY transaction 檢查 `data_sources`、migrations 與既有 readiness。當時核心 decision / pricing / sizing / paper / prediction 心跳正常；36 小時內 12 場全部有 ml-v2 預測，0001–0012 migrations 皆有套用紀錄。這是當次資料庫觀測，不證明遠端容器 volume、網站部署版本或所有第三方來源現時可用。

| 來源 | 當時狀態 | 影響與處理 |
|---|---|---|
| `balldontlie` | 8 月舊心跳，現行排程未使用 | 舊來源紀錄；不需為了消除紅燈重新啟用或刪除 |
| `bbref` | 7 月舊心跳，現行排程未使用 | 同上；歷史數據來源保留供追溯 |
| `espn` | 10/1 留下「1 天失敗」；其他來源較晚成功且新鮮 | 備援待命，保留警告；尚未重新實測 ESPN，不能宣稱它現在正常 |
| `nba_referees` | 當天台灣 10:37 成功，18:38 已隔 8 小時 | 排程只在 00–10、20–23 時的 :07/:37 執行；下次 20:07。未接入模型，不影響目前預測 |
| `nbainjuries_backfill` | 10/2 歷史回填留下「2 份失敗」 | 與即時 `nbainjuries` 分開；會影響歷史研究完整度，重做相關研究時應檢查失敗報告，不重寫為成功 |
| `twsport` | `warn / not_configured`，正式台彩快照 0 筆 | 實際影響台彩決策，不能產生可操作額度；需取得有效台彩盤口 |

原狀態頁將所有 `warn` 一律標成「資料過期」，還固定聲稱「目前為 seed 資料」，是本次修正的誤導來源。現在 API 附上來源角色、狀態、說明與處理方向；整體狀態只彙總 operational 來源，歷史警告另列。原始 `last_status`、`last_error`、成功／執行時間與依間隔計算的 `stale` 均保留，不改 DB 心跳。

裁判分類依實際時段判定，不把整個休息日無條件視為正常；前一日舊心跳、漏掉當日早上輪詢、晚間未恢復或來源回報錯誤，仍告警。`src/lib/source-health.ts` 的裁判時段必須與 `pipeline/core/scheduling.py` 同步維護。此判讀只供監控，不放寬 quote freshness 或決策額度有效性。

## 不手動匯出 HAR 的可行性

結論：單靠網站按鈕無法保證繞開既有資料取得限制；有瀏覽器輔助或資料 API 兩條可研究路徑，但目前沒有經驗證、已授權的免 HAR 實作。未新增空的匯入按鈕。

### 一般瀏覽器輔助

可設計桌面 Chrome 擴充套件，接收決策中心的明確同步要求，於使用者正常開啟台彩頁面時取得盤口 JSON，再送至經驗證的匯入端點。Chrome 官方提供 [網頁與擴充套件通訊](https://developer.chrome.com/docs/extensions/develop/concepts/messaging#external-webpage)、[指定頁面／frame 的腳本與權限](https://developer.chrome.com/docs/extensions/reference/api/scripting)，以及 [DevTools 網路回應讀取](https://developer.chrome.com/docs/extensions/reference/api/devtools/network)。後者仍需要開啟 DevTools，因此不能把 DevTools 方案當成完全無操作的一鍵同步。

這是技術可行性推論，尚未在台彩驗證。應先取得資料使用許可；不破解 challenge、重用別人的 session、偽裝或繞過封鎖。公開會員條款要求本人使用帳戶與保護憑證，沒有明確提供本專案自動擷取、儲存及再利用盤口的授權，不能由「正常瀏覽器看得到」推導法律許可。參見[台彩線上通路使用條款](https://article.sportslottery.com.tw/zh-tw/guide/112)。

實作前需確認：允許讀取的官方頁面、免登入盤口是否完整、全場／上半場資料覆蓋、擴充套件 host 權限、僅允許自家網站要求同步、登入驗證與短期匯入憑證。只傳盤口必要欄位，排除 cookie、Authorization、會員／投注／帳戶資訊。保留每筆回應實際觀測時間，不以按鈕或匯入時間冒充新盤口；缺邊、封盤、過期、部分成功與網路失敗皆須明確呈現。桌面 Chrome 與一般手機瀏覽器支援不同，不能承諾手機同樣一鍵。

### 第三方台彩資料 API

查到 [JBot 台彩賠率 API 文件](https://sportsbot.tech/api/sportslottery/) 宣告支援 NBA 台彩賠率，其文件稱約每 10–20 分鐘更新。[API 方案與條款](https://sportsbot.tech/api/)列訂閱每月 1,000 元、每日 300 次，呼叫間隔大於 5 秒；免費試用不允許定期排程。這些僅是供應商公開說明，未購買、未呼叫、未驗證服務存續與目前授權。

此路徑可讓雲端排程與決策中心按鈕共用 Python 擷取工作，較接近跨裝置的一鍵同步；但要先確認供應商有權提供本用途、資料儲存／展示權限、上半場三向獨贏與 push／封盤 metadata、來源時區與原始觀測時間。缺少上述資訊的市場應維持 unsupported，不能以國際盤或不同結算方式替代台彩；供應商資料需保留獨立 provenance，不能無聲冒充官方直接擷取。

下一步可先確認台彩或供應商的資料許可與完整樣本，再選定方案及實作。這份評估沒有把技術可行性當成法律結論。

## 本機驗證

- `npm run test:status`：18 passed，含真正過期、警告、錯誤、備援降級、歷史警告、裁判休息／恢復／漏跑、原始資訊保留及空來源狀態。
- `npm run build`：通過。
- 隔離 `/private/tmp/nba-fixes-20261028/` 的 D1 API smoke：162 passed／0 failed，沒有正式 `DATABASE_URL`。
- 傷病頁以長姓名測試 fixture 驗證 1280px 桌面及 390px 手機：姓名＋主力為單行，手機名單局部捲動，頁面無橫向溢出；狀態頁 390px 無橫向溢出。檢查頁面無 JavaScript runtime error。
- 設計檢查僅提示既有全站 `Inter` 字型，屬原有視覺系統，本次保留。

未驗證：正式網站部署後畫面、免 HAR 真實盤口匯入、第三方 API／擴充套件權限與授權、晚間裁判排程實際恢復。正式環境觀測只在上述時間點成立。
