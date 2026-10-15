# 開發踩坑紀錄（Development Pitfalls）

> 由原 `README.md` 的「踩坑記錄」與環境備註整理而來（2026-10-15 移出）。遇到類似症狀時先查這裡。

## 1. Supabase Transaction pooler 在 Cloudflare Workers 間歇卡死（2026-08-01）

**症狀**

```
GET /api/games/tomorrow  → 200 OK (2s)
GET /api/games/today     → 500，僅 10ms 就被判定「Worker's code had hung」
```

**根因（兩層疊加）**

1. `src/db/index.ts` 原本把 pg 連線做成跨請求共用的模組級單例。Cloudflare Workers 禁止一個請求沿用另一個請求開啟的 socket I/O，被 runtime 判定為掛起。→ 改為**每個請求建立獨立連線**（`max: 1`，不快取）。
2. **Transaction pooler（port 6543）** 與 `postgres.js` 的連線管線化（pipelining）假設衝突，修正單例後仍間歇卡死。→ 改用 **Session pooler（同 host，port 5432）**，在 8 端點 × 5 輪並行壓測與 90 項 smoke test 下皆穩定。

**代價與未來方向**：每次請求多一次 TCP+TLS（約 150–300ms），自用可接受。若要對多人開放，建議改用 [Cloudflare Hyperdrive](https://developers.cloudflare.com/hyperdrive/)（需 `wrangler hyperdrive create`）。

**另外**：Supabase 直連位址 `db.<ref>.supabase.co` 只有 IPv6，多數家用 / 雲端網路連不上（`EHOSTUNREACH`），一律用 Session pooler URL。

## 2. `stats.nba.com` 對部分 IP 靜默丟棄請求

DNS/TCP/TLS 皆正常（~30ms），但 HTTP 零回應位元組（`ttfb=0`，IPv4/IPv6 皆同），而站台根路徑、`www.nba.com` 正常。屬 Akamai 封鎖，非程式問題：換 IP（雲端主機 / VPN / 熱點）或改用 NBA CDN / ESPN 備援。

排查：

```bash
curl -4 -m 15 -w "%{time_connect} %{time_appconnect} %{time_starttransfer}\n" -o /dev/null \
  -H "Referer: https://www.nba.com/" \
  "https://stats.nba.com/stats/scoreboardv3?GameDate=2025-02-01&LeagueID=00"
```

## 3. ESPN

- 不可偽造瀏覽器 UA（`Mozilla/5.0` 會 403），用 requests 預設 UA。
- scoreboard 只接受單日 `dates=YYYYMMDD`。

## 4. `nba_api` 標頭

- **不要**傳 `headers=` 或覆寫 User-Agent：`headers=` 會整個取代預設標頭，舊版自訂的 Chrome 120 UA 與套件預設 `Sec-Ch-Ua`（Chrome 145）矛盾，反而更容易被擋。一律沿用 `NBAStatsHTTP.headers`，也不要自行重建 `requests.Session`。
- `nba_api.live` 端點的預設標頭（Chrome 87）已過期，打 `cdn.nba.com` 會 403；CDN 請求改用 stats 預設標頭（去掉 Host），見 `pipeline/core/sources/nba_cdn.py`。

## 5. Supabase 免費專案閒置會被暫停

暫停後 pooler 回 `tenant/user not found`、直連主機 DNS 消失，需到 Supabase Dashboard 手動 **Restore**。排程器長時間停機後要先檢查這點。

## 6. Python 背景執行

背景執行請加 `-u`（或 `PYTHONUNBUFFERED=1`），否則輸出被緩衝，容易誤判為卡住。

## 7. APScheduler

- `add_job(..., next_run_time=None)` 是「建立成暫停」，不是「立即執行」。
- `CronTrigger` 不指定 `timezone` 會用機器本地時區。排程現集中在 `pipeline/core/scheduling.py`，每個 Cron 皆帶 `Asia/Taipei`，並有 next-run-time 測試。

## 8. `nbainjuries` 時間與格式

- timestamp 是**美東當地 naive 時間**（非 UTC）。
- 2025-12-22 起為 15 分鐘一份報告。
- 球員名稱格式為 `Last, First`。

## 9. macOS 跑 XGBoost 需要 OpenMP

研究用評估（C.5C 等）會載入 XGBoost，Mac 缺 `libomp.dylib` 會失敗：

```bash
brew install libomp
# 或暫時借用 scikit-learn 內建的：
export DYLD_FALLBACK_LIBRARY_PATH=pipeline/.venv/lib/python3.11/site-packages/sklearn/.dylibs
```

注意 `nohup` 會清掉 `DYLD_*` 變數。Docker 映像已加裝 `libgomp1`。現行 production 推論用 Logistic / Ridge，不依賴 XGBoost。

## 10. Cloudflare WebCrypto PBKDF2 迭代上限（2026-10）

Cloudflare Workers 的 PBKDF2 最多 **100,000** 次迭代，超過會丟 `NotSupportedError`（第一次以 210,000 次註冊時因此失敗）。現行做法：新雜湊用 100,000 次，並要求新註冊密碼至少 **12 字元**；儲存迭代數超過上限的舊雜湊會安全地登入失敗。免費版每請求 10ms CPU 仍可能讓登入出現 Error 1102，必要時升級 Workers Paid，**不要降低迭代數**。詳見 [production-runbook.md](production-runbook.md) §3。

## 11. 台灣運彩自動擷取被 Cloudflare 擋

自動化瀏覽器（headless 或一般視窗）呼叫台彩 JSON 端點皆 403。本專案**不繞過**防護，改用一般瀏覽器匯出 HAR 手動匯入（[production-runbook.md](production-runbook.md) §7）。
