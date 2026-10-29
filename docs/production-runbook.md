# Production runbook

Cloudflare Pages ⇄ Supabase Postgres ⇄ Railway scheduler. Phase D.1–D.5 are frozen (tag `phase-d5-complete`); this file only covers operating them.

```
 Cloudflare Pages (Hono web + API + auth)  ──reads/writes──▶  Supabase Postgres (Session pooler, port 5432)
                                                                      ▲
 Railway "nba-scheduler" (Python, 21 jobs, always on, game-aware — §4b) ────────────────┘
   └─ Volume /data/model-artifacts  (production ML artifact, survives redeploys)
 The Odds API (international odds — diagnostic only)     Taiwan Sports Lottery (actionable — user-initiated Chrome sync / HAR fallback)
```

## 1. Frozen rules (never change for operational reasons)

model / distribution decisions (C.5E) · `risk-v1` (1/4 Kelly, 2 % single bet, 3 % same game, 8 % day) · Taiwan odds are the actionable market, international odds are diagnostic only · no historical ROI claims; paper (prospective) vs actual bets stay separate · no recalibration from recent results.

**Never:** bypass Cloudflare / anti-bot on the lottery site · backfill historical odds and present them as as-of data · insert fake bets / bankroll rows into production · tune `risk-v1` from ROI (new policy version only) · paste or commit `DATABASE_URL`, API keys, `SESSION_SECRET`, HAR files.

## 2. Railway: manual setup (one time)

| Step | Value |
|---|---|
| New service | **nba-scheduler** → Deploy from GitHub repo `samhung1205/NBA_Analysis`, branch `main` |
| Root Directory | `/pipeline` (Railway auto-detects `pipeline/Dockerfile`) |
| Build / Start command | **leave empty** — the Dockerfile `ENTRYPOINT` runs `docker-entrypoint.sh` → `python scheduler.py` |
| Replicas | **1** (never scale out: duplicate jobs would double-spend Odds API credits) |
| Restart policy | Always (a crash must not leave the scheduler down) |
| Volume | create one, mount path **`/data/model-artifacts`** |
| Healthcheck | none (no HTTP port) |
| Usage | Workspace → Usage: set an email alert / Compute hard limit. The limit is shared by every project in the workspace; reaching it takes all workloads offline. Monitor service RAM, including Monday retraining peaks. [Cost control](https://docs.railway.com/pricing/cost-control) |

Variables (names only — enter values in the Railway UI). Everything the Python code reads:

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | Supabase **Session pooler** URI, port **5432** | required. Not 6543, not `db.<ref>.supabase.co` (IPv6 only) |
| `ODDS_API_KEY` | (secret) | without it the Odds API job reports `not_configured`; Taiwan path unaffected |
| `MODEL_ARTIFACT_DIR` | `/data/model-artifacts` | required on Railway, else retrains are lost on redeploy |
| `TWSPORT_ENABLED` | `false` | Railway has no Chrome and the site blocks automation; manual HAR instead |
| `ODDS_API_REGIONS` | `us` | default |
| `ODDS_API_INCLUDE_H1` | `false` | default (per-event calls cost extra credits) |
| `ODDS_API_QUOTA_RESERVE` | `25` | below this, paid endpoints stop (`quota_low`) |
| `ODDS_API_QUOTA_WARN` | `100` | below this, status page shows warn |
| `ODDS_API_CRON_HOURS` | `0,6,12,18` | Taipei time, :40 — 3 credits per run when games are in the 48 h window, 0 otherwise |
| `TZ` | `Asia/Taipei` | log timestamps only; all triggers carry an explicit timezone |

Optional (defaults are fine): `PG_POOL_MAX` (4; Supabase session pooler has ~15 shared slots with the website), `ODDS_API_BOOKMAKERS`, `BALLDONTLIE_API_KEY`, `NBA_API_MIN_DELAY`/`NBA_API_MAX_DELAY`, `ALERT_WEBHOOK_URL`. **Not needed by the scheduler:** `SESSION_SECRET` (website only), `LINE_CHANNEL_ACCESS_TOKEN` (not read by any code).

Telegram push (Phase E.1, optional — §4c): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (both required to enable), optional `TELEGRAM_NOTIFY_USER_ID`, `TELEGRAM_NOTIFY_INCLUDE_REVIEW`, `NOTIFY_SITE_URL`. Requires migration `0009_phase_e1_notifications.sql`.

### Post-deploy checks (Railway)

First boot logs (Deploy logs):

```
[entrypoint] MODEL_ARTIFACT_DIR=/data/model-artifacts
[entrypoint] No persistent production artifact found; bootstrapping...
[entrypoint] Bootstrap artifact copied.
... 已登錄 job：<id> — ...     (21 lines)
```

Registered jobs (from `core/scheduling.py`): `daily_schedule_scores`, `live_final_refresh`, `injuries_peak`, `injuries_offpeak`, `predict_early`, `predict_final`, `predict_injury_refresh`, `weekly_retrain`, `odds_twsport`, `odds_oddsapi`, `market_pricing` (pricing + sizing), `paper_strategy`, `decision_board`, `notify_actionable`, `notify_daily_summary`, `notify_system_alerts` (the three notify jobs return immediately unless Telegram is configured), `referee_assignments` (§4d), `roster_sync`, `browser_sync` (§4e).

1. **Stays alive** – no restart loop; log lines keep arriving every minute (`decision_board`).
2. **Heartbeats / DB writes** – from your laptop (reads `pipeline/.env`, read-only):
   ```bash
   cd pipeline && .venv/bin/python run_production_check.py --post-deploy
   ```
   Within ~10 minutes of deploy `decision_board`, `market_pricing`, `bet_sizing`, `paper_strategy` heartbeats must be PASS (before that, `--post-deploy` correctly FAILs).
3. **Artifact persistence test (harmless):** Railway → Deployments → Redeploy. The new boot log must say `Persistent production artifact already exists; keeping it.` and `run_production_check.py` must show the same artifact version as before.
4. **Odds API quota safety** – `oddsapi` line of the check shows remaining credits. The job does not run at startup (`startup_catchup=False`), so redeploys cost nothing; with no game inside 48 h it spends 0 credits.
5. **No duplicates** – exactly one replica; one `已登錄 job` block per boot. If two blocks appear in the same minute from different containers, scale down to 1 immediately.

Redeploy behaviour: Railway stops the old container, starts the new one, mounted volume untouched; the entrypoint only copies the bootstrap artifact when `CURRENT.json` is missing, so a retrained model is never overwritten by the image's older copy.

## 3. Cloudflare Pages: manual setup

Do **not** deploy before the account cleanup in §6 is done. `npm run deploy:prod` = `vite build` + `wrangler pages deploy dist --project-name webapp`: it uploads `dist/` and applies `wrangler.jsonc` as the production config (D1 is explicitly cleared for `production`/`preview`, so production cannot fall back to the sandbox DB). It does not touch secrets.

1. Pages project `webapp` (create in dashboard or on first deploy).
2. Secrets (Settings → Variables and Secrets, **Production**): `DATABASE_URL` (Session pooler, 5432), `SESSION_SECRET` (`openssl rand -base64 32`, ≥ 32 chars). The API now **fails closed** when `DATABASE_URL` is set but `SESSION_SECRET` is missing / weak / the dev default.
3. `ALLOW_REGISTRATION`: registration is closed on Postgres once any user exists. To create your real account, set `ALLOW_REGISTRATION=true`, register, then **delete the variable** and redeploy.
4. Verify without printing values: `npx wrangler pages secret list --project-name webapp` (names only).
5. `npm run build && npm run deploy:prod`, then check `/healthz`, `/api/system/status` (`db_driver` must be `postgres`), `/api/bets` without cookie → 401.
6. **Password hashing:** Cloudflare WebCrypto PBKDF2 currently caps iterations at **100 000** (a higher count throws `NotSupportedError`, which is what broke the first registration attempt at 210 000). New hashes therefore use the runtime maximum: PBKDF2-HMAC-SHA256, 100 000 iterations, 16-byte salt, 256-bit key, stored as `pbkdf2$<iterations>$<salt>$<hash>`. This is **not** equivalent to 210 000 iterations; it is compensated for by requiring **at least 12 characters** for new registrations (login never checks length, so existing passwords are unaffected). Verification reads the iteration count from the stored hash; a stored hash above the cap (e.g. an old 210 000 one), or any malformed hash, simply fails to log in (fail closed, logged server-side without hash/password) — the legacy demo/test accounts are in that state and are meant to be deleted. For a future multi-user / public deployment use managed authentication (e.g. Supabase Auth) instead of this home-grown scheme.
7. Free plan CPU limit is 10 ms/request; PBKDF2 at 100 000 iterations may still return Error 1102 on login. If so, use Workers Paid ($5/month). Do not lower the iteration count.

Auth model: `/api/decision-board`, `/api/bets*`, `/api/bankroll*` call `requireUser`; games / odds / sizing / predictions / system status are public by design (no personal data).

### 3a. Supabase Data API 安全邊界（2026-10-21）

網站使用自訂 session；Hono 與 Python 均以 `DATABASE_URL` 直接連 Postgres，不使用 Supabase Auth / REST / GraphQL。網站的 `requireUser` **不會保護** Supabase 自動產生的 Data API。2026-10-21 唯讀確認通知所指的正式庫：33 張表未啟用 RLS；`anon` / `authenticated` 對 33 張表與 `v_odds_quotes` 均有讀寫權限。這證實授權缺口存在，**不能據此判定是否曾遭他人讀取或修改**。

`migrations/postgres/0010_data_api_lockdown.sql` 是針對這個架構的修正：

- 對目前 schema 的所有資料表（含 `schema_migrations`）啟用 RLS，不新增對外開放 policy。
- 撤銷 `PUBLIC`、`anon`、`authenticated` 的表、個別欄位、序號與函式權限，並將 `v_odds_quotes` 改用 `security_invoker`，避免 view 以 owner 權限繞過 RLS。
- 撤銷 migration 執行者在該 schema 新建表／序號時自動授予 API 角色的權限。**新 migration 仍須明確啟用新表 RLS**；其他 creator 的 default privileges 要另外審查。
- 固定現有六個 trigger 函式的 `search_path=pg_catalog`，保留投注及帳本的不可變性檢查。

現行後端使用 `postgres`（`BYPASSRLS`），不受這些 RLS policy 限制；權限不可移交給瀏覽器。未來若改成其他 DB role，須先驗證直接 SQL 的權限／RLS，不能為恢復服務把資料開放給 `anon`。D1 沒有 Supabase Data API，不套用 0010。

正式套用前須獲授權、確認目標及既有 migration 狀態。使用同一個 migration 交易提交修正與版本紀錄，不重跑 seed，不修改或刪除業務資料。以當下檢查結果確認是否已套用：

```bash
cd pipeline
.venv/bin/python run_security_check.py
# PASS：所有表已啟用 RLS，API 角色無有效表／欄位／序號／函式權限，
# users / bets / bankroll_ledger / v_odds_quotes 的 8 次角色讀取測試皆被拒絕。
```

此命令為唯讀，僅輸出結構診斷；角色探測用 `LIMIT 0`，不傳回使用者資料。它驗證 Postgres 授權邊界，不代表已完成 HTTP Data API、Supabase Advisor 或存取紀錄的檢查。通知可能是過去的快照，修正後請重新執行 Dashboard Security Advisor。

防止意外重新開放：Dashboard → Integrations → Data API 可停用未使用的 Data API（[官方說明](https://supabase.com/docs/guides/api/securing-your-api#disable-the-data-api)）；此設定與 SQL migration 分開，是否停用須以 Dashboard 為準。新函式仍可能繼承 PostgreSQL 的全域 `PUBLIC EXECUTE` 預設，須在建立函式的同一交易明確 `REVOKE EXECUTE ... FROM PUBLIC, anon, authenticated`；0010 不修改整個資料庫的全域 default privileges 或其他 Supabase schema。

隔離回歸：`cd pipeline && .venv/bin/python -m pytest -q tests/test_data_api_security_db.py tests/test_decision_db.py tests/test_execution_db.py tests/test_notify_db.py`。fixture 只對隨機 schema 套用 migration；0010 使用 `current_schema()`，不可改成硬編碼 `public`。失敗時修正 migration／後端角色，**不要以停用 RLS 或重新開放 API 權限作為例行回滾**。

2026-10-21 修正驗證：隔離 schema 的初版資安測試與既有 decision / execution / notify 共 **33 passed**；補齊個別欄位授權案例與唯讀稽核工具後，最終資安專項 **5 passed**（包含修正前可讀、修正後拒絕、後端仍可讀寫、重跑與新表／序號 default privileges）。Python 語法檢查及 `git diff --check` 通過。正式庫修正前唯讀稽核為 **FAIL：33 張表未啟用 RLS、8 次角色讀取皆未遭拒絕**。

**正式套用完成（2026-10-21，經使用者明確授權）**：已確認通知中的專案與既有 0001–0009 紀錄，在單一交易內套用 0010、驗證通過並寫入 `schema_migrations`，提交後以全新唯讀連線再驗證 **PASS：33 張表、1 個 view、8 次角色讀取全部遭拒絕、problems=[]**。後端帳號對所有 33 張表的 SELECT / INSERT / UPDATE / DELETE 權限均保留，8 個表／view 的後端讀取探測通過；六個 trigger 函式 search path 已固定。`decision_board`、`market_pricing`、`bet_sizing`、`paper_strategy`、`notify_telegram` 最近心跳均為 `ok`（檢查時 0–4 分鐘內）。已發送 PostgREST schema reload 通知；未修改或刪除業務資料，也未部署網站／排程程式。

套用檔案 SHA-256：`34e2402143e7e37566f8ba7f212e9780799e6d3758128f61e3bc4b6eb397bc6e`。尚未驗證 HTTP Data API 與 Dashboard Advisor；本機未設定正式網站 URL，未執行網站 HTTP smoke。Postgres 權限修正已實測生效，但不能據此判定修正前是否曾有資料外洩。

## 4. Daily operations

- Look at `python run_production_check.py` (or the website status page) once a day.
- Taiwan odds: when you want fresh actionable odds, import a HAR (§7). No fresh HAR ⇒ pricing / sizing / decisions stay empty or stale — that is safe, not an error.
- Weekly: first Monday after deploy watch Railway memory during `weekly_retrain` (Mon 16:00 Taipei). A failed retrain leaves the current artifact in place.

## 4b. Scheduler efficiency / idle behavior (`scheduler-efficiency-v1`)

Railway stays **always on** (a 24/7 `BlockingScheduler`). What changed is that expensive work is *game-aware*: every high-frequency job first asks Postgres (indexed, tiny queries) whether there is anything to do, and returns immediately if not. An idle skip is a normal outcome — not an error, not a missing heartbeat. "Eligible game" is the project's existing definition: `status <> 'final'` and `season_stage ∈ {regular, playin, playoffs}` (preseason never wakes the pipeline).

| Job | Gate (idle ⇒ skip) | Unchanged on purpose |
|---|---|---|
| `injuries_peak` / `injuries_offpeak` | next eligible, not-yet-started game decides cadence (below) | ET report probing, report-time and as-of logic, history writes |
| `predict_final`, `predict_injury_refresh` | no eligible game in their window ⇒ artifact not even loaded | `predict_early` (daily, doubles as artifact-health + liveness check) |
| `market_pricing` (pricing + sizing) | no game in 48 h, or no odds snapshot yet ⇒ nothing to price/size. Pricing is also skipped when (games, max odds id, max prediction id) equals the last *successful* pricing run | **Sizing is not skipped while odds exist**: quote staleness (`stale_quote`) changes with time, so identical inputs at a later T can size differently |
| `paper_strategy` | no T-60 candidate (same `due_games` query) and no pending/ungradable paper bet ⇒ no-op | execution-v1, the >15 min `decision_window_missed` rule |
| `decision_board` | idle ⇒ every minute only **confirms** the latest snapshots (`last_confirmed_at`); full materialization at most every 30 min | the 10 min `MATERIALIZATION_STALE_AFTER` contract (see below) |
| `weekly_retrain` | no more eligible final games than the promoted artifact was trained on ⇒ `retrain_skipped_no_new_data` | gates, atomic write, promote, rollback (manual `run_retrain.py` always trains) |
| `odds_oddsapi` | none | 4 runs/day, free `/events` first, reserve/warn thresholds — a DB gate could miss games during schedule-ingestion lag for four free requests/day |
| `odds_twsport` | disabled on Railway ⇒ one heartbeat upsert per 30 min | no anti-bot workaround; manual HAR is the path |
| `daily_schedule_scores` | none | see below |
| `live_final_refresh` | already gated before this change (no live/unsettled game ⇒ no external call) | |

**Injury polling** (by time to the *nearest* eligible game that has not started; measured in UTC instants, so DST cannot distort it):

| Time to next game | Policy |
|---|---|
| > 36 h or no game | no injury polling |
| 12 h < t ≤ 36 h | at most once / 120 min |
| 3 h < t ≤ 12 h | at most once / 60 min |
| 0 < t ≤ 3 h | at most once / 15 min |
| game already started | that game alone no longer triggers pregame polling |

APScheduler still wakes every 15 / 30 min; the function decides. Cadence uses `data_sources.nbainjuries.last_attempt_at` (survives restarts), with a 2-minute grace so a run that finished a few seconds late does not wait a whole extra grid step. `expected_interval_min` follows the tier (NULL when idle), so the status page does not call an idle source stale.

**Why schedule sync stays daily in the off-season:** `daily_schedule_scores` (12:00 Taipei) is the mechanism that discovers when games become relevant again. Every gate above is driven by what is in `games`; if sync stopped, nothing would ever wake up.

**Why the decision board is not simply relaxed:** the UI marks a board `stale` when `last_confirmed_at` is older than 10 minutes, and a bet/bankroll change must show up immediately. So idle is narrow and verified each minute: no unfinished game around today/tomorrow, no open actual bet, no unsettled paper bet, and the state vector (users, bankroll `risk_state_version`, ledger / bet_events / bets ids, paper ledger ids, Taiwan source state, today's date) equals the one at the last full materialization (same process, < 30 min ago). Any change — a user recording, correcting or voiding a bet, a settlement, a new game, a HAR import, midnight — takes the full path (settlement → ledger sync → materialize). Process restart ⇒ first run is full.

**Reading the logs:** `[scheduler-efficiency] <job> skipped: <reason>` — reasons `idle_no_upcoming_game`, `outside_injury_window`, `cadence_not_due`, `no_game_in_window`, `no_odds_snapshots`, `no_fresh_inputs`, `no_candidate_or_open_bet`, `idle`, `no_new_training_data`. INFO is logged when the reason changes and at most hourly otherwise (with an `xN since last log` count); the rest is DEBUG. `run_production_check.py` shows next eligible game, hours to it, injury tier, and whether pricing / paper / decision / retrain are active or idle. No dollar estimate: read Railway → Metrics after ~24 h.

Rollback of the policy: `git revert <scheduler-efficiency commit>` and redeploy (restores the previous every-tick behavior; no data migration involved).

### 4b.1. 常駐 RAM：傷病 JVM / 重訓工作隔離（2026-10-30）

`core/job_process.py` 將兩個大型工作交給使用同一 Python 執行檔與環境的獨立行程；工作結束後由 OS 回收 JVM、Python heap 與原生套件配置。排程器持續執行，每分鐘決策／推播與每 15 秒 browser sync 的觸發時間沿用。

- 傷病：原本的 `injury_poll_job` 在父行程完成 next-game／cadence gate；只有真的需要抓取時才啟動子行程執行既有 `fetch_injuries_job`。傳遞原本的 UTC `now` 與 expected interval，ET 報告探測、解析、交易、冪等與心跳均沿用。JVM 不會被載入排程器。
- 重訓：子行程執行既有 `retrain_job`，保留無新資料略過、固定模型規格、artifact 檔案鎖、gates、原子寫入與 promotion 規則。排程註冊時不再 import 重訓模組。
- 子行程以 `sys.executable` 啟動，不經 shell、不緩衝 stdout/stderr；繼承 Railway 的環境變數及 `MODEL_ARTIFACT_DIR`，自己建立／關閉 DB pool，不共享父行程的 DB connection。沒有新增依賴或 migration。
- 傷病工作上限 10 分鐘、重訓上限 60 分鐘；超時會終止並回收子行程。非零退出／超時會由父行程記錄 `error` 心跳並報錯，不會退回父行程執行。檢查 `worker_failed` / `worker_timeout`，確認來源與執行時間後再調整上限。

Railway logs 的 `[memory-worker]` 會記錄工作前後的 **parent_rss_mib**、elapsed 秒數，以及子行程退出時的 **peak_rss_mib**。兩者分別是當下 RSS 與整個子行程生命週期峰值，不可混為一談。Linux 使用 `/proc/self/statm` 讀 RSS；無法讀取時顯示 `None`，不影響工作。

本機離線對照（macOS，僅初始化 JVM 並配置 128 MiB Java 測試陣列，**沒有抓真實 PDF、連 DB 或重訓模型**）：原行程完成後 RSS 164.8 → 408.6 MiB，JVM 留在排程器；隔離工作完成後 RSS 165.1 → 165.1 MiB，排程器未載入 JVM。這證明隔離能回收該工作配置，**不代表正式 RAM 或帳單會降低相同比例**。Python／NumPy 等其他排程仍常駐，這一版沒有改模型或降低資料時效。

驗證：`cd pipeline && DATABASE_URL=postgresql://invalid:invalid@127.0.0.1:1/none PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -m 'not db' -q` → **707 passed、174 deselected**；`tests/test_job_process_db.py tests/test_injuries.py tests/test_scheduler_efficiency.py -m db` → **19 passed、46 deselected**。真實子行程只連隨機測試 schema，HTTP/PDF 來源以合成資料替代；確認 UTC、冪等、心跳、無新資料 gate 與 artifact 未變。另驗證非零退出／超時後行程已回收並記錄錯誤，語法與 diff 檢查通過。

部署後以相同比賽時段觀察 24–48 小時：RAM 基線在傷病工作完成後應回落、子行程應正常退出、`nbainjuries` / `model_retrain` 與 `decision_board` 心跳持續健康。獨立工作會增加重新載入套件的 CPU 成本，須同時比較總用量；每週重訓的正式高峰仍需等下一次週一工作確認。尚未部署前不能宣稱線上節費。

## 4c. Telegram push (`notify-v1`, Phase E.1, optional)

Reads only what Python already materialized; computes no probability / EV / Kelly / capacity, and never places bets. Disabled (jobs return at once, no DB access, no heartbeat) until both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.

| Job | When (Taipei) | Sends |
|---|---|---|
| `notify_actionable` | every minute at :50 s (after `decision_board`) | new Taiwan **ACTIONABLE** opportunities from the user's latest decision snapshot, only if it was confirmed ≤ 10 min ago. One message per batch; each game × market × side × line × odds is sent once. `TELEGRAM_NOTIFY_INCLUDE_REVIEW=true` also sends **REVIEW** (qualified but data-quality flagged — early season most opportunities are REVIEW). International odds are never sent. |
| `notify_daily_summary` | 12:40 | tomorrow's (Taipei) eligible games with the latest prediction; one per day; nothing when there are no games |
| `notify_system_alerts` | :04 / :19 / :34 / :49 | `run_production_check --post-deploy` FAIL set: message when it changes (new / recovered), reminder every 6 h while unchanged; skipped for the first 15 min after a (re)start. If the DB itself is unreachable the alert is sent directly with in-process de-duplication. |

Setup: 1) `npm run db:migrate:pg` (applies 0009); 2) set the variables on Railway (§2); 3) from your laptop `cd pipeline && .venv/bin/python run_notify.py --test`. Inspect without sending: `run_notify.py --job actionable|daily|system --dry-run`.

Delivery: every message is claimed in `notification_events` (`dedup_key` UNIQUE) before sending, so a duplicate replica cannot double-send; a failed send is kept as `failed` and retried up to 3 times. Heartbeat: `data_sources.notify_telegram` (status page category 推播). The token is never logged or stored.

Limits: the scheduler cannot alert about itself being down (no process, no message) — use an external uptime monitor if needed. Multiple website users need `TELEGRAM_NOTIFY_USER_ID` (one chat ↔ one user).

## 4d. Referee assignments (`referees-v1`, Phase E.2 — data collection only)

Collects the official pre-game assignments (https://official.nba.com/referee-assignments/, published ≈ 09:00 ET) so a prospective as-of history exists. **Not used by `ml-v2.0`, pricing, sizing or any strategy.**

- Job `referee_assignments`: Taipei 20:07–10:37 every 30 min (= 08:07–21:37 ET in both EDT and EST); does nothing (no HTTP request) unless an eligible not-yet-started game tips off within 16 h.
- Storage: `referee_assignment_reports` (one row per distinct page content; same content only advances `last_seen_at`; a late swap adds a new row, old rows are never rewritten) and `referee_assignments` (per game, `game_id` matched by ET date + teams; unmatched rows keep `game_id NULL` + `match_status` and are re-matched after schedule sync). `observed_at` = response completion time, never the page date.
- As-of rule for future research: officials known at T = the latest report with `observed_at ≤ T`. Post-game box-score officials must never be written here.
- Failures (403 / changed page / missing officials) are recorded as heartbeat `error` on `data_sources.nba_referees`, nothing is stored, no retry / IP rotation. Empty table (not yet published) stores nothing. The page's date is the page's *current* ET date: before the new slate is published (≈ 09:00 ET) it still lists the previous slate labelled as today, so matching covers the page date and the day before; a fully previous-day match is `previous_slate` (normal). Idle runs also touch the heartbeat.
- Requires migration `0011_phase_e2_referees.sql` (new tables have RLS enabled and API roles revoked, per §3a). Manual: `cd pipeline && .venv/bin/python run_referees.py --dry-run`.

## 4e. Current rosters (`roster-sync-v1`, 2026-10-21)

`players.team_id` used to follow only the player's latest box score, so off-season moves stayed on the old team until the first game (e.g. Klay Thompson DAL → MIA, Giannis MIL → MIA). The 2026-10-21 read-only dry-run against production found **107 moved, 69 new (rookies / signings), 585 no longer on any roster**.

- Source: `stats.nba.com/stats/playerindex` (Active=1, NBA PERSON_ID → `nba_player_id`); falls back to ESPN team rosters (name-only: updates uniquely matched existing players, never adds or releases).
- An incomplete roster (missing team, < 13 players on a team, unknown abbr) is rejected as a whole and recorded as heartbeat `error` on `data_sources.nba_rosters`; nothing is written.
- Applied players get `roster_synced_at`; `refresh_player_current_team()` only overrides them after a later game. The injury "not listed → Available" row is no longer written on a team the player has left (per a newer roster sync).
- Requires migration `0012_player_roster_sync.sql` (adds one column, no new table). Manual: `cd pipeline && .venv/bin/python run_roster_sync.py --dry-run`, then without `--dry-run` to apply.
- Job `roster_sync`: daily Taipei 11:50 (= ET the previous night, after that day's transactions; before the 12:00 schedule sync and 12:20 early predictions). No startup catch-up (a restart does not hit stats.nba.com again). Heartbeat `data_sources.nba_rosters` expects 24 h; `warn` = some ESPN names unmatched, `error` = fetch failed / incomplete roster (nothing written).

**Applied to production (2026-10-22 TPE, explicitly authorized by the user):** `0012_player_roster_sync.sql` applied alone in one transaction together with its `schema_migrations` row (`0011` was deliberately left unapplied — still in development — so `npm run db:migrate:pg` was not used). `run_roster_sync.py` applied 107 moves / 69 additions / 585 releases (source `nba_playerindex`, 0 unmatched); a dry-run right after showed 0 changes, 519 players on teams, heartbeat `nba_rosters` = `ok`. Klay Thompson and Giannis Antetokounmpo are now MIA. The website / scheduler code was not deployed.

## 4e. Chrome 盤口同步（2026-10-28）

免費同步改由使用者桌面 Chrome 正常開啟台彩，決策中心要求擷取後交給 Python；不需要 HAR 或付費資料 API。安裝、0013 migration、兩側旗標、登入限制與回滾注意事項見 [browser-sync-guide.md](browser-sync-guide.md)。第 19 個 `browser_sync` job 每 15 秒處理佇列，預設停用、不抓外部資料。本次已依使用者正式授權套用 0013（單一交易與版本紀錄；新表 RLS、anon／authenticated 有效權限皆拒絕）。網站 `038acace` 與 Railway `2fa84597` 已部署成功，處理器旗標啟用、心跳健康，模型沿用。使用者尚需安裝 Chrome 助手與完成首次真實擷取；完整證據與操作見同步指南。

## 5. States

| State | Meaning | Action |
|---|---|---|
| `WAITING` (no future games, no odds yet, no predictions, 0 decisions, no bankroll) | season timing / nothing to do yet | none |
| `WAITING` nbainjuries / pricing / retrain "idle" | `scheduler-efficiency-v1` found nothing to do (§4b) | none |
| `BLOCKED` twsport | auto-scrape disabled or Cloudflare-blocked (expected) | manual HAR when you want to bet |
| `PASS` | healthy | none |
| `WARN` oddsapi quota < 100 / < 25 | credits low | none until reserve (paid calls auto-stop); raise cadence never |
| `WARN` stale non-critical heartbeat | e.g. injuries / schedule | check Railway logs |
| `FAIL` DB unreachable | Supabase paused or URL wrong | restore Supabase project / fix `DATABASE_URL` |
| `FAIL` artifact missing / hash mismatch / sklearn mismatch | model cannot load | §8 rollback or retrain |
| `FAIL` critical heartbeat stale (post-deploy) | scheduler down | Railway logs, redeploy |
| `auth_failed` on oddsapi | key rejected | rotate `ODDS_API_KEY` |

Supabase free projects pause when idle: if the scheduler is ever down for long, restore the project first.

## 6. Production data / account cleanup (read-only audit, you approve deletions)

```bash
cd pipeline && .venv/bin/python run_data_audit.py
```

Audit on 2026-10-05: **user #1 is the README demo account and still uses the publicly documented demo password**; user #2 is also a test account; neither has bets / bankroll / events. `model_metrics` has two `elo-v0.1-seed` rows (fake seed metrics, shown on the performance page). Bets, ledger, bankroll, decisions, odds: all empty (no fake data). `predictions` hold only `ml-v1.0` / `elo-v1.0` backtest-era rows (KEEP; not prospective evidence).

Suggested order (run in the Supabase SQL editor; each block is rolled back until you change `ROLLBACK` to `COMMIT`):

1. Create your real account (§3 step 3).
2. Remove demo / test users and seed metrics:
   ```sql
   BEGIN;
   DELETE FROM users WHERE id IN (1, 2) AND email LIKE '%@example.com'
     AND NOT EXISTS (SELECT 1 FROM bets WHERE user_id = users.id)
     AND NOT EXISTS (SELECT 1 FROM bankroll_accounts WHERE user_id = users.id);
   DELETE FROM model_metrics WHERE model_version ILIKE '%seed%';
   -- check the reported row counts, then:
   ROLLBACK;   -- change to COMMIT after review
   ```
3. Optional: stale `data_sources` rows `bbref`, `balldontlie`, `nba_api` (no job updates them). Harmless; they show as stale on the status page.
4. Seed injury row (found 2026-10-21): `injuries.id = 9` is the seed's "Klay Thompson GSW Questionable" (report time 2026-08-01 03:30 UTC, created 2026-08-01 — no official report exists then). The real 2026-10-20 23:45 report then copied its GSW team into `id = 79` ("Available / Not listed on latest official report"), which is why the injury page showed Klay under GSW. Neither row exists in `injury_report_entries` (the as-of source used by models). Also from the seed: only 10 teams have `teams.name_zh` (the site no longer depends on it).
   ```sql
   BEGIN;
   DELETE FROM injuries
    WHERE id IN (9, 79) AND player_id = (SELECT id FROM players WHERE nba_player_id = 202691)
      AND team_id = (SELECT id FROM teams WHERE abbr = 'GSW');
   -- expect DELETE 2, then:
   ROLLBACK;   -- change to COMMIT after review
   ```
   **Done 2026-10-22 TPE (user authorized):** exactly these 2 rows deleted; Klay has no remaining `injuries` rows, and none of the latest injury rows since 2026-10-20 TPE sit on a team different from the synced roster.

## 7. Taiwan Sports Lottery — manual HAR workflow

Automatic scraping is blocked by Cloudflare; this project does not circumvent it.

1. Normal Chrome → DevTools → Network → load the NBA league and each relevant game's full-game / first-half markets → **Export HAR (sanitized)**, including response content → save to `pipeline/captures/` (git/docker-ignored; `*.har` is ignored everywhere). Cookies / Authorization are not required; [Chrome's export reference](https://developer.chrome.com/docs/devtools/network/reference/#save-as-har) explains the sanitized export.
2. Offline inspection (no DB / `.env` access; shows market coverage and per-game capture windows):
   ```bash
   cd pipeline
   .venv/bin/python run_twsport_har.py captures/<file>.har --inspect
   ```
3. DB matching dry-run (reads the configured DB, writes nothing):
   ```bash
   .venv/bin/python run_twsport_har.py captures/<file>.har
   ```
4. After confirming the target DB and matching, import, then shred the file (HAR may still contain sensitive data):
   ```bash
   .venv/bin/python run_twsport_har.py captures/<file>.har --import --delete-after
   ```

As-of semantics: snapshot `fetched_at` = the HAR response completion time (`startedDateTime + time`), never the import time. The paper strategy (execution-v1, T = tip-off − 60 min) only reads `fetched_at ≤ T`, so a HAR captured after T cannot influence that game's decision. Taiwan's risk-v1 quote-age limit is 60 min at T, making the capture window tip-off − 120 min through tip-off − 60 min. Capture shortly before T and import promptly, preferably before T. HARs older than 2 h are refused unless `--allow-stale` (still stored with the true capture time); that flag never overrides the decision cutoff or quote-age guard. `--inspect` can diagnose an old capture but does not certify DB matching, paper eligibility or actionable status. Future timestamps beyond the existing five-minute clock tolerance are rejected. The tool never prints headers or bodies. See [source access details](data-source-access-2026-10-15.md).

## 8. Rollbacks

- **Model artifact:** on Railway shell (or locally against the same dir) `python run_retrain.py --list`, then `python run_retrain.py --rollback` (previous) or `--rollback --to <version>`. Versions are immutable and never auto-deleted; `CURRENT.json` switches atomically. To discard a bad volume completely, delete `CURRENT.json` on the volume and redeploy — the bootstrap artifact is re-copied.
- **Scheduler code:** Railway → Deployments → pick a previous deployment → Redeploy. Or `git revert` on `main` (do not move tag `phase-d5-complete`).
- **Website:** Cloudflare Pages → Deployments → previous deployment → Rollback to this deployment.
- **Database:** migrations are forward-only; `0001–0008` are applied (`0009` Telegram push, `0010` Data API lockdown, `0011` referee assignments, `0012` roster sync column). Use the pre-0008 backup only for disasters, never as routine.

## 9. Readiness command reference

```bash
cd pipeline
.venv/bin/python run_production_check.py                 # before deploy: stale heartbeats = WAITING
.venv/bin/python run_production_check.py --post-deploy   # after deploy: critical stale heartbeats = FAIL
.venv/bin/python run_production_check.py --json          # machine readable
.venv/bin/python run_data_audit.py                       # KEEP / REVIEW / SAFE TO REMOVE
.venv/bin/python run_model_monitor.py --days 14 --output artifacts/model-monitor-latest.json  # 實際賽前預測評估；不重訓、不寫 DB
.venv/bin/python scheduler.py --print-next-runs          # next trigger per job
.venv/bin/python run_notify.py --job system --dry-run     # Telegram alert that would be sent now (no send / write)
```

All of these are read-only, never call The Odds API, and never print secrets.

Recent model evaluation: [2026-10-29 review](model-review-2026-10-29.md). The read-only CLI selects observed pregame records and reports early / T-60 performance separately. The separate materializer below publishes aggregates to the website; neither recalibrates production or treats unavailable Taiwan paper ROI as zero.

### Enabled: independent adaptive shadow / website model reports (2026-10-29)

**0014 was applied at 17:25 TPE and the independent experiment activated at 17:28 TPE, with explicit user authorization.** The website and Railway deployment succeeded; both new heartbeats are healthy and the live performance page shows the experiment collecting. See [deployment evidence and frozen experiment rules](model-shadow-2026-10-29.md). Two independent jobs bring the total to 21: `model_shadow` (every minute at :20, enabled in production) and `model_monitor` (:14/:44). Production has `MODEL_SHADOW_ENABLED=true` and `MODEL_MONITOR_ENABLED=true`. Existing production inference, weekly retraining, pricing, sizing and actual bet settlement keep their current rules. Disable the shadow flag to stop collecting; retain its ledger. There is no automatic promotion.
