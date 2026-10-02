"""傷病快照解析、as-of 還原與「球員消失」語意（純邏輯，不連 DB）"""
from datetime import date, datetime, timedelta

from core import injury_asof as ia
from core import injury_history as ih
from core.timeutil import UTC


def utc(*a):
    return datetime(*a, tzinfo=UTC)


TEAMS = ih.TeamDirectory.from_rows([
    {"id": 1, "abbr": "MIL", "name": "Milwaukee Bucks"}, {"id": 2, "abbr": "MEM", "name": "Memphis Grizzlies"},
    {"id": 3, "abbr": "LAC", "name": "Los Angeles Clippers"}, {"id": 4, "abbr": "DEN", "name": "Denver Nuggets"}])
PLAYERS = ih.PlayerDirectory.from_rows([
    {"id": 10, "name": "Giannis Antetokounmpo", "team_id": 1}, {"id": 11, "name": "Damian Lillard", "team_id": 1},
    {"id": 20, "name": "Ja Morant", "team_id": 2}, {"id": 21, "name": "Jaren Jackson Jr.", "team_id": 2}])


def row(team, name, status, reason="x", matchup="MIL@MEM", gdate="02/15/2024"):
    return {"game_date": gdate, "matchup": matchup, "team": team, "player_name": name, "status": status,
            "reason": reason}


def nys(team, matchup="MIL@MEM", gdate="02/15/2024"):
    return {"game_date": gdate, "matchup": matchup, "team": team, "player_name": float("nan"),
            "status": float("nan"), "reason": "NOT YET SUBMITTED"}


def snapshot(rid, when, rows):
    parsed = ih.parse_report(rows, when, TEAMS, PLAYERS)
    rep = {"id": rid, "report_time_utc": when, "coverage": parsed.coverage}
    ent = [{"report_id": rid, "game_date": e.game_date, "team_id": e.team_id, "player_id": e.player_id,
            "player_name": e.player_name, "status": e.status, "reason": e.reason} for e in parsed.entries]
    return rep, ent


def index(*snaps):
    reps, ents = [s[0] for s in snaps], [e for s in snaps for e in s[1]]
    return ia.build_index(reps, ents, {1: "MIL", 2: "MEM", 3: "LAC", 4: "DEN"})


TIP = utc(2024, 2, 16, 1, 0)            # 2024-02-15 20:00 ET
D = date(2024, 2, 15)


# ---------------- 解析 ---------------- #

def test_parse_coverage_nys_and_implied():
    rows = [row("Milwaukee Bucks", "Antetokounmpo, Giannis", "Questionable"),
            nys("Memphis Grizzlies")]
    p = ih.parse_report(rows, utc(2024, 2, 15, 22), TEAMS, PLAYERS)
    cov = p.coverage["2024-02-15"]
    assert cov["MIL"] == {"n": 1, "nys": False, "implied": False}
    assert cov["MEM"] == {"n": 0, "nys": True, "implied": False}
    assert len(p.entries) == 1 and p.entries[0].player_id == 10 and p.entries[0].team_id == 1


def test_parse_team_with_no_rows_is_implied_submitted_empty():
    p = ih.parse_report([row("Milwaukee Bucks", "Lillard, Damian", "Out")], utc(2024, 2, 15, 22), TEAMS, PLAYERS)
    assert p.coverage["2024-02-15"]["MEM"] == {"n": 0, "nys": False, "implied": True}


def test_parse_unmatched_player_kept_with_null_id_and_counted():
    p = ih.parse_report([row("Milwaukee Bucks", "Nobody, Unknown", "Out")], utc(2024, 2, 15, 22), TEAMS, PLAYERS)
    assert p.n_unmatched == 1 and p.entries[0].player_id is None and p.entries[0].player_name == "Nobody, Unknown"


def test_parse_name_normalisation_and_la_clippers_alias():
    p = ih.parse_report([row("Memphis Grizzlies", "Jackson Jr., Jaren", "Out"),
                         row("LA Clippers", "Whoever, Some", "Out", matchup="LAC@DEN")],
                        utc(2024, 2, 15, 22), TEAMS, PLAYERS)
    assert p.entries[0].player_id == 21
    assert p.entries[1].team_id == 3          # 'LA Clippers' → LAC


def test_parse_two_game_dates_in_one_report():
    rows = [row("Milwaukee Bucks", "Lillard, Damian", "Out", gdate="02/15/2024"),
            row("Milwaukee Bucks", "Lillard, Damian", "Probable", gdate="02/16/2024", matchup="MIL@DEN")]
    p = ih.parse_report(rows, utc(2024, 2, 15, 22), TEAMS, PLAYERS)
    assert p.game_dates == ["2024-02-15", "2024-02-16"] and len(p.entries) == 2


# ---------------- as-of 還原 ---------------- #

def test_asof_restores_last_status_before_tip_and_ignores_later_reports():
    r1 = snapshot(1, utc(2024, 2, 15, 22), [row("Milwaukee Bucks", "Antetokounmpo, Giannis", "Questionable")])
    r2 = snapshot(2, utc(2024, 2, 16, 0), [row("Milwaukee Bucks", "Antetokounmpo, Giannis", "Out")])
    r3 = snapshot(3, utc(2024, 2, 16, 2), [row("Milwaukee Bucks", "Antetokounmpo, Giannis", "Available")])  # 開賽後
    idx = index(r1, r2, r3)
    st = ia.team_state_asof(idx, team_abbr="MIL", game_date=D, cutoff_utc=TIP)
    assert st.known and st.report_id == 2 and st.status_of(10) == "Out"
    early = ia.team_state_asof(idx, team_abbr="MIL", game_date=D, cutoff_utc=utc(2024, 2, 15, 23))
    assert early.report_id == 1 and early.status_of(10) == "Questionable"


def test_disappeared_player_is_not_listed_not_permanently_out():
    r1 = snapshot(1, utc(2024, 2, 15, 22), [row("Milwaukee Bucks", "Lillard, Damian", "Out"),
                                             row("Memphis Grizzlies", "Morant, Ja", "Out")])
    # r2：Lillard 不再出現；MIL 仍有別人被列 → 該隊已申報
    r2 = snapshot(2, utc(2024, 2, 16, 0), [row("Milwaukee Bucks", "Antetokounmpo, Giannis", "Probable"),
                                            row("Memphis Grizzlies", "Morant, Ja", "Out")])
    idx = index(r1, r2)
    st = ia.team_state_asof(idx, team_abbr="MIL", game_date=D, cutoff_utc=TIP)
    assert st.known and st.report_id == 2
    assert st.status_of(11) == ia.NOT_LISTED and st.absent_prob(11) == 0.0
    assert st.status_of(10) == "Probable"
    # 在 r2 之前仍是 Out（歷史還原正確）
    before = ia.team_state_asof(idx, team_abbr="MIL", game_date=D, cutoff_utc=utc(2024, 2, 15, 23, 30))
    assert before.status_of(11) == "Out" and before.absent_prob(11) == 1.0


def test_team_with_no_rows_after_being_listed_is_available_via_implied_empty():
    r1 = snapshot(1, utc(2024, 2, 15, 22), [row("Milwaukee Bucks", "Lillard, Damian", "Out"),
                                             row("Memphis Grizzlies", "Morant, Ja", "Out")])
    r2 = snapshot(2, utc(2024, 2, 16, 0), [row("Memphis Grizzlies", "Morant, Ja", "Out")])   # MIL 一列都沒有
    st = ia.team_state_asof(index(r1, r2), team_abbr="MIL", game_date=D, cutoff_utc=TIP)
    assert st.known and st.implied_empty and st.status_of(11) == ia.NOT_LISTED


def test_not_yet_submitted_does_not_clear_previous_state():
    r1 = snapshot(1, utc(2024, 2, 15, 22), [row("Milwaukee Bucks", "Lillard, Damian", "Out"),
                                             row("Memphis Grizzlies", "Morant, Ja", "Out")])
    r2 = snapshot(2, utc(2024, 2, 16, 0), [nys("Milwaukee Bucks"), row("Memphis Grizzlies", "Morant, Ja", "Out")])
    st = ia.team_state_asof(index(r1, r2), team_abbr="MIL", game_date=D, cutoff_utc=TIP)
    assert st.known and st.report_id == 1 and st.status_of(11) == "Out" and st.skipped_unsubmitted == 1


def test_unknown_when_no_report_covers_team_game():
    r1 = snapshot(1, utc(2024, 2, 15, 22), [row("Denver Nuggets", "Whoever, X", "Out", matchup="LAC@DEN")])
    st = ia.team_state_asof(index(r1), team_abbr="MIL", game_date=D, cutoff_utc=TIP)
    assert not st.known and st.status_of(10) == ia.UNKNOWN and st.absent_prob(10) is None
    only_nys = snapshot(2, utc(2024, 2, 15, 23), [nys("Milwaukee Bucks"), nys("Memphis Grizzlies")])
    assert not ia.team_state_asof(index(only_nys), team_abbr="MIL", game_date=D, cutoff_utc=TIP).known


def test_out_for_today_does_not_carry_into_tomorrows_game():
    r1 = snapshot(1, utc(2024, 2, 15, 22), [row("Milwaukee Bucks", "Lillard, Damian", "Out")])
    r2 = snapshot(2, utc(2024, 2, 16, 14), [row("Milwaukee Bucks", "Antetokounmpo, Giannis", "Probable",
                                                 gdate="02/17/2024", matchup="MIL@DEN")])
    idx = index(r1, r2)
    nxt = ia.team_state_asof(idx, team_abbr="MIL", game_date=date(2024, 2, 17), cutoff_utc=utc(2024, 2, 17, 23))
    assert nxt.report_id == 2 and nxt.status_of(11) == ia.NOT_LISTED


def test_report_exactly_at_cutoff_is_excluded_strictly_before():
    r = snapshot(1, TIP, [row("Milwaukee Bucks", "Lillard, Damian", "Out")])
    assert not ia.team_state_asof(index(r), team_abbr="MIL", game_date=D, cutoff_utc=TIP).known
    assert ia.team_state_asof(index(r), team_abbr="MIL", game_date=D, cutoff_utc=TIP + timedelta(seconds=1)).known


def test_decision_offset_uses_earlier_report():
    r1 = snapshot(1, utc(2024, 2, 15, 22), [row("Milwaukee Bucks", "Lillard, Damian", "Questionable")])
    r2 = snapshot(2, utc(2024, 2, 16, 0, 30), [row("Milwaukee Bucks", "Lillard, Damian", "Out")])
    idx = index(r1, r2)
    assert ia.team_state_asof(idx, team_abbr="MIL", game_date=D, cutoff_utc=TIP).status_of(11) == "Out"
    assert ia.team_state_asof(idx, team_abbr="MIL", game_date=D,
                              cutoff_utc=TIP - timedelta(minutes=60)).status_of(11) == "Questionable"


def test_lookback_limit_ignores_ancient_reports():
    r = snapshot(1, utc(2024, 2, 1, 22), [row("Milwaukee Bucks", "Lillard, Damian", "Out")])
    assert not ia.team_state_asof(index(r), team_abbr="MIL", game_date=D, cutoff_utc=TIP).known


def test_older_report_added_later_does_not_override_newer_state():
    """snapshot 以 report_time 為準：不論寫入順序，as-of 結果相同（補抓較舊報告不會覆蓋較新狀態）。"""
    old = snapshot(7, utc(2024, 2, 15, 22), [row("Milwaukee Bucks", "Lillard, Damian", "Questionable")])
    new = snapshot(3, utc(2024, 2, 16, 0), [row("Milwaukee Bucks", "Lillard, Damian", "Out")])
    a = ia.team_state_asof(index(old, new), team_abbr="MIL", game_date=D, cutoff_utc=TIP)
    b = ia.team_state_asof(index(new, old), team_abbr="MIL", game_date=D, cutoff_utc=TIP)
    assert a.status_of(11) == b.status_of(11) == "Out" and a.report_id == b.report_id == 3


def test_status_probability_priors_are_monotonic():
    p = ia.P_ABSENT
    assert p["Out"] > p["Doubtful"] > p["Questionable"] > p["Probable"] > p["Available"] == p[ia.NOT_LISTED] == 0.0


def test_unmapped_team_name_never_becomes_implied_healthy():
    """隊名對不到的列：該場「推定無人列入」的一方改標無資訊，避免把可能存在的傷兵當成全員健康。"""
    rows = [row("Milwaukee Bucks", "Lillard, Damian", "Out"),
            row("Mystery Franchise", "Someone, Else", "Out")]          # MEM 的列，但隊名無法對應
    p = ih.parse_report(rows, utc(2024, 2, 15, 22), TEAMS, PLAYERS)
    assert p.coverage["2024-02-15"]["MEM"]["nys"] is True and p.coverage["2024-02-15"]["MEM"]["implied"] is False
    assert p.coverage["2024-02-15"]["MIL"]["nys"] is False
    idx = index(snapshot(1, utc(2024, 2, 15, 22), rows))
    assert not ia.team_state_asof(idx, team_abbr="MEM", game_date=D, cutoff_utc=TIP).known
