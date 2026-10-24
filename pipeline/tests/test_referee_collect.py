"""Phase E.2 referees-v1：解析 / 球隊對應 / 內容版本化 / as-of 觀測時間 / 賽事比對 / 閒置不抓 / 失敗不寫。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from core.jobs import referee_collect as rc
from test_source_access import HEADER, page

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 14, 30, tzinfo=UTC)          # 10:30 ET
ROWS = ("<tr><td>Golden State @ L.A. Lakers</td><td>James Capers (#19)</td><td>Mousa Dagher (#28)</td>"
        "<td>Scott Twardoski (#52)</td><td></td></tr>"
        "<tr><td>Sacramento @ LA Clippers</td><td>Mark Lindsay (#29)</td><td>Ray Acosta (#54)</td>"
        "<td>Brett Nansel (#44)</td><td>Alt Person (#99)</td></tr>")


def html(rows=ROWS, day="October 21, 2026"):
    return page(rows).replace("October 15, 2026", day)


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class Getter:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def __call__(self, url, headers, timeout):
        self.calls += 1
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


# ------------------------------- 純邏輯 ------------------------------- #

def test_city_labels_cover_all_30_teams_and_la_variants():
    assert len(set(rc.CITY_TO_ABBR.values())) == 30
    assert rc.team_abbr("L.A. Lakers") == "LAL" and rc.team_abbr("LA  Clippers") == "LAC"
    assert rc.team_abbr("Golden State") == "GSW" and rc.team_abbr("Seattle") is None


def test_officials_parse_roles_numbers_and_skip_empty_alternate():
    row = {"crew_chief": "Nick Buchert (#3)", "referee": "Nate Green (#41)", "umpire": "Sha'Rae Mitchell (#98)",
           "alternate": ""}
    assert rc.officials(row) == [{"role": "crew_chief", "name": "Nick Buchert", "number": 3},
                                 {"role": "referee", "name": "Nate Green", "number": 41},
                                 {"role": "umpire", "name": "Sha'Rae Mitchell", "number": 98}]
    assert rc.parse_official("No Number") == {"name": "No Number", "number": None}


def test_content_hash_ignores_row_order_and_observed_time():
    a = {"report_date_et": "2026-10-21", "observed_at": "x", "assignments": [{"game": "B"}, {"game": "A"}]}
    b = {**a, "observed_at": "y", "assignments": [{"game": "A"}, {"game": "B"}]}
    assert rc.content_hash(a) == rc.content_hash(b)
    assert rc.content_hash(a) != rc.content_hash({**a, "assignments": [{"game": "A", "referee": "swap"}]})


def test_match_row_statuses():
    d = datetime(2026, 10, 21).date()
    idx = {("GSW", "LAL"): [(7, d)], ("SAC", "LAC"): [(8, d), (9, d - timedelta(days=1))]}
    assert rc.match_row(idx, "Golden State @ L.A. Lakers") == (7, "matched", d)
    assert rc.match_row(idx, "Sacramento @ LA Clippers") == (None, "ambiguous", None)   # 連兩天同對戰：不猜
    assert rc.match_row(idx, "Boston @ Miami") == (None, "no_game", None)
    assert rc.match_row(idx, "Seattle @ Miami") == (None, "unknown_team", None)


# ------------------------------- DB（暫存 schema） ------------------------------- #

def add_game(db, seeded_ids, home, away, when, status="scheduled"):
    with db.cursor() as cur:
        return db.upsert_game(cur, nba_game_id=f"00226{home}{away}", season="2026-27", season_stage="regular",
                              date_utc=when, home_team_id=seeded_ids[home], away_team_id=seeded_ids[away],
                              status=status)


@pytest.fixture()
def teams(db):
    ids = {}
    with db.cursor() as cur:
        for i, abbr in enumerate(("LAL", "GSW", "LAC", "SAC"), start=1):
            ids[abbr] = db.upsert_team(cur, nba_team_id=1610612700 + i, abbr=abbr, name=abbr)
    return ids


def rows(db, sql, params=()):
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def job(db, get, now=NOW, **kw):
    return rc.collect_referees_job(now, db=db, get=get, clock=lambda: now + timedelta(seconds=1), **kw)


def test_idle_day_makes_no_request(db, teams):
    g = Getter(Resp(html()))
    assert job(db, g).state == "idle" and g.calls == 0


def test_store_match_dedupe_and_late_swap(db, teams):
    tip = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)          # 22:00 ET 10/21
    lal = add_game(db, teams, "LAL", "GSW", tip)
    lac = add_game(db, teams, "LAC", "SAC", tip + timedelta(minutes=30))
    rep = job(db, Getter(Resp(html())))
    assert rep.state == "ready" and rep.new_report and rep.matched == 2 and rep.unmatched == []
    [r1] = rows(db, "SELECT * FROM referee_assignment_reports")
    assert r1["observed_at"] == NOW + timedelta(seconds=1) and str(r1["report_date_et"]) == "2026-10-21"
    a = {x["game_id"]: x for x in rows(db, "SELECT * FROM referee_assignments")}
    assert set(a) == {lal, lac} and a[lac]["officials"][-1] == {"role": "alternate", "name": "Alt Person", "number": 99}
    # 30 分鐘後內容相同 → 不新增，只推進 last_seen_at
    later = NOW + timedelta(minutes=30)
    assert not job(db, Getter(Resp(html())), now=later).new_report
    [r1b] = rows(db, "SELECT * FROM referee_assignment_reports")
    assert r1b["id"] == r1["id"] and r1b["last_seen_at"] == later + timedelta(seconds=1)
    assert r1b["observed_at"] == r1["observed_at"]
    # 賽前換人 → 新 report，舊的保留不改寫
    swapped = html(ROWS.replace("Mousa Dagher (#28)", "Someone Else (#77)"))
    assert job(db, Getter(Resp(swapped)), now=NOW + timedelta(hours=2)).new_report
    reps = rows(db, "SELECT id, observed_at FROM referee_assignment_reports ORDER BY observed_at")
    assert len(reps) == 2 and len(rows(db, "SELECT 1 FROM referee_assignments")) == 4
    # as-of：換人之前的時點只看得到第一份
    asof = rows(db, """SELECT a.officials FROM referee_assignments a JOIN referee_assignment_reports r ON r.id = a.report_id
                        WHERE a.game_id = %s AND r.observed_at <= %s ORDER BY r.observed_at DESC LIMIT 1""",
                (lal, NOW + timedelta(hours=1)))
    assert asof[0]["officials"][1]["name"] == "Mousa Dagher"


def test_unsynced_game_is_rematched_later_without_new_report(db, teams):
    tip = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)
    add_game(db, teams, "LAL", "GSW", tip)                  # LAC 那場賽程尚未同步
    rep = job(db, Getter(Resp(html())))
    assert rep.matched == 1 and rep.unmatched == ["Sacramento @ LA Clippers:no_game"]
    lac = add_game(db, teams, "LAC", "SAC", tip)
    rep2 = job(db, Getter(Resp(html())), now=NOW + timedelta(minutes=30))
    assert not rep2.new_report and rep2.rematched == 1
    assert {r["game_id"] for r in rows(db, "SELECT game_id FROM referee_assignments")} >= {lac}


def test_page_dated_today_but_listing_previous_slate(db, teams):
    """2026-10-24 實測：09:00 ET 前頁面仍是前一輪對戰，卻標成今天 → 比對到前一天、state = previous_slate。"""
    prev_tip = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)     # 10/20 22:00 ET（已開打）
    lal = add_game(db, teams, "LAL", "GSW", prev_tip, status="final")
    lac = add_game(db, teams, "LAC", "SAC", prev_tip)
    add_game(db, teams, "GSW", "LAC", datetime(2026, 10, 21, 18, 0, tzinfo=UTC))  # 10/21 ET 14:00 的另一場：讓 job 非 idle
    rep = job(db, Getter(Resp(html())), now=datetime(2026, 10, 21, 4, 50, tzinfo=UTC))   # 00:50 ET 10/21
    assert rep.state == "previous_slate" and rep.matched == 2 and rep.slate_days == {"2026-10-20"}
    assert {r["game_id"] for r in rows(db, "SELECT game_id FROM referee_assignments")} == {lal, lac}
    [r] = rows(db, "SELECT report_date_et, observed_at FROM referee_assignment_reports")
    assert str(r["report_date_et"]) == "2026-10-21"                                # 頁面標示照實保存
    assert r["observed_at"] > prev_tip                                              # 開賽後才觀測 → as-of 自然排除


@pytest.mark.parametrize("resp", [Resp("", 403), requests.ConnectionError("blocked"),
                                  Resp("<html>challenge</html>")])
def test_blocked_or_changed_page_writes_nothing(db, teams, resp):
    add_game(db, teams, "LAL", "GSW", datetime(2026, 10, 22, 2, 0, tzinfo=UTC))
    g = Getter(resp)
    rep = job(db, g)
    assert rep.state == "unavailable" and g.calls == 1                  # 不重試
    assert rows(db, "SELECT 1 FROM referee_assignment_reports") == []


def test_unpublished_and_stale_pages(db, teams):
    add_game(db, teams, "LAL", "GSW", datetime(2026, 10, 22, 2, 0, tzinfo=UTC))
    assert job(db, Getter(Resp(html(rows="")))).state == "waiting"
    assert rows(db, "SELECT 1 FROM referee_assignment_reports") == []    # 空表不寫
    rep = job(db, Getter(Resp(html(day="October 20, 2026"))))
    assert rep.state == "stale" and rep.report_date_et == "2026-10-20"   # 昨天的頁面：照實記錄日期，不冒充今天


def test_new_tables_have_rls(db):
    got = rows(db, "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
                   "('referee_assignment_reports', 'referee_assignments') AND relnamespace = current_schema()::regnamespace")
    assert len(got) == 2 and all(r["relrowsecurity"] for r in got)


def test_header_constant_matches_parser():
    assert "Crew Chief" in HEADER
