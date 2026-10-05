"""
Production readiness check（唯讀）
------------------------------------------------------------
python run_production_check.py [--post-deploy] [--json]

* 只做 SELECT：資料庫連線設為 READ ONLY transaction；不呼叫任何外部 API（不花 The Odds API 額度）；不寫任何東西。
* 不印出任何 secret：只報「有沒有設定」，連線字串只報 port / 是否 pooler。
* 狀態語意（嚴重度由低到高）：
    PASS     正常
    WAITING  預期中的等待（賽季尚未開打、盤口尚未進入窗口、尚無人下注…），不需要處理
    WARN     值得看但不致命
    BLOCKED  已知且已接受的外部阻擋（例如台彩自動爬取被 Cloudflare 擋 → 走手動 HAR）
    FAIL     需要處理（資料庫連不上、artifact 缺失、migration 缺、部署後排程器心跳過期…）
  結束碼：有任何 FAIL → 1，否則 0（--strict 時 WARN 也 → 2）。
* 部署前（預設）心跳過期只 WARN（排程器本來就還沒跑）；--post-deploy 時關鍵 job 過期 → FAIL。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

PASS, WAITING, WARN, BLOCKED, FAIL = "PASS", "WAITING", "WARN", "BLOCKED", "FAIL"
SEVERITY = {PASS: 0, WAITING: 1, BLOCKED: 2, WARN: 3, FAIL: 4}
REQUIRED_MIGRATION_PREFIX = "0008"
EXPECTED_MIGRATIONS = ("0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008")


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def worst(checks: list[Check]) -> str:
    return max((c.status for c in checks), key=lambda s: SEVERITY[s], default=PASS)


# ------------------------------------------------------------------ #
# 純函式分類（不碰 DB，可單元測試）                                       #
# ------------------------------------------------------------------ #

def age_minutes(ts: datetime | None, now: datetime) -> float | None:
    return None if ts is None else (now - ts).total_seconds() / 60


def classify_heartbeat(row: dict | None, now: datetime, *, expected_min: float, critical: bool, post_deploy: bool,
                       stale_factor: float = 3.0, min_stale_min: float = 15.0) -> tuple[str, str]:
    """data_sources 心跳 → (status, detail)。stale = 最近一次 attempt 超過 max(expected × factor, min_stale)。
    部署前（post_deploy=False）：沒有 / 過期的心跳是預期的（排程器還沒跑）→ WAITING。
    部署後：critical job → FAIL，其餘 → WARN。job 自己回報 error 不分階段（critical 且部署後 FAIL，否則 WARN）。"""
    bad = FAIL if (critical and post_deploy) else WARN
    if row is None:
        return (bad if post_deploy else WAITING), "尚無心跳（job 還沒執行過）"
    if row.get("last_status") == "error":
        return bad, f"最近一次 error：{(row.get('last_error') or '')[:120]}"
    attempt = age_minutes(row.get("last_attempt_at"), now)
    limit = max(expected_min * stale_factor, min_stale_min)
    if attempt is None or attempt > limit:
        shown = "未知" if attempt is None else f"{attempt:.0f} 分鐘前"
        return (bad if post_deploy else WAITING), (
            f"最後執行 {shown}（預期每 {expected_min:.0f} 分鐘、上限 {limit:.0f}）"
            + ("" if post_deploy else "；排程器尚未部署 / 尚未重新啟動時屬預期"))
    note = f"；{row['last_error'][:80]}" if row.get("last_status") == "warn" and row.get("last_error") else ""
    return PASS, f"最後執行 {attempt:.0f} 分鐘前，status={row.get('last_status')}{note}"


def classify_oddsapi(row: dict | None, last_run: dict | None, now: datetime, *, key_set: bool, warn_at: int,
                     reserve: int, post_deploy: bool) -> tuple[str, str]:
    if not key_set:
        return WARN, "ODDS_API_KEY 未設定（job 會回報 not_configured，國際盤僅供參考，不影響台彩路徑）"
    if row is None or row.get("last_outcome") is None:
        return (WAITING, "尚未執行過（每日 00/06/12/18:40 台灣時間；啟動時不補跑）")
    outcome = row.get("last_outcome")
    remaining = ((row.get("meta") or {}).get("quota_remaining")
                 if isinstance(row.get("meta"), dict) else None)
    if remaining is None and last_run:
        remaining = last_run.get("quota_remaining")
    qtxt = f"，額度剩 {remaining}" if remaining is not None else ""
    if outcome == "no_nba_markets":
        return WAITING, f"沒有進入 48 小時窗口的 NBA 賽事（不花額度）{qtxt}"
    if outcome in ("auth_failed",):
        return FAIL, f"API key 被拒（auth_failed）{qtxt}"
    if outcome == "quota_low" or (remaining is not None and remaining < reserve):
        return WARN, f"額度低於保留值 {reserve}，付費端點已停用{qtxt}"
    if remaining is not None and remaining < warn_at:
        return WARN, f"額度低於警戒值 {warn_at}{qtxt}"
    if outcome in ("success", "partial"):
        age = age_minutes(row.get("last_success_at"), now)
        if age is not None and age > 6 * 60 * 2 + 60:
            return WARN, f"上次成功 {age / 60:.1f} 小時前（預期每 6 小時）{qtxt}"
        return PASS, f"outcome={outcome}{qtxt}"
    if outcome == "not_configured":
        return WARN, "outcome=not_configured"
    return WARN, f"outcome={outcome}：{(row.get('last_error') or '')[:100]}{qtxt}"


def classify_twsport(row: dict | None, newest_snapshot_at: datetime | None, now: datetime, *, enabled: bool,
                     games_in_window: int) -> tuple[str, str]:
    """台彩是 actionable market，但自動爬取被 Cloudflare 擋（不繞過）→ 預期 BLOCKED / 手動 HAR。"""
    snap_age = age_minutes(newest_snapshot_at, now)
    snap_txt = ("尚無任何台彩盤口快照" if snap_age is None
                else f"最新台彩快照 {snap_age / 60:.1f} 小時前")
    if not enabled:
        return BLOCKED, f"自動爬取已停用（TWSPORT_ENABLED=false；預期）→ 手動 HAR 匯入。{snap_txt}"
    outcome = (row or {}).get("last_outcome")
    if outcome == "blocked":
        return BLOCKED, f"Cloudflare 擋下自動化瀏覽器（不繞過）→ 手動 HAR 匯入。{snap_txt}"
    if outcome in ("success", "partial"):
        return PASS, f"outcome={outcome}。{snap_txt}"
    if outcome == "no_nba_markets":
        return WAITING, "目前沒有 NBA 盤"
    if row is None or outcome is None:
        return WAITING, f"尚未執行過。{snap_txt}"
    return WARN, f"outcome={outcome}。{snap_txt}"


def classify_future_games(n_next48h: int, n_future: int) -> tuple[str, str]:
    if n_future == 0:
        return WAITING, "資料庫沒有未來賽事（休賽期 / 賽程尚未同步；不是錯誤）"
    if n_next48h == 0:
        return WAITING, f"未來共 {n_future} 場，但 48 小時內沒有比賽（尚未進入預測 / 盤口窗口）"
    return PASS, f"48 小時內 {n_next48h} 場、未來共 {n_future} 場"


def classify_predictions(n_total: int, n_window: int, n_window_missing: int) -> tuple[str, str]:
    if n_window == 0:
        return (PASS if n_total else WAITING), f"ml-v2 預測累計 {n_total} 筆；36 小時窗口內沒有比賽"
    if n_window_missing == 0:
        return PASS, f"36 小時窗口 {n_window} 場全部有 ml-v2 預測（累計 {n_total}）"
    return WARN, f"36 小時窗口 {n_window} 場中 {n_window_missing} 場尚無 ml-v2 預測（predict_early 每日 12:20 台灣時間）"


def classify_count_table(label: str, n: int, *, waiting_reason: str) -> tuple[str, str]:
    return (PASS, f"{label} {n} 筆") if n else (WAITING, f"{label} 0 筆（{waiting_reason}）")


def classify_database_url(url: str | None) -> tuple[str, str]:
    """只看 port / host 型態，不輸出 URL。"""
    if not url:
        return FAIL, "DATABASE_URL 未設定"
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        port, host = u.port, u.hostname or ""
    except ValueError:
        return FAIL, "DATABASE_URL 格式無法解析"
    if u.scheme not in ("postgres", "postgresql"):
        return FAIL, f"DATABASE_URL scheme={u.scheme!r}，需為 postgresql"
    if port == 6543:
        return WARN, "port 6543 = Supabase Transaction pooler；正式環境請用 Session pooler（port 5432）"
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return WARN, "Supabase 直連位址只有 IPv6，Railway 可能連不上；請用 Session pooler"
    return PASS, f"port={port}，{'pooler' if 'pooler' in host else 'non-pooler'} host"


# ------------------------------------------------------------------ #
# 收集（唯讀 SELECT）                                                    #
# ------------------------------------------------------------------ #

def _one(cur, sql: str, params: tuple = ()) -> dict | None:
    cur.execute(sql, params)
    return cur.fetchone()


def _val(cur, sql: str, params: tuple = ()) -> Any:
    row = _one(cur, sql, params)
    return None if row is None else next(iter(row.values()))


def _guard(area: str, name: str, fn: Callable[[], list[Check]]) -> list[Check]:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — 單一檢查失敗不能讓整份報告消失
        return [Check(area, name, FAIL, f"檢查本身失敗：{type(e).__name__}: {str(e)[:160]}")]


def check_environment(env: dict[str, str]) -> list[Check]:
    out: list[Check] = []
    st, d = classify_database_url(env.get("DATABASE_URL") or env.get("DATABASE_URL_DIRECT"))
    out.append(Check("env", "DATABASE_URL", st, d))
    out.append(Check("env", "ODDS_API_KEY", PASS if env.get("ODDS_API_KEY") else WARN,
                     "已設定" if env.get("ODDS_API_KEY") else "未設定（國際盤 job 會 not_configured）"))
    tw = (env.get("TWSPORT_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
    out.append(Check("env", "TWSPORT_ENABLED", PASS if not tw else WARN,
                     "false（Railway 應維持關閉；自動爬取被 Cloudflare 擋）" if not tw
                     else "true：Railway 沒有 Chrome，會一直 blocked；請設為 false"))
    mad = env.get("MODEL_ARTIFACT_DIR")
    out.append(Check("env", "MODEL_ARTIFACT_DIR", PASS if mad else WARN,
                     mad if mad else "未設定 → 使用映像內 pipeline/artifacts/production（Railway 上重新部署會遺失重訓結果，請掛 volume）"))
    tz = env.get("TZ")
    out.append(Check("env", "TZ", PASS if tz == "Asia/Taipei" else WARN,
                     tz or "未設定（排程已明確使用 Asia/Taipei，影響僅限 log 時間）"))
    return out


def check_artifact(root: Path | None = None) -> list[Check]:
    from . import artifact as art
    out: list[Check] = []
    r = art.artifact_root(root)
    if not (r / "CURRENT.json").exists():
        return [Check("artifact", "CURRENT.json", FAIL, f"{r} 沒有 CURRENT.json（entrypoint 應 bootstrap；或執行 python run_retrain.py）")]
    try:
        loaded = art.load_current(r)
    except art.ArtifactError as e:
        return [Check("artifact", "load_current", FAIL, f"{type(e).__name__}: {str(e)[:200]}")]
    m = loaded.manifest
    out.append(Check("artifact", "current", PASS,
                     f"{loaded.artifact_version}（schema {m.get('schema_version')}，sha256 {str(m.get('sha256'))[:12]}…，"
                     f"known_good={m.get('known_good')}，cutoff {loaded.training_cutoff_utc}）"))
    out.append(Check("artifact", "root", PASS if os.access(r, os.W_OK) else FAIL,
                     f"{r}（{'可寫' if os.access(r, os.W_OK) else '不可寫：重訓 / rollback 會失敗'}，"
                     f"{len(art.list_versions(r))} 個版本）"))
    hist = art.read_history(r)
    if hist:
        last = hist[-1]
        out.append(Check("artifact", "history", PASS, f"最近事件：{last.get('event') or last.get('reason')} → "
                                                       f"{last.get('artifact_version') or last.get('to')}"))
    return out


def check_scheduler_definition(now: datetime) -> list[Check]:
    from ..scheduling import job_specs, next_run_times
    specs = job_specs()
    nxt = next_run_times(specs, now)
    never = [k for k, v in nxt.items() if v is None]
    out = [Check("scheduler", "jobs", FAIL if never else PASS,
                 f"{len(specs)} 個 job 已定義" + (f"；永不觸發：{never}" if never else ""))]
    soonest = sorted((v, k) for k, v in nxt.items() if v)[:3]
    out.append(Check("scheduler", "next_runs", PASS,
                     "；".join(f"{k} @ {v.isoformat(timespec='seconds')}" for v, k in soonest)))
    return out


def check_database(cur, now: datetime) -> list[Check]:
    out: list[Check] = []
    out.append(Check("database", "reachable", PASS, "SELECT 1 OK（read-only transaction）"))
    cur.execute("SELECT filename FROM schema_migrations")
    have = {r["filename"][:4] for r in cur.fetchall()}
    missing = [p for p in EXPECTED_MIGRATIONS if p not in have]
    out.append(Check("database", "migrations", FAIL if missing else PASS,
                     f"缺少 {missing}（npm run db:migrate:pg）" if missing else f"0001–{REQUIRED_MIGRATION_PREFIX} 皆已套用"))
    return out


def check_games_and_predictions(cur, now: datetime) -> list[Check]:
    n48 = _val(cur, "SELECT COUNT(*) FROM games WHERE date_utc > %s AND date_utc <= %s AND status <> 'final'",
               (now, now + timedelta(hours=48)))
    nf = _val(cur, "SELECT COUNT(*) FROM games WHERE date_utc > %s AND status <> 'final'", (now,))
    last_final = _val(cur, "SELECT MAX(date_utc) FROM games WHERE status = 'final'")
    st, d = classify_future_games(int(n48), int(nf))
    out = [Check("data", "future_games", st, d + (f"；最後一場已結束賽事 {last_final.date()}" if last_final else ""))]
    ntot = int(_val(cur, "SELECT COUNT(*) FROM predictions WHERE model_version LIKE 'ml-v2%%'"))
    nwin = int(_val(cur, "SELECT COUNT(*) FROM games WHERE date_utc > %s AND date_utc <= %s AND status <> 'final'",
                    (now, now + timedelta(hours=36))))
    nmiss = int(_val(cur, """SELECT COUNT(*) FROM games g WHERE g.date_utc > %s AND g.date_utc <= %s AND g.status <> 'final'
                              AND NOT EXISTS (SELECT 1 FROM predictions p WHERE p.game_id = g.id
                                              AND p.model_version LIKE 'ml-v2%%')""",
                     (now, now + timedelta(hours=36))))
    st, d = classify_predictions(ntot, nwin, nmiss)
    out.append(Check("data", "ml-v2 predictions", st, d))
    return out


def check_heartbeats(cur, now: datetime, post_deploy: bool) -> list[Check]:
    cur.execute("SELECT * FROM data_sources")
    rows = {r["source_key"]: r for r in cur.fetchall()}
    # (source_key, expected_min, critical)；critical = 排程器是否存活的判斷依據
    jobs = [("decision_board", 1, True), ("market_pricing", 5, True), ("bet_sizing", 5, True),
            ("paper_strategy", 5, True), ("model_predict", 24 * 60, False), ("nbainjuries", 30, False),
            ("model_retrain", 7 * 24 * 60, False)]
    out = []
    for key, exp, crit in jobs:
        row = rows.get(key)
        if key == "model_retrain":
            if row is None:
                out.append(Check("heartbeat", key, WAITING, "尚未重訓過（每週一 16:00 台灣時間）"))
                continue
            st, d = classify_heartbeat(row, now, expected_min=exp, critical=False, post_deploy=post_deploy,
                                       stale_factor=1.3)
        else:
            st, d = classify_heartbeat(row, now, expected_min=exp, critical=crit, post_deploy=post_deploy,
                                       stale_factor=1.25 if exp >= 24 * 60 else 3.0)
        out.append(Check("heartbeat", key, st, d))
    sched = [rows.get(k) for k in ("nba_cdn", "espn", "nba_api") if rows.get(k)]
    if sched:
        best = max(sched, key=lambda r: r.get("last_attempt_at") or datetime.min.replace(tzinfo=now.tzinfo))
        st, d = classify_heartbeat(best, now, expected_min=24 * 60, critical=False, post_deploy=post_deploy,
                                   stale_factor=1.25)
        out.append(Check("heartbeat", f"schedule/scores ({best['source_key']})", st, d))
    else:
        out.append(Check("heartbeat", "schedule/scores", WAITING if not post_deploy else WARN, "尚無賽程來源心跳"))
    return out


def check_odds(cur, now: datetime, env: dict[str, str], post_deploy: bool) -> list[Check]:
    cur.execute("SELECT * FROM data_sources WHERE source_key IN ('twsport', 'oddsapi')")
    rows = {r["source_key"]: r for r in cur.fetchall()}
    cur.execute("SELECT source, COUNT(*) AS n, MAX(fetched_at) AS newest FROM odds_snapshots GROUP BY source")
    snaps = {r["source"]: r for r in cur.fetchall()}
    win = int(_val(cur, "SELECT COUNT(*) FROM games WHERE date_utc > %s AND date_utc <= %s AND status <> 'final'",
                   (now, now + timedelta(hours=48))))
    tw_enabled = (env.get("TWSPORT_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
    st, d = classify_twsport(rows.get("twsport"), (snaps.get("twsport") or {}).get("newest"), now,
                             enabled=tw_enabled, games_in_window=win)
    out = [Check("odds", "twsport (actionable)", st, d + f"；累計 {(snaps.get('twsport') or {}).get('n', 0)} 筆快照")]
    last_run = _one(cur, "SELECT quota_remaining, quota_used, outcome, started_at FROM odds_fetch_runs "
                         "WHERE source = 'oddsapi' AND quota_remaining IS NOT NULL ORDER BY started_at DESC LIMIT 1")
    st, d = classify_oddsapi(rows.get("oddsapi"), last_run, now, key_set=bool(env.get("ODDS_API_KEY")),
                             warn_at=int(env.get("ODDS_API_QUOTA_WARN") or 100),
                             reserve=int(env.get("ODDS_API_QUOTA_RESERVE") or 25), post_deploy=post_deploy)
    out.append(Check("odds", "oddsapi (diagnostic only)", st,
                     d + f"；累計 {(snaps.get('oddsapi') or {}).get('n', 0)} 筆快照（不花額度：本檢查不呼叫 API）"))
    return out


def check_pricing_to_evidence(cur, now: datetime) -> list[Check]:
    out: list[Check] = []
    n = int(_val(cur, "SELECT COUNT(*) FROM market_pricing_snapshots"))
    st, d = classify_count_table("定價快照", n, waiting_reason="尚無台彩盤口 × 預測可定價")
    out.append(Check("pipeline", "pricing (D.2)", st, d))
    n = int(_val(cur, "SELECT COUNT(*) FROM bet_sizing_snapshots"))
    st, d = classify_count_table("理論注碼快照", n, waiting_reason="尚無定價結果；不是推薦")
    out.append(Check("pipeline", "sizing (D.3)", st, d))
    nd = int(_val(cur, "SELECT COUNT(*) FROM paper_strategy_decisions"))
    nb = int(_val(cur, "SELECT COUNT(*) FROM paper_strategy_bets"))
    ns = int(_val(cur, "SELECT COUNT(*) FROM paper_strategy_bets WHERE settlement_status <> 'pending'"))
    out.append(Check("pipeline", "paper strategy (D.4)", PASS if nd else WAITING,
                     f"decisions {nd}、paper bets {nb}（已結算 {ns}）；僅 prospective，無歷史 ROI"))
    users = int(_val(cur, "SELECT COUNT(*) FROM users"))
    ds = _one(cur, "SELECT COUNT(*) AS n, MAX(last_confirmed_at) AS latest FROM decision_snapshots")
    if int(ds["n"]):
        age = age_minutes(ds["latest"], now)
        out.append(Check("pipeline", "decision board (D.5)", PASS if age < 15 else WARN,
                         f"{ds['n']} 筆 snapshot、{users} 位使用者；最新確認 {age:.0f} 分鐘前"))
    else:
        out.append(Check("pipeline", "decision board (D.5)", WAITING if users else WARN,
                         "尚無 decision snapshot（排程器尚未物化 / 沒有未來賽事）" if users else "沒有使用者"))
    na = int(_val(cur, "SELECT COUNT(*) FROM bankroll_accounts"))
    nf = int(_val(cur, "SELECT COUNT(*) FROM bankroll_ledger WHERE entry_type = 'initial_funding'"))
    out.append(Check("bankroll", "readiness", PASS if nf else WAITING,
                     f"{na} 個帳戶、{nf} 筆 initial_funding" + ("" if nf else "（需在網站「資金」頁設定初始資金後才有注碼額度）")))
    real = int(_val(cur, "SELECT COUNT(*) FROM bets WHERE record_status = 'active'"))
    out.append(Check("evidence", "separation", PASS,
                     f"paper decisions {nd} / paper bets {nb}（已結算 {ns}） 與 實際注單 {real} 分開保存；"
                     "尚無歷史 ROI 宣稱，樣本不足前不得解讀"))
    return out


def run_checks(*, post_deploy: bool = False, now: datetime | None = None, env: dict[str, str] | None = None,
               artifact_root: Path | None = None) -> list[Check]:
    from ..timeutil import now_utc
    now = now or now_utc()
    env = dict(os.environ) if env is None else env
    out: list[Check] = _guard("env", "environment", lambda: check_environment(env))
    out += _guard("artifact", "artifact", lambda: check_artifact(artifact_root))
    url = env.get("DATABASE_URL") or env.get("DATABASE_URL_DIRECT")
    if not url:
        return out + [Check("database", "reachable", FAIL, "沒有 DATABASE_URL，略過所有資料庫檢查")]
    try:
        out += _guard("scheduler", "definition", lambda: check_scheduler_definition(now))
    except Exception as e:  # noqa: BLE001
        out.append(Check("scheduler", "definition", FAIL, f"{type(e).__name__}: {str(e)[:160]}"))
    import psycopg
    from psycopg.rows import dict_row
    try:
        conn = psycopg.connect(url, row_factory=dict_row, autocommit=False, connect_timeout=15, prepare_threshold=None)
    except Exception as e:  # noqa: BLE001
        return out + [Check("database", "reachable", FAIL, f"連線失敗：{type(e).__name__}（檢查 DATABASE_URL / 網路 / Supabase 是否被暫停）")]
    with conn:
        conn.read_only = True
        with conn.cursor() as cur:
            for name, fn in (("database", lambda: check_database(cur, now)),
                             ("games", lambda: check_games_and_predictions(cur, now)),
                             ("heartbeats", lambda: check_heartbeats(cur, now, post_deploy)),
                             ("odds", lambda: check_odds(cur, now, env, post_deploy)),
                             ("pipeline", lambda: check_pricing_to_evidence(cur, now))):
                out += _guard(name, name, fn)
                if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
                    conn.rollback()
                    conn.read_only = True
    return out


def render_text(checks: list[Check]) -> str:
    width = max(len(c.area) + len(c.name) + 3 for c in checks)
    lines = [f"{c.status:<8} {(c.area + ' / ' + c.name):<{width}} {c.detail}" for c in checks]
    counts = {s: sum(c.status == s for c in checks) for s in SEVERITY}
    lines.append("")
    lines.append("  ".join(f"{s}={n}" for s, n in counts.items()) + f"   overall={worst(checks)}")
    return "\n".join(lines)


def to_json(checks: list[Check]) -> str:
    return json.dumps({"overall": worst(checks), "checks": [c.as_dict() for c in checks]}, ensure_ascii=False, indent=1)
