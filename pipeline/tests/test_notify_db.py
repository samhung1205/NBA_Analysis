"""Phase E.1 notify-v1（暫存 schema）：ACTIONABLE 只通知一次 / 失敗重試上限 / 過期 snapshot 不推播 / 每日摘要 / 系統告警持久去重。

ACTIONABLE 走真實的 D.2 定價 → D.3 sizing → D.5 decision board 物化（與 test_decision_db 相同的合成資料），
推播 job 只讀物化結果。Telegram 以假 client 取代，不打網路。
"""
from __future__ import annotations

from datetime import date, timedelta

from core.decision import policy as P
from core.notify import job as nj
from test_decision_db import NOW, _ready, account, opps, run, snapshots, user
from test_pricing_artifact import game, league, setup  # noqa: F401
from test_notify import FakeClient


class Settings:
    telegram_bot_token = telegram_chat_id = "set"
    telegram_notify_user_id = None
    notify_site_url = None
    telegram_notify_include_review = True     # 合成資料帶 injury_unknown 等旗標 → qualified 機會都在 REVIEW


class ActionableOnly(Settings):
    telegram_notify_include_review = False


def notifications(db, kind=None):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM notification_events WHERE (%s::text IS NULL OR kind = %s) ORDER BY id", (kind, kind))
        return cur.fetchall()


def actionable(db, now, client):
    return nj.actionable_job(now, db=db, client=client, settings=Settings())


def _board(db, game, setup):
    _ready(db, game, setup)
    uid = user(db)
    account(db, uid)
    run(db, uid=uid)
    [s] = [x for x in snapshots(db, uid) if x["betting_day"].isoformat() == "2026-10-22"]
    acts = [o for o in opps(db, s["id"]) if o["status_group"] in (P.ACTIONABLE, P.REVIEW)]
    assert acts, "合成資料應產生至少一個 qualified（ACTIONABLE / REVIEW）機會"
    return uid, acts


def test_actionable_notified_once_and_matches_board(db, game, setup):
    uid, acts = _board(db, game, setup)
    c = FakeClient()
    rep = actionable(db, NOW, c)
    assert rep.candidates == len(acts) and rep.sent == 1 and len(c.sent) == 1
    rows = notifications(db, "actionable")
    assert len(rows) == len(acts) and {r["status"] for r in rows} == {"sent"} and all(r["user_id"] == uid for r in rows)
    assert "REVIEW" in c.sent[0] and nj.M.DISCLAIMER in c.sent[0]
    # 下一分鐘（board 內容沒變）→ 不重送
    run(db, NOW + timedelta(minutes=1), uid=uid)
    assert actionable(db, NOW + timedelta(minutes=1), c).sent == 0 and len(c.sent) == 1
    # dry-run 也知道已經送過
    assert nj.actionable_job(NOW, db=db, settings=Settings(), dry_run=True).messages == []


def test_review_only_board_is_silent_by_default(db, game, setup):
    _, acts = _board(db, game, setup)
    n_actionable = sum(o["status_group"] == P.ACTIONABLE for o in acts)
    c = FakeClient()
    rep = nj.actionable_job(NOW, db=db, client=c, settings=ActionableOnly())
    assert rep.candidates == n_actionable
    assert len(notifications(db, "actionable")) == n_actionable and len(c.sent) == (1 if n_actionable else 0)


def test_actionable_failure_retries_up_to_limit(db, game, setup):
    _board(db, game, setup)
    bad = FakeClient(fail=True)
    for i in range(nj.MAX_ATTEMPTS):
        assert actionable(db, NOW + timedelta(seconds=i), bad).failed == 1
    assert actionable(db, NOW + timedelta(seconds=9), bad).failed == 0            # 重試次數用完
    rows = notifications(db, "actionable")
    assert {r["status"] for r in rows} == {"failed"} and {r["attempts"] for r in rows} == {nj.MAX_ATTEMPTS}
    assert all("HTTP 500" in r["last_error"] for r in rows)


def test_stale_board_is_not_notified(db, game, setup):
    _board(db, game, setup)
    c = FakeClient()
    rep = actionable(db, NOW + P.MATERIALIZATION_STALE_AFTER + timedelta(minutes=1), c)
    assert rep.candidates == 0 and not c.sent and notifications(db) == []


def test_multiple_users_require_explicit_target(db, game, setup):
    _board(db, game, setup)
    user(db, "second@example.com")
    rep = actionable(db, NOW, FakeClient())
    assert "TELEGRAM_NOTIFY_USER_ID" in rep.note and notifications(db) == []


def test_daily_summary_once_per_day(db, game, setup):
    _ready(db, game, setup)                                    # 有 2026-10-22（台灣）比賽與預測
    c = FakeClient()
    rep = nj.daily_summary_job(NOW, db=db, client=c, settings=Settings(), day=date(2026, 10, 22))
    assert rep.sent == 1 and rep.candidates == 1 and "BOS" in c.sent[0] and "主勝" in c.sent[0]
    again = nj.daily_summary_job(NOW + timedelta(hours=1), db=db, client=c, settings=Settings(), day=date(2026, 10, 22))
    assert again.note == "already_sent" and len(c.sent) == 1
    empty = nj.daily_summary_job(NOW, db=db, client=c, settings=Settings(), day=date(2026, 11, 30))
    assert empty.note == "no_games" and len(c.sent) == 1


def test_system_alert_state_persists_in_db(db):
    nj.SYSTEM_MEMORY.update(fails=None, since=None, sent_at=None)
    c = FakeClient()
    fails = {"heartbeat/decision_board": "過期"}
    kw = dict(db=db, client=c, settings=Settings(), ignore_grace=True)
    assert nj.system_alert_job(NOW, failures=fails, **kw).sent == 1
    nj.SYSTEM_MEMORY.update(fails=None, since=None, sent_at=None)      # 模擬排程器重啟：只剩資料庫狀態
    assert nj.system_alert_job(NOW + timedelta(minutes=15), failures=fails, **kw).note == "unchanged"
    assert nj.system_alert_job(NOW + timedelta(minutes=30), failures={}, **kw).sent == 1
    [a, b] = notifications(db, "system_alert")
    assert a["payload"]["fails"] == ["heartbeat/decision_board"] and b["payload"]["fails"] == []
    assert a["status"] == b["status"] == "sent" and "系統已恢復" in b["message"]
