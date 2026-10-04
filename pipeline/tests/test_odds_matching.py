"""D.1 game identity reconciliation（純邏輯）"""
from datetime import datetime, timedelta

import pytest

from core.odds.canonical import SourceEvent
from core.odds.matching import CandidateGame, ExistingLink, match_event
from core.odds.team_aliases import ALIAS_INDEX, TEAM_ALIASES, build_alias_index, normalize, resolve_abbr
from core.timeutil import TPE, UTC

TEAM_IDS = {abbr: i + 1 for i, abbr in enumerate(sorted(TEAM_ALIASES))}
T = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)        # 台灣 10:00 / 美東 10-20 22:00


def ev(home, away, when=T, stage="regular", eid="X1"):
    return SourceEvent(source="test", source_event_id=eid, home_name=home, away_name=away, commence_utc=when,
                       stage=stage)


def game(gid, home, away, when=T, status="scheduled"):
    return CandidateGame(gid, TEAM_IDS[home], TEAM_IDS[away], when, status, "regular", f"00226{gid:05d}")


# ---- alias map --------------------------------------------------------------- #

def test_alias_map_covers_30_teams_and_is_unambiguous():
    assert len(TEAM_ALIASES) == 30
    assert set(ALIAS_INDEX.values()) == set(TEAM_ALIASES)
    with pytest.raises(ValueError):
        build_alias_index({"LAC": ("Clippers",), "LAL": ("Clippers",)})


@pytest.mark.parametrize("name,abbr", [
    ("Los Angeles Clippers", "LAC"), ("LA Clippers", "LAC"), ("L.A. Clippers", "LAC"), ("洛杉磯快艇", "LAC"),
    ("los angeles lakers", "LAL"), ("Golden State Warriors", "GSW"), ("金州勇士", "GSW"),
    ("Philadelphia 76ers", "PHI"), ("費城76人", "PHI"), ("費城七六人", "PHI"), ("費城７６人", "PHI"),  # 全形數字
    ("拉斯維加斯王牌\r", None), ("猶他爵士\r", "UTA"), ("  New   York  Knicks ", "NYK"),
    ("Los Angeles", None), ("LA", None), ("洛杉磯", None), ("", None), (None, None),
])
def test_team_alias_resolution(name, abbr):
    assert resolve_abbr(name) == abbr


def test_normalize_strips_control_chars_and_width():
    assert normalize("美國職籃熱身賽\r") == "美國職籃熱身賽"
    assert normalize("ＬＡ  Ｃｌｉｐｐｅｒｓ") == "la clippers"


# ---- matching ---------------------------------------------------------------- #

def test_exact_match_by_teams_and_time():
    r = match_event(ev("Los Angeles Clippers", "Golden State Warriors", T + timedelta(minutes=10)),
                    [game(1, "LAC", "GSW"), game(2, "LAL", "GSW")], TEAM_IDS)
    assert (r.status, r.game_id, r.method, r.time_delta_min) == ("matched", 1, "teams_time", -10.0)


def test_timezone_conversion_taipei_vs_utc():
    """台彩 tsstart 為 +08:00：2026-10-21T10:00+08:00 = 02:00Z。"""
    tpe = datetime(2026, 10, 21, 10, 0, tzinfo=TPE)
    r = match_event(ev("洛杉磯快艇", "金州勇士", tpe), [game(1, "LAC", "GSW")], TEAM_IDS)
    assert r.status == "matched" and r.time_delta_min == 0.0
    # 把台灣時間當 UTC（差 8 小時）→ 嚴格視窗外，只能以「改期」對上且會留下 time_delta 供檢查
    naive_wrong = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    r2 = match_event(ev("洛杉磯快艇", "金州勇士", naive_wrong), [game(1, "LAC", "GSW")], TEAM_IDS)
    assert r2.method == "rescheduled" and r2.time_delta_min == -480.0 and r2.alert
    assert not r.alert


def test_ambiguous_match_is_not_guessed():
    g = [game(1, "LAC", "GSW", T), game(2, "LAC", "GSW", T + timedelta(hours=2))]
    r = match_event(ev("LA Clippers", "Golden State Warriors", T + timedelta(hours=1)), g, TEAM_IDS)
    assert r.status == "ambiguous" and r.game_id is None and len(r.candidates) == 2 and r.alert


def test_home_away_reversal_is_rejected():
    r = match_event(ev("Golden State Warriors", "Los Angeles Clippers"), [game(1, "LAC", "GSW")], TEAM_IDS)
    assert r.status == "rejected" and r.reason == "home_away_reversed" and r.game_id is None and r.alert


def test_rescheduled_game_matched_within_wide_window_with_delta():
    g = [game(1, "NYK", "PHI", T + timedelta(hours=24))]          # 改期到隔天
    r = match_event(ev("New York Knicks", "Philadelphia 76ers"), g, TEAM_IDS)
    assert r.status == "matched" and r.method == "rescheduled" and r.time_delta_min == 1440.0
    # 已結束的比賽不會被「改期」對上
    g2 = [game(1, "NYK", "PHI", T - timedelta(hours=24), status="final")]
    assert match_event(ev("New York Knicks", "Philadelphia 76ers"), g2, TEAM_IDS).status == "unmatched"


def test_postponed_game_rejected():
    r = match_event(ev("New York Knicks", "Philadelphia 76ers"), [game(1, "NYK", "PHI", status="postponed")], TEAM_IDS)
    assert r.status == "rejected" and r.reason == "game_postponed"


def test_unknown_team_and_unmatched_reasons():
    r = match_event(ev("拉斯維加斯王牌", "金州瓦爾基里"), [], TEAM_IDS)
    assert r.status == "unmatched" and r.reason == "unknown_team" and r.alert
    pre = match_event(ev("洛杉磯快艇", "金州勇士", stage="preseason"), [], TEAM_IDS)
    assert pre.reason == "preseason_not_tracked" and not pre.alert
    far = match_event(ev("洛杉磯快艇", "金州勇士"), [], TEAM_IDS, schedule_horizon_utc=T - timedelta(days=1))
    assert far.reason == "beyond_schedule_horizon" and not far.alert
    none = match_event(ev("洛杉磯快艇", "金州勇士"), [], TEAM_IDS, schedule_horizon_utc=T + timedelta(days=1))
    assert none.reason == "no_candidate" and none.alert
    same = match_event(ev("Lakers", "Los Angeles Lakers"), [], TEAM_IDS)
    assert same.status == "rejected" and same.reason == "same_team"


def test_existing_link_is_reused_and_invalidated_when_inconsistent():
    g = [game(1, "LAC", "GSW", T + timedelta(hours=5)), game(2, "LAC", "GSW", T + timedelta(hours=1))]
    r = match_event(ev("LA Clippers", "Golden State Warriors"), g, TEAM_IDS, existing=ExistingLink(1, "matched"))
    assert r.game_id == 1 and r.method == "link"
    # 既有對應的比賽主客不符 → 不沿用，重新比對
    g2 = [game(1, "GSW", "LAC"), game(2, "LAC", "GSW")]
    r2 = match_event(ev("LA Clippers", "Golden State Warriors"), g2, TEAM_IDS, existing=ExistingLink(1, "matched"))
    assert r2.game_id == 2 and r2.reason == "link_invalidated"
    # 人工對應優先
    r3 = match_event(ev("LA Clippers", "Golden State Warriors"), g, TEAM_IDS,
                     existing=ExistingLink(1, "matched", manual=True))
    assert r3.game_id == 1 and r3.method == "manual"
