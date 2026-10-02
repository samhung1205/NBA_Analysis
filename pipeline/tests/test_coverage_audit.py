"""歷史完整度稽核：覆蓋率計算正確、缺漏可定位、不補造"""
import pytest

from core import injury_history as ih
from core.jobs import coverage_audit as ca
from core.jobs.box_store import store_box_basic
from core.metrics import FORMULA_VERSION
from tests.helpers import BOS, NYK, lineup, make_box, team_raw, utc

SEASON = "2021-22"


def mk_game(db, seeded, gid, when, *, box=None, store=True, home="BOS", away="NYK", fix=None, stage="regular",
            status="final"):
    """建立一場賽事；box 給定且 store=True 就寫入完整 box score。回傳 games.id。"""
    box = box or make_box(gid, when, BOS, NYK)
    q = dict(box["quarters"])
    with db.cursor() as cur:
        game_id = db.upsert_game(cur, nba_game_id=gid, season=SEASON, season_stage=stage, date_utc=when,
                                 home_team_id=seeded[home], away_team_id=seeded[away], status=status,
                                 home_pts=box["home"]["team"]["pts"], away_pts=box["away"]["team"]["pts"], **q)
        if store:
            store_box_basic(cur, {"id": game_id, "home_team_id": seeded[home], "away_team_id": seeded[away]}, box)
        for col, val in (fix or {}).items():                    # 之後才竄改（store_box_basic 會以 box 逐節為準）
            cur.execute(f"UPDATE games SET {col} = %s WHERE id = %s", (val, game_id))
    return game_id


def build(db, seeded):
    """5 場賽事，各有已知缺陷：
       g1 完整                      g2 ESPN 暫存列（無官方 ID、無 box）
       g3 有球隊統計但沒有球員       g4 分鐘加總錯誤 + 先發只有 4 人
       g4b 逐節加總 ≠ 總分（home_q1 被改）
    """
    ids = {}
    ids["g1"] = mk_game(db, seeded, "0022100001", utc(2022, 1, 10, 0))
    ids["g2"] = mk_game(db, seeded, "espn:777", utc(2022, 1, 12, 0), store=False)
    box3 = make_box("0022100003", utc(2022, 1, 14, 0), BOS, NYK, home_players=[], away_players=[])
    ids["g3"] = mk_game(db, seeded, "0022100003", utc(2022, 1, 14, 0), box=box3)
    bad_players = lineup(1)
    bad_players[0] = {**bad_players[0], "started": 0}                                # 只剩 4 位先發
    bad_players[-1] = {**bad_players[-1], "min": bad_players[-1]["min"] + 15.0}      # 分鐘加總 +15
    box4 = make_box("0022100004", utc(2022, 1, 16, 0), BOS, NYK, home_players=bad_players)
    ids["g4"] = mk_game(db, seeded, "0022100004", utc(2022, 1, 16, 0), box=box4)
    ids["g4b"] = mk_game(db, seeded, "0022100005", utc(2022, 1, 18, 0), fix={"home_q1": 99})
    return ids


def ingest_report(db, rows, when):
    with db.cursor() as cur:
        teams, players = ih.load_directories(cur)
    with db.transaction() as cur:
        ih.ingest_snapshot(cur, ih.parse_report(rows, when, teams, players))


def row(team, name, status, gdate, matchup="NYK@BOS"):
    return {"game_date": gdate, "matchup": matchup, "team": team, "player_name": name, "status": status, "reason": "x"}


def test_audit_counts_and_missing_locators_are_exact(seeded, db):
    ids = build(db, seeded)
    # g1（ET 2022-01-09）：兩隊都被報告涵蓋；g3（ET 2022-01-13）：只有 BOS 被涵蓋（NYK 不在報告的賽事裡）
    ingest_report(db, [row("Boston Celtics", "Brown, Jaylen", "Out", "01/09/2022"),
                       row("New York Knicks", "Jackson Jr., Jaren", "Out", "01/09/2022")], utc(2022, 1, 9, 22))
    ingest_report(db, [row("Boston Celtics", "Brown, Jaylen", "Out", "01/13/2022", matchup="LAL@BOS")],
                  utc(2022, 1, 13, 22))
    with db.cursor() as cur:
        res = ca.audit(cur, [SEASON])[SEASON]

    k = lambda key: res["keys"][key]
    assert res["games_final"] == 5
    assert (k("official_id")["have"], k("official_id")["missing"]) == (4, 1)               # g2 是 espn:
    assert k("score_present")["have"] == 5 and k("quarters")["have"] == 5
    assert k("quarter_sum_ok")["have"] == 4 and k("quarter_sum_ok")["missing"] == 1        # g4b
    assert k("h1_present")["have"] == 5
    assert k("team_stats")["have"] == 4                                                    # g2 沒有
    assert k("team_pts_ok")["have"] == 4
    assert k("player_stats")["have"] == 3                                                  # g1、g4、g4b（g2、g3 沒有）
    assert k("starters_5")["have"] == 2 and k("minutes_ok")["have"] == 2                   # g4 因先發/分鐘被排除
    assert k("derived_metrics")["have"] == 4
    assert k("injury_both_teams")["have"] == 1 and k("injury_any_team")["have"] == 2        # g1 / g1+g3
    assert k("team_stats")["pct"] == 80.0

    miss = res["missing"]["team_stats"]
    assert [m["game_id"] for m in miss] == [ids["g2"]]
    assert miss[0]["matchup"] == "NYK@BOS" and miss[0]["date_et"] == "2022-01-11" and miss[0]["nba_game_id"] == "espn:777"
    assert res["missing_dates"]["player_stats"] == {"2022-01-11": 1, "2022-01-13": 1}
    assert {m["game_id"] for m in res["missing"]["starters_5"]} == {ids["g2"], ids["g3"], ids["g4"]}
    assert [m["game_id"] for m in res["missing"]["quarter_sum_ok"]] == [ids["g4b"]]
    assert res["injury_report_age_min"]["median"] > 0


def test_audit_never_counts_non_final_or_preseason_games(seeded, db):
    mk_game(db, seeded, "0022100001", utc(2022, 1, 10, 0))
    mk_game(db, seeded, "0022100002", utc(2022, 1, 20, 0), status="scheduled", store=False)
    mk_game(db, seeded, "0012100001", utc(2021, 10, 5, 0), stage="preseason")
    with db.cursor() as cur:
        res = ca.audit(cur, [SEASON], include_injury=False)[SEASON]
    assert res["games_final"] == 1 and res["keys"]["team_stats"]["pct"] == 100.0


def test_audit_does_not_fabricate_missing_raw_counts(seeded, db):
    box = make_box("0022100001", utc(2022, 1, 10, 0), BOS, NYK, home_raw=team_raw(110, fga=None))
    mk_game(db, seeded, "0022100001", utc(2022, 1, 10, 0), box=box)
    with db.cursor() as cur:
        res = ca.audit(cur, [SEASON], include_injury=False)[SEASON]
    assert res["keys"]["team_stats"]["have"] == 0 and res["keys"]["derived_metrics"]["have"] == 0   # 缺原始計數 → 不算完整


def test_audit_stale_formula_version_is_not_counted_as_derived(seeded, db):
    mk_game(db, seeded, "0022100001", utc(2022, 1, 10, 0))
    with db.cursor() as cur:
        assert ca.audit(cur, [SEASON], include_injury=False)[SEASON]["keys"]["derived_metrics"]["have"] == 1
        cur.execute("UPDATE team_game_derived SET formula_version = 'box-v0'")
        assert ca.audit(cur, [SEASON], include_injury=False)[SEASON]["keys"]["derived_metrics"]["have"] == 0
    assert FORMULA_VERSION != "box-v0"


def test_audit_empty_database_is_empty_not_error(db):
    with db.cursor() as cur:
        assert ca.audit(cur, include_injury=False) == {}


def test_format_table_and_ledger(seeded, db):
    build(db, seeded)
    with db.cursor() as cur:
        res = ca.audit(cur, [SEASON], include_injury=False)
        led = ca.injury_report_ledger(cur)
    table = ca.format_table(res)
    assert SEASON in table and "team_stats" in table and "4/5 (80.0%) -1" in table
    assert led["total"]["reports"] == 0


def test_evaluate_game_pure_flags():
    g = dict(nba_game_id="0022100001", home_pts=110, away_pts=100, home_abbr="BOS", away_abbr="NYK",
             date_utc=utc(2022, 1, 10, 0),
             **{f"{s}_q{i}": v for s in ("home", "away") for i, v in zip(range(1, 5), (27, 27, 27, 29) if s == "home" else (25, 25, 25, 25))},
             home_ot=None, away_ot=None, home_h1=54, home_h2=56, away_h1=50, away_h2=50,
             h_n=10, a_n=10, h_mins=240.0, a_mins=240.0, h_st=5, a_st=5, h_tmin=240.0, a_tmin=240.0,
             h_dver=FORMULA_VERSION, a_dver=FORMULA_VERSION, h_ortg=1.0, a_ortg=1.0, h_pace=1.0, a_pace=1.0,
             **{f"{p}_{c}": v for p in ("h", "a") for c, v in dict(
                 tpts=110 if p == "h" else 100, fga=88, fgm=40, fg3m=12, fg3a=33, ftm=18, fta=22, oreb=10, dreb=35,
                 tov=12, ast=24).items()})
    f = ca.evaluate_game(g)
    assert all(f[k] for k in ca.AUDIT_KEYS if not k.startswith("injury")), f
    g["home_ot"] = 5                                                      # 延長賽加時分數使加總對不上總分
    assert not ca.evaluate_game(g)["quarter_sum_ok"]
    g["home_ot"] = None
    g["h_mins"] = 238.5                                                   # 分鐘容許 ±1
    assert not ca.evaluate_game(g)["minutes_ok"]


def test_injury_validation_counts_played_vs_status(seeded, db):
    """Out 球員未上場、Not Listed 的常規先發有上場 → 對應比例正確。"""
    from core.jobs.injury_validation import validate
    # 5 場歷史（讓 BOS 球員成為常規先發），再一場被稽核的比賽
    for i in range(5):
        mk_game(db, seeded, f"00221000{i + 10}", utc(2022, 1, 1 + i * 2, 0))
    target = mk_game(db, seeded, "0022100099", utc(2022, 1, 12, 0),
                     box=make_box("0022100099", utc(2022, 1, 12, 0), BOS, NYK))
    with db.cursor() as cur:
        cur.execute("SELECT nba_player_id, id FROM players WHERE nba_player_id = 100 OR nba_player_id = 101")
        pid = {r["nba_player_id"]: r["id"] for r in cur.fetchall()}
        cur.execute("DELETE FROM player_game_stats WHERE game_id = %s AND player_id = %s", (target, pid[100]))   # 100 沒上場
    ingest_report(db, [{"game_date": "01/11/2022", "matchup": "NYK@BOS", "team": "Boston Celtics",
                        "player_name": "Player1x0", "status": "Out", "reason": "x"},
                       {"game_date": "01/11/2022", "matchup": "NYK@BOS", "team": "New York Knicks",
                        "player_name": "Nobody, Else", "status": "Out", "reason": "x"}], utc(2022, 1, 11, 22))
    with db.cursor() as cur:
        res = validate(cur)
    assert res["official_status_vs_played"]["Out"] == {"n": 1, "played": 0, "dnp_rate": 1.0}
    reg = res["regular_starters_by_pregame_state"]
    assert reg["Out"]["dnp_rate"] == 1.0                                   # 常規先發 Player1x0 被列 Out 且確實缺陣
    assert reg["Not Listed"]["n"] >= 4 and reg["Not Listed"]["dnp_rate"] == 0.0
