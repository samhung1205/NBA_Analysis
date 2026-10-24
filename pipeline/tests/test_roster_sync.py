"""現役名單同步（roster-sync-v1）：來源解析、完整性、players.team_id 套用、出賽回填不倒退、傷病「消失→Available」不寫到舊東家"""
from datetime import datetime

import pytest

from core.jobs import roster_sync as rs
from core.jobs.daily import NOT_LISTED_REASON, ingest_injury_rows
from core.sources import rosters as src
from core.sources.rosters import RosterEntry as E
from core.timeutil import UTC
from tests.helpers import count, utc

# ---------------- 純邏輯 ---------------- #


def test_parse_player_index_keeps_only_rostered_players():
    raw = {"resultSets": [{"headers": ["PERSON_ID", "PLAYER_LAST_NAME", "PLAYER_FIRST_NAME", "TEAM_ID",
                                       "TEAM_ABBREVIATION", "POSITION", "ROSTER_STATUS"],
                           "rowSet": [[202691, "Thompson", "Klay", 1610612748, "MIA", "G", 1.0],
                                      [1, "Agent", "Free", 0, "", "F", None]]}]}
    assert src.parse_player_index(raw) == [E("MIA", "Klay Thompson", 202691, "G")]


def test_season_for_switches_in_july_et():
    assert src.season_for(datetime(2026, 10, 21, 3, tzinfo=UTC)) == "2026-27"
    assert src.season_for(datetime(2026, 6, 30, 12, tzinfo=UTC)) == "2025-26"
    assert src.season_for(datetime(2027, 1, 5, tzinfo=UTC)) == "2026-27"


def test_completeness_rejects_missing_thin_or_unknown_teams(monkeypatch):
    monkeypatch.setattr(src, "MIN_PER_TEAM", 2)
    full = [E(a, f"{a}{i}", None, None) for a in ("BOS", "NYK") for i in range(2)]
    assert src.completeness_problem(full, {"BOS", "NYK"}) is None
    assert "缺少" in src.completeness_problem(full[:2], {"BOS", "NYK"})
    assert "不足" in src.completeness_problem(full[:3], {"BOS", "NYK"})
    assert "未知" in src.completeness_problem(full + [E("XXX", "a", None, None)], {"BOS", "NYK"})


# ---------------- 需要 DB ---------------- #

@pytest.fixture()
def small_rosters(monkeypatch):
    monkeypatch.setattr(src, "MIN_PER_TEAM", 1)


def team_of(db, name):
    with db.cursor() as cur:
        cur.execute("SELECT t.abbr FROM players p LEFT JOIN teams t ON t.id = p.team_id WHERE p.name = %s", (name,))
        return cur.fetchone()["abbr"]


def nba_roster(*extra):
    # seeded：Brown/Tatum 在 BOS、Jackson 在 NYK。名單：Brown 轉 LAL、Tatum 留 BOS、Jackson 不在名單（離隊）、新秀加入 NYK
    return [E("LAL", "Jaylen Brown", 1627759, "G"), E("BOS", "Jayson Tatum", 1628369, "F"),
            E("NYK", "New Rookie", 1642000, "G"), *extra]


def test_nba_roster_moves_adds_and_releases(seeded, db, small_rosters):
    plan = rs.sync_rosters_job(fetch=lambda: ("nba_playerindex", nba_roster()), now=utc(2026, 10, 21))
    assert [(m[1], m[2], m[3]) for m in plan.moved] == [("Jaylen Brown", "BOS", "LAL")]
    assert [e.name for e in plan.added] == ["New Rookie"]
    assert [(r[1], r[2]) for r in plan.released] == [("Jaren Jackson Jr.", "NYK")]
    assert team_of(db, "Jaylen Brown") == "LAL"
    assert team_of(db, "Jayson Tatum") == "BOS"
    assert team_of(db, "Jaren Jackson Jr.") is None
    assert team_of(db, "New Rookie") == "NYK"
    # 重跑：沒有新的轉隊 / 新增 / 離隊
    again = rs.sync_rosters_job(fetch=lambda: ("nba_playerindex", nba_roster()), now=utc(2026, 10, 22))
    assert (again.moved, again.added, again.released) == ([], [], [])


def test_incomplete_roster_writes_nothing(seeded, db, small_rosters):
    assert rs.sync_rosters_job(fetch=lambda: ("nba_playerindex", nba_roster()[:1])) is None   # 缺 BOS / NYK
    assert team_of(db, "Jaylen Brown") == "BOS"
    assert count(db, "data_sources", "source_key = 'nba_rosters' AND last_status = 'error'") == 1


def test_espn_fallback_matches_by_name_and_never_releases(seeded, db, small_rosters):
    roster = [E("LAL", "Jaylen Brown", None, "G"), E("BOS", "Jayson Tatum", None, "F"),
              E("NYK", "Someone Unknown", None, "G")]
    plan = rs.sync_rosters_job(fetch=lambda: ("espn", roster))
    assert team_of(db, "Jaylen Brown") == "LAL"
    assert team_of(db, "Jaren Jackson Jr.") == "NYK"          # 只有姓名的名單不能斷定離隊
    assert plan.added == [] and [e.name for e in plan.unmatched] == ["Someone Unknown"]


def _play(db, seeded, gid, when, player_name, team):
    with db.cursor() as cur:
        g = db.upsert_game(cur, nba_game_id=gid, season="2025-26", season_stage="regular", date_utc=when,
                           home_team_id=seeded["BOS"], away_team_id=seeded["NYK"], status="final")
        cur.execute("SELECT id FROM players WHERE name = %s", (player_name,))
        db.upsert_player_game_stats(cur, game_id=g, player_id=cur.fetchone()["id"], team_id=seeded[team],
                                    started=1, min=30.0)
        db.refresh_player_current_team(cur)


def test_last_season_box_score_does_not_revert_roster_team(seeded, db, small_rosters):
    _play(db, seeded, "0022501230", utc(2026, 4, 10), "Jaylen Brown", "BOS")      # 上季最後一場在 BOS
    rs.sync_rosters_job(fetch=lambda: ("nba_playerindex", nba_roster()), now=utc(2026, 10, 21))
    with db.cursor() as cur:
        db.refresh_player_current_team(cur)                                          # 修正前：會蓋回 BOS
    assert team_of(db, "Jaylen Brown") == "LAL"
    _play(db, seeded, "0022600005", utc(2026, 10, 23), "Jaylen Brown", "LAL")      # 名單同步後出賽 → 依出賽
    assert team_of(db, "Jaylen Brown") == "LAL"


def _inj(team, name, status, gdate="10/21/2026", matchup="BOS@NYK"):
    return {"game_date": gdate, "matchup": matchup, "team": team, "player_name": name, "status": status,
            "reason": "Knee"}


def test_moved_player_is_not_marked_available_on_former_team(seeded, db, small_rosters):
    with db.cursor() as cur:
        ingest_injury_rows(cur, [_inj("Boston Celtics", "Brown, Jaylen", "Questionable")], utc(2026, 8, 1, 3, 30))
    rs.sync_rosters_job(fetch=lambda: ("nba_playerindex", nba_roster()), now=utc(2026, 10, 20))
    with db.cursor() as cur:
        # BOS 已申報（只列 Tatum），Brown 已轉隊 LAL → 不該在 BOS 寫一列 Available（修正前會寫）
        ingest_injury_rows(cur, [_inj("Boston Celtics", "Tatum, Jayson", "Out")], utc(2026, 10, 20, 23, 45))
        cur.execute("SELECT count(*) AS n FROM injuries i JOIN players p ON p.id = i.player_id "
                    "WHERE p.name = 'Jaylen Brown' AND i.reason = %s", (NOT_LISTED_REASON,))
        assert cur.fetchone()["n"] == 0


def test_player_still_on_team_is_still_marked_available(seeded, db, small_rosters):
    rs.sync_rosters_job(fetch=lambda: ("nba_playerindex", nba_roster()), now=utc(2026, 10, 1))
    with db.cursor() as cur:
        ingest_injury_rows(cur, [_inj("Boston Celtics", "Tatum, Jayson", "Out")], utc(2026, 10, 20, 20))
        ingest_injury_rows(cur, [_inj("Boston Celtics", "Brown, Jaylen", "Out")], utc(2026, 10, 20, 23, 45))
        cur.execute("SELECT i.status, t.abbr FROM injuries i JOIN players p ON p.id = i.player_id "
                    "JOIN teams t ON t.id = i.team_id WHERE p.name = 'Jayson Tatum' ORDER BY i.report_time_utc DESC LIMIT 1")
        assert dict(cur.fetchone()) == {"status": "Available", "abbr": "BOS"}


def test_daily_schedule_runs_before_schedule_sync_and_predictions():
    from core.scheduling import job_specs, next_run_times
    from core.timeutil import TPE

    specs = job_specs()
    t = next_run_times(specs, utc(2026, 10, 22, 2, 0))            # 台灣 10:00
    assert t["roster_sync"] == datetime(2026, 10, 22, 11, 50, tzinfo=TPE)
    assert t["roster_sync"] < t["daily_schedule_scores"] < t["predict_early"]
    assert {s.id: s for s in specs}["roster_sync"].startup_catchup is False   # 重啟不額外打 stats.nba.com


def test_fetch_failure_records_error_heartbeat_and_does_not_raise(seeded, db):
    def boom():
        raise ConnectionError("blocked")
    assert rs.sync_rosters_job(fetch=boom) is None
    assert count(db, "data_sources", "source_key = 'nba_rosters' AND last_status = 'error' AND expected_interval_min = 1440") == 1
    assert team_of(db, "Jaylen Brown") == "BOS"
