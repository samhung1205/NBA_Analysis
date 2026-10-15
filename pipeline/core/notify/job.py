"""
Telegram 推播工作（Phase E.1 notify-v1；只讀已物化結果 + 寫 notification_events）
------------------------------------------------------------
    actionable_job        每分鐘（decision_board 之後）：使用者最新、仍新鮮（≤ 10 分鐘）的 decision snapshot 中
                          新出現的台彩 ACTIONABLE 機會 → 一則合併訊息。同一 game × 玩法 × 選項 × 盤口線 × 賠率只通知一次。
                          TELEGRAM_NOTIFY_INCLUDE_REVIEW=true 時也通知 REVIEW（qualified 但有資料品質旗標，例如開季
                          season_opener / injury_unknown；開季初期多數機會會落在這一組），訊息逐筆標示分組。
    daily_summary_job     每日 12:40（台灣，early 預測之後）：台灣明日的 eligible 賽事與最新預測摘要（每天一則）。
    system_alert_job      每 15 分鐘：production readiness（--post-deploy 語意）的 FAIL 集合有變化 → 通知新增 / 恢復；
                          持續未排除每 6 小時提醒一次。資料庫連不上時改用行程記憶體去重、直接送出。

邊界：
* 不計算任何機率 / EV / Kelly / 額度：全部來自 Python 已物化的 decision_opportunities / predictions / readiness。
* 平台不會自動下注；訊息附免責提醒、不使用「推薦」字眼。國際盤（diagnostic）永遠不推播。
* notification_events.dedup_key UNIQUE：先領取（INSERT … ON CONFLICT DO NOTHING）再送出，不會重送；
  送出失敗保留 failed 列，之後最多重試 MAX_ATTEMPTS 次。
* TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未設定 → 三個 job 立即返回（不連 DB、不寫心跳）。
* 排程器本身停機時無法自我告警（行程不在就送不出訊息）；這需要外部監控，不在 notify-v1 範圍內。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..decision import policy as DP
from ..production.features import HISTORY_STAGES
from ..timeutil import ensure_utc, now_utc, parse_utc, tpe_date, tpe_day_bounds_utc
from . import messages as M
from .telegram import TelegramClient, TelegramError, client_from_settings

log = logging.getLogger(__name__)

NOTIFY_VERSION = "notify-v1"
HEARTBEAT = dict(source_key="notify_telegram", display_name="Telegram 推播（E.1 notify-v1）", category="notify")
MAX_ATTEMPTS = 3
STUCK_PENDING_AFTER = timedelta(minutes=10)     # 送出途中行程被終止 → pending 卡住；超過即可重新領取
SYSTEM_REMIND_EVERY = timedelta(hours=6)
STARTUP_GRACE = timedelta(minutes=15)           # 剛部署 / 重啟時心跳本來就可能過期，不立刻告警
_PROCESS_STARTED = time.monotonic()
SYSTEM_MEMORY: dict[str, Any] = {"fails": None, "since": None, "sent_at": None}


@dataclass
class NotifyReport:
    job: str
    as_of: str
    dry_run: bool = False
    candidates: int = 0
    sent: int = 0
    failed: int = 0
    note: str | None = None
    messages: list[str] = field(default_factory=list)


def schema_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('notification_events') IS NOT NULL AS ok")
    return bool(cur.fetchone()["ok"])


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, allow_nan=False, default=str)


def _claim(cur, now: datetime, *, kind: str, dedup_key: str, message: str, user_id: int | None = None,
           betting_day: date | None = None, payload: dict | None = None) -> int | None:
    """領取一則推播 → id；已送出 / 重試用完 / 別人正在送 → None。"""
    cur.execute("""INSERT INTO notification_events (kind, dedup_key, user_id, betting_day, message, payload,
                                                    created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (dedup_key) DO NOTHING RETURNING id""",
                (kind, dedup_key, user_id, betting_day, message, _j(payload or {}), now, now))
    r = cur.fetchone()
    if r is not None:
        return r["id"]
    cur.execute("""UPDATE notification_events SET status = 'pending', updated_at = %s
                    WHERE dedup_key = %s AND attempts < %s
                      AND (status = 'failed' OR (status = 'pending' AND updated_at < %s))
                    RETURNING id""",
                (now, dedup_key, MAX_ATTEMPTS, now - STUCK_PENDING_AFTER))
    r = cur.fetchone()
    return r["id"] if r else None


def _deliver(cur, client: TelegramClient, ids: list[int], text: str, now: datetime, rep: NotifyReport) -> bool:
    err: str | None = None
    try:
        client.send(text)
    except TelegramError as e:
        err = str(e)[:500]
    if err is None:
        cur.execute("""UPDATE notification_events SET status = 'sent', attempts = attempts + 1, sent_at = %s,
                              updated_at = %s, last_error = NULL, message = %s WHERE id = ANY(%s)""",
                    (now, now, text, ids))
        rep.sent += 1
        return True
    cur.execute("""UPDATE notification_events SET status = 'failed', attempts = attempts + 1, updated_at = %s,
                          last_error = %s, message = %s WHERE id = ANY(%s)""", (now, err, text, ids))
    rep.failed += 1
    log.warning("[notify] %s 送出失敗：%s", rep.job, err)
    return False


def _sent_keys(cur, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    cur.execute("SELECT dedup_key FROM notification_events WHERE dedup_key = ANY(%s) AND status = 'sent'", (keys,))
    return {r["dedup_key"] for r in cur.fetchall()}


def resolve_user(cur, st) -> tuple[int | None, str | None]:
    """推播對象（單一 chat 對應單一網站使用者）：TELEGRAM_NOTIFY_USER_ID，或資料庫裡唯一的使用者。"""
    if st.telegram_notify_user_id is not None:
        cur.execute("SELECT id FROM users WHERE id = %s", (st.telegram_notify_user_id,))
        return (st.telegram_notify_user_id, None) if cur.fetchone() else (None, "TELEGRAM_NOTIFY_USER_ID 不存在")
    cur.execute("SELECT id FROM users ORDER BY id LIMIT 2")
    rows = cur.fetchall()
    if not rows:
        return None, "沒有使用者"
    if len(rows) > 1:
        return None, "有多位使用者：請設定 TELEGRAM_NOTIFY_USER_ID"
    return rows[0]["id"], None


def _context(db, client, settings) -> tuple[Any, TelegramClient | None, Any]:
    from .. import db as default_db
    from ..config import settings as default_settings
    st = settings or default_settings
    return db or default_db, client if client is not None else client_from_settings(st), st


# ------------------------------------------------------------------ #
# ACTIONABLE 機會                                                      #
# ------------------------------------------------------------------ #

def opportunity_key(user_id: int, day: date, o: dict) -> str:
    line = "na" if o.get("display_line") is None else f"{float(o['display_line']):g}"
    odds = "na" if o.get("decimal_odds") is None else f"{float(o['decimal_odds']):.3f}"
    return (f"actionable:u{user_id}:{day.isoformat()}:g{o['game_id']}:{o.get('market')}:{o.get('side')}:{line}:"
            f"{o.get('bookmaker')}:{odds}")


def load_actionable(cur, user_id: int, day: date, now: datetime, *, include_review: bool = False
                    ) -> tuple[list[dict], str | None]:
    """UI 讀到的那一筆最新 snapshot；過期（> MATERIALIZATION_STALE_AFTER）不推播（額度可能已不正確）。"""
    cur.execute("""SELECT id, last_confirmed_at FROM decision_snapshots WHERE user_id = %s AND betting_day = %s
                    ORDER BY last_confirmed_at DESC, id DESC LIMIT 1""", (user_id, day))
    snap = cur.fetchone()
    if snap is None:
        return [], "no_snapshot"
    if now - ensure_utc(snap["last_confirmed_at"]) > DP.MATERIALIZATION_STALE_AFTER:
        return [], "stale_snapshot"
    groups = [DP.ACTIONABLE] + ([DP.REVIEW] if include_review else [])
    cur.execute("""SELECT o.game_id, o.market, o.market_type, o.side, o.display_line, o.bookmaker, o.decimal_odds,
                          o.status_group, o.warnings,
                          o.ev_per_unit, o.model_prob, o.fair_no_vig_prob, o.suggested_stake_amount,
                          o.max_additional_stake_amount, g.date_utc,
                          th.abbr AS home_abbr, th.name_zh AS home_name_zh, ta.abbr AS away_abbr, ta.name_zh AS away_name_zh
                     FROM decision_opportunities o
                     JOIN games g ON g.id = o.game_id
                     JOIN teams th ON th.id = g.home_team_id
                     JOIN teams ta ON ta.id = g.away_team_id
                    WHERE o.snapshot_id = %s AND o.status_group = ANY(%s) AND o.scope_kind = 'taiwan_primary'
                      AND COALESCE(g.status, 'scheduled') = 'scheduled' AND g.date_utc > %s
                    ORDER BY g.date_utc, o.display_rank, o.id""", (snap["id"], groups, now))
    return list(cur.fetchall()), None


def actionable_job(now: datetime | None = None, *, db=None, client: TelegramClient | None = None, settings=None,
                   dry_run: bool = False) -> NotifyReport:
    from ..decision.job import target_days

    db, client, st = _context(db, client, settings)
    now = ensure_utc(now or now_utc())
    rep = NotifyReport("actionable", now.isoformat(), dry_run)
    if client is None and not dry_run:
        rep.note = "not_configured"
        return rep
    with db.cursor() as cur:
        ready = schema_ready(cur)
        if not ready and not dry_run:
            rep.note = "資料庫尚未套用 migrations/postgres/0009_phase_e1_notifications.sql"
            return rep
        uid, why = resolve_user(cur, st)
        if uid is None:
            rep.note = why
            return rep
        for day in target_days(cur, now):
            opps, _ = load_actionable(cur, uid, day, now, include_review=st.telegram_notify_include_review)
            rep.candidates += len(opps)
            keyed = [(opportunity_key(uid, day, o), o) for o in opps]
            if dry_run:
                done = _sent_keys(cur, [k for k, _ in keyed]) if ready else set()
                fresh = [o for k, o in keyed if k not in done]
                if fresh:
                    rep.messages.append(M.format_actionable(day, fresh, st.notify_site_url))
                continue
            claimed = [(i, o) for k, o in keyed
                       if (i := _claim(cur, now, kind="actionable", dedup_key=k, message="(pending)", user_id=uid,
                                       betting_day=day, payload={"version": NOTIFY_VERSION, "game_id": o["game_id"]}))
                       is not None]
            if claimed:
                text = M.format_actionable(day, [o for _, o in claimed], st.notify_site_url)
                _deliver(cur, client, [i for i, _ in claimed], text, now, rep)
    return rep


# ------------------------------------------------------------------ #
# 每日賽事摘要                                                          #
# ------------------------------------------------------------------ #

def load_day_games(cur, day: date) -> list[dict]:
    lo, hi = tpe_day_bounds_utc(day)
    cur.execute("""SELECT g.id, g.date_utc, th.abbr AS home_abbr, ta.abbr AS away_abbr,
                          p.home_win_prob, p.pred_margin, p.pred_total,
                          p.features_json -> 'data_quality' -> 'flags' AS quality_flags
                     FROM games g
                     JOIN teams th ON th.id = g.home_team_id
                     JOIN teams ta ON ta.id = g.away_team_id
                     LEFT JOIN LATERAL (SELECT home_win_prob, pred_margin, pred_total, features_json FROM predictions
                                         WHERE game_id = g.id ORDER BY created_at DESC, id DESC LIMIT 1) p ON TRUE
                    WHERE g.date_utc >= %s AND g.date_utc < %s AND g.season_stage = ANY(%s)
                      AND COALESCE(g.status, 'scheduled') <> 'final'
                    ORDER BY g.date_utc, g.id""", (lo, hi, list(HISTORY_STAGES)))
    return list(cur.fetchall())


def daily_summary_job(now: datetime | None = None, *, db=None, client: TelegramClient | None = None, settings=None,
                      dry_run: bool = False, day: date | None = None) -> NotifyReport:
    db, client, st = _context(db, client, settings)
    now = ensure_utc(now or now_utc())
    day = day or tpe_date(now) + timedelta(days=1)
    rep = NotifyReport("daily_summary", now.isoformat(), dry_run)
    if client is None and not dry_run:
        rep.note = "not_configured"
        return rep
    with db.cursor() as cur:
        if not schema_ready(cur) and not dry_run:
            rep.note = "資料庫尚未套用 migrations/postgres/0009_phase_e1_notifications.sql"
            return rep
        games = load_day_games(cur, day)
        rep.candidates = len(games)
        if not games:
            rep.note = "no_games"
            return rep
        text = M.format_daily_summary(day, games, st.notify_site_url)
        if dry_run:
            rep.messages.append(text)
            return rep
        i = _claim(cur, now, kind="daily_summary", dedup_key=f"daily_summary:{day.isoformat()}", message=text,
                   betting_day=day, payload={"version": NOTIFY_VERSION, "n_games": len(games)})
        if i is None:
            rep.note = "already_sent"
            return rep
        _deliver(cur, client, [i], text, now, rep)
    return rep


# ------------------------------------------------------------------ #
# 系統異常告警                                                          #
# ------------------------------------------------------------------ #

def collect_failures(now: datetime, run_checks: Callable[..., list] | None = None) -> dict[str, str]:
    from ..production import readiness as R
    checks = (run_checks or R.run_checks)(post_deploy=True, now=now)
    return {f"{c.area}/{c.name}": c.detail for c in checks if c.status == R.FAIL}


def _load_prev_system(db) -> dict | None:
    with db.cursor() as cur:
        if not schema_ready(cur):
            return None
        cur.execute("""SELECT payload, sent_at FROM notification_events WHERE kind = 'system_alert' AND status = 'sent'
                        ORDER BY sent_at DESC, id DESC LIMIT 1""")
        r = cur.fetchone()
    if r is None:
        return None
    p = r["payload"] or {}
    return {"fails": set(p.get("fails") or []), "since": parse_utc(p["since"]) if p.get("since") else None,
            "sent_at": ensure_utc(r["sent_at"])}


def system_alert_job(now: datetime | None = None, *, db=None, client: TelegramClient | None = None, settings=None,
                     dry_run: bool = False, failures: dict[str, str] | None = None,
                     ignore_grace: bool = False) -> NotifyReport:
    db, client, _ = _context(db, client, settings)
    now = ensure_utc(now or now_utc())
    rep = NotifyReport("system_alert", now.isoformat(), dry_run)
    if client is None and not dry_run:
        rep.note = "not_configured"
        return rep
    if not ignore_grace and time.monotonic() - _PROCESS_STARTED < STARTUP_GRACE.total_seconds():
        rep.note = "startup_grace"
        return rep
    current = failures if failures is not None else collect_failures(now)
    rep.candidates = len(current)
    db_ok = True
    try:
        prev = _load_prev_system(db)
    except Exception as e:  # noqa: BLE001  資料庫本身可能就是這次的 FAIL
        db_ok, prev = False, None
        log.warning("[notify] 讀取上一則系統告警失敗（%s），改用行程記憶體去重", type(e).__name__)
    mem = SYSTEM_MEMORY
    if mem["sent_at"] is not None and (prev is None or mem["sent_at"] > prev["sent_at"]):
        prev = {"fails": set(mem["fails"]), "since": mem["since"], "sent_at": mem["sent_at"]}
    s, p = set(current), set(prev["fails"]) if prev else set()
    reminder = False
    if s == p:
        if not s or prev is None or now - prev["sent_at"] < SYSTEM_REMIND_EVERY:
            rep.note = "unchanged"
            return rep
        reminder = True
    since = (prev["since"] or now) if (prev and s & p) else now
    text = M.format_system_alert(current, s - p, p - s, reminder_since=since if reminder else None)
    if dry_run:
        rep.messages.append(text)
        return rep
    payload = {"version": NOTIFY_VERSION, "fails": sorted(s), "since": since.isoformat()}
    key = f"system_alert:{now:%Y%m%dT%H%M}:{hashlib.sha256(_j(sorted(s)).encode()).hexdigest()[:12]}"
    ok = False
    if db_ok:
        try:
            with db.cursor() as cur:
                i = _claim(cur, now, kind="system_alert", dedup_key=key, message=text, payload=payload)
                if i is None:
                    rep.note = "already_claimed"
                    return rep
                ok = _deliver(cur, client, [i], text, now, rep)
        except Exception as e:  # noqa: BLE001
            log.warning("[notify] 系統告警寫入資料庫失敗（%s），直接送出", type(e).__name__)
            db_ok = False
    if not db_ok:
        try:
            client.send(text)
            ok = True
            rep.sent += 1
        except TelegramError as e:
            rep.failed += 1
            log.warning("[notify] 系統告警送出失敗：%s", e)
    if ok:
        mem.update(fails=set(s), since=since, sent_at=now)
    return rep


# ------------------------------------------------------------------ #
# 排程進入點                                                            #
# ------------------------------------------------------------------ #

def _scheduled(fn: Callable[[], NotifyReport]) -> None:
    from .. import db
    from ..config import settings
    if client_from_settings(settings) is None:       # 未設定 Telegram：不連 DB、不寫心跳
        return
    status, err, n = "ok", None, 0
    try:
        rep = fn()
        n = rep.sent
        if rep.failed:
            status, err = "warn", f"{rep.job}：{rep.failed} 則送出失敗（會重試）"
        elif rep.note and ("0009" in rep.note or "使用者" in rep.note or "USER_ID" in rep.note):
            status, err = "warn", f"{rep.job}：{rep.note}"
    except Exception as e:  # noqa: BLE001
        log.exception("notify job 失敗")
        status, err = "error", f"{type(e).__name__}: {e}"[:500]
    try:
        with db.cursor() as cur:
            db.heartbeat(cur, status=status, error=err, records_updated=n, **HEARTBEAT)
    except Exception:  # noqa: BLE001  DB 不可用時系統告警已直接送出
        log.warning("[notify] 心跳寫入失敗")


def notify_actionable_scheduled() -> None:
    _scheduled(actionable_job)


def notify_daily_summary_scheduled() -> None:
    _scheduled(daily_summary_job)


def notify_system_alerts_scheduled() -> None:
    _scheduled(system_alert_job)
