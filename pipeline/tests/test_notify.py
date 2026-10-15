"""Phase E.1 notify-v1（純邏輯，不連 DB、不打網路）：Telegram 傳送 / token 不外洩 / 訊息格式 / 系統告警去重。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from core.notify import job as nj
from core.notify import messages as M
from core.notify.telegram import MAX_LEN, TelegramClient, TelegramError, split_message
from core.production import readiness as R

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 4, 0, tzinfo=UTC)
TOKEN = "123456:SECRET-token-value"


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {"ok": True}

    def json(self):
        return self._body


class Recorder:
    def __init__(self, resp=None, exc=None):
        self.calls, self.resp, self.exc = [], resp or FakeResp(), exc

    def __call__(self, url, json, timeout):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return self.resp


class FakeClient:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, text):
        if self.fail:
            raise TelegramError("HTTP 500: boom")
        self.sent.append(text)
        return 1


class DownDB:
    """資料庫連不上：任何 cursor() 都丟例外。"""
    @contextmanager
    def cursor(self):
        raise OSError("db down")
        yield  # pragma: no cover


@pytest.fixture(autouse=True)
def _reset_memory():
    nj.SYSTEM_MEMORY.update(fails=None, since=None, sent_at=None)
    yield


# ------------------------------- telegram ------------------------------- #

def test_split_message_respects_limit_and_keeps_lines():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= MAX_LEN for p in parts)
    assert "\n".join(parts) == text
    assert all(len(p) <= 10 for p in split_message("y" * 25, limit=10))


def test_send_posts_html_message():
    rec = Recorder()
    assert TelegramClient(TOKEN, "42", post=rec).send("<b>hi</b>") == 1
    url, body = rec.calls[0]
    assert url.endswith("/sendMessage") and body == {"chat_id": "42", "text": "<b>hi</b>", "parse_mode": "HTML",
                                                      "disable_web_page_preview": True}


def test_errors_never_contain_token():
    rec = Recorder(exc=requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage failed"))
    with pytest.raises(TelegramError) as e:
        TelegramClient(TOKEN, "42", post=rec).send("x")
    assert TOKEN not in str(e.value) and "network" in str(e.value)
    rec = Recorder(resp=FakeResp(401, {"ok": False, "description": f"Unauthorized {TOKEN}"}))
    with pytest.raises(TelegramError) as e:
        TelegramClient(TOKEN, "42", post=rec).send("x")
    assert TOKEN not in str(e.value) and "HTTP 401" in str(e.value)


# ------------------------------- messages ------------------------------- #

OPP = {"game_id": 7, "market": "spread", "market_type": "spread", "side": "home", "display_line": -3.5,
       "bookmaker": "twsport", "decimal_odds": 1.75, "ev_per_unit": 0.042, "model_prob": 0.61,
       "fair_no_vig_prob": 0.552, "suggested_stake_amount": 800.0, "max_additional_stake_amount": 1200.0,
       "date_utc": datetime(2026, 10, 21, 23, 30, tzinfo=UTC), "home_abbr": "NYK", "home_name_zh": "尼克",
       "away_abbr": "BOS", "away_name_zh": "<塞爾提克>"}


def test_actionable_message_content_and_disclaimer():
    text = M.format_actionable(date(2026, 10, 22), [OPP, {**OPP, "side": "away", "display_line": 3.5}],
                               "https://example.test/")
    assert "BOS @ NYK" in text and "10/22 07:30" in text
    assert "[ACTIONABLE] 讓分 尼克 -3.5 @ 1.75｜EV +4.2%" in text and "&lt;塞爾提克&gt; +3.5" in text
    assert "理論金額 800（剩餘上限 1,200）" in text and "https://example.test/decision" in text
    assert M.DISCLAIMER in text and "推薦" not in text
    review = M.format_actionable(date(2026, 10, 22), [{**OPP, "status_group": "review",
                                                       "warnings": ["data_quality:season_opener", "over_theoretical_target"]}])
    assert "[REVIEW 需檢視]" in review and "⚠ 資料品質：season_opener" in review and "over_theoretical" not in review


def test_daily_summary_handles_missing_prediction_and_flags():
    games = [{"id": 1, "date_utc": datetime(2026, 10, 21, 23, 0, tzinfo=UTC), "home_abbr": "NYK", "away_abbr": "BOS",
              "home_win_prob": 0.583, "pred_margin": 3.14, "pred_total": 221.5, "quality_flags": ["season_opener"]},
             {"id": 2, "date_utc": datetime(2026, 10, 22, 2, 0, tzinfo=UTC), "home_abbr": "LAL", "away_abbr": "GSW"}]
    text = M.format_daily_summary(date(2026, 10, 22), games)
    assert "共 2 場" in text and "07:00 <b>BOS @ NYK</b>　主勝 58.3%｜分差(主−客) +3.1｜總分 221.5　⚠ season_opener" in text
    assert "10:00 <b>GSW @ LAL</b>　尚無預測" in text and M.DISCLAIMER in text


def test_opportunity_key_changes_with_quote_not_with_amount():
    k = nj.opportunity_key(1, date(2026, 10, 22), OPP)
    assert k == nj.opportunity_key(1, date(2026, 10, 22), {**OPP, "suggested_stake_amount": 1.0, "ev_per_unit": 0.5})
    assert k != nj.opportunity_key(1, date(2026, 10, 22), {**OPP, "decimal_odds": 1.8})
    assert k != nj.opportunity_key(1, date(2026, 10, 22), {**OPP, "display_line": -4.5})


def test_classify_notify():
    assert R.classify_notify(False, False, None)[0] == R.WAITING
    assert R.classify_notify(True, False, True)[0] == R.WARN
    assert R.classify_notify(True, True, False)[0] == R.WARN
    assert R.classify_notify(True, True, True)[0] == R.PASS


# --------------------- jobs without Telegram configured --------------------- #

class Unset:
    telegram_bot_token = telegram_chat_id = telegram_notify_user_id = notify_site_url = None
    telegram_notify_include_review = False


def test_unconfigured_jobs_do_not_touch_db():
    for fn in (nj.actionable_job, nj.daily_summary_job):
        assert fn(NOW, db=DownDB(), settings=Unset()).note == "not_configured"
    assert nj.system_alert_job(NOW, db=DownDB(), settings=Unset(), failures={}).note == "not_configured"


# --------------------------- system alerts (DB down) --------------------------- #

def sysjob(client, now, failures):
    return nj.system_alert_job(now, db=DownDB(), client=client, settings=Unset(), failures=failures, ignore_grace=True)


def test_system_alert_new_unchanged_reminder_recovered_with_db_down():
    c = FakeClient()
    assert sysjob(c, NOW, {}).note == "unchanged" and not c.sent                      # 一開始就正常 → 不送
    fails = {"database/reachable": "連線失敗", "heartbeat/decision_board": "過期 30 分鐘"}
    assert sysjob(c, NOW, fails).sent == 1 and "系統異常" in c.sent[-1] and "🆕" in c.sent[-1]
    assert sysjob(c, NOW + timedelta(minutes=15), {**fails, "heartbeat/decision_board": "過期 45 分鐘"}).note == "unchanged"
    assert len(c.sent) == 1                                                            # detail 變動不算新告警
    assert sysjob(c, NOW + timedelta(hours=6, minutes=1), fails).sent == 1 and "仍未排除" in c.sent[-1]
    rep = sysjob(c, NOW + timedelta(hours=6, minutes=16), {"database/reachable": "連線失敗"})
    assert rep.sent == 1 and "已恢復：heartbeat/decision_board" in c.sent[-1]
    assert sysjob(c, NOW + timedelta(hours=7), {}).sent == 1 and "系統已恢復" in c.sent[-1]
    assert sysjob(c, NOW + timedelta(hours=20), {}).note == "unchanged"


def test_system_alert_send_failure_is_retried_next_run():
    bad = FakeClient(fail=True)
    fails = {"artifact/CURRENT.json": "missing"}
    assert sysjob(bad, NOW, fails).failed == 1
    good = FakeClient()
    assert sysjob(good, NOW + timedelta(minutes=15), fails).sent == 1     # 上次沒送成功 → 不算已通知


def test_startup_grace_skips_system_alerts(monkeypatch):
    monkeypatch.setattr(nj, "_PROCESS_STARTED", nj.time.monotonic())
    rep = nj.system_alert_job(NOW, db=DownDB(), client=FakeClient(), settings=Unset(), failures={"a/b": "x"})
    assert rep.note == "startup_grace"
