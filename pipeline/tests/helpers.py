"""C.5B 測試共用：合成 box score / 賽事建立工具（不連外部來源）"""
from __future__ import annotations

from datetime import datetime

from core.sources.common import quarters_from_periods
from core.timeutil import UTC

BOS, NYK, LAL = 1610612738, 1610612752, 1610612747     # conftest.seeded 的 nba_team_id


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def _split(pts: int) -> list[int]:
    base = pts // 4
    return [base, base, base, pts - 3 * base]


def team_raw(pts=110, **over):
    d = dict(pts=pts, fg_pct=0.45, fg3_pct=0.35, ft_pct=0.8, reb=45, oreb=10, dreb=35, ast=24, stl=7, blk=5,
             tov=12, pf=18, fgm=40, fga=88, fg3m=12, fg3a=33, ftm=18, fta=22, team_min=240.0)
    d.update(over)
    return d


def player(pid, name, started, minutes, pts=10):
    return {"nba_player_id": pid, "name": name, "position": "G", "started": int(started), "min": float(minutes),
            "pts": pts, "reb": 4, "ast": 3, "stl": 1, "blk": 0, "tov": 2, "fgm": 4, "fga": 9, "fg3m": 1, "fg3a": 3,
            "ftm": 1, "fta": 2, "plus_minus": 1.0}


def lineup(prefix: int, n: int = 8, total: float = 240.0):
    """n 位球員，前 5 位先發，分鐘加總 = total。"""
    mins = [34, 32, 30, 28, 26, 22, 20, 16, 12, 10, 6, 4][:n]
    scale = total / sum(mins)
    return [player(prefix * 100 + i, f"Player{prefix}x{i}", i < 5, round(m * scale, 2)) for i, m in enumerate(mins)]


def make_box(gid: str, when: datetime, home_nba: int, away_nba: int, *, hpts=110, apts=100, status="final",
             home_players=None, away_players=None, home_raw=None, away_raw=None, ot_periods=0):
    hp, ap = _split(hpts), _split(apts)
    periods = lambda parts: [{"period": i + 1, "score": s} for i, s in enumerate(parts)]
    home = {"team": home_raw or team_raw(hpts), "players": home_players if home_players is not None else lineup(1),
            "periods": periods(hp)}
    away = {"team": away_raw or team_raw(apts), "players": away_players if away_players is not None else lineup(2),
            "periods": periods(ap)}
    return {"nba_game_id": gid, "date_utc": when, "status": status, "arena": "Test Arena",
            "home_team_id": home_nba, "away_team_id": away_nba, "home": home, "away": away,
            "quarters": {**quarters_from_periods("home", home["periods"]), **quarters_from_periods("away", away["periods"])}}


def add_espn_game(db, seeded, key: str, when: datetime, home="BOS", away="NYK", *, season="2021-22",
                  stage="regular", hpts=110, apts=100):
    """ESPN 備援回填的暫存賽事（沒有官方 gameId）。"""
    with db.cursor() as cur:
        return db.upsert_game(cur, nba_game_id=f"espn:{key}", season=season, season_stage=stage, date_utc=when,
                              home_team_id=seeded[home], away_team_id=seeded[away], status="final",
                              home_pts=hpts, away_pts=apts, home_q1=25, home_q2=25, home_q3=30, home_q4=30,
                              away_q1=25, away_q2=25, away_q3=25, away_q4=25, home_h1=50, home_h2=60,
                              away_h1=50, away_h2=50)


def count(db, table, where="TRUE", params=()):
    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}", params)
        return cur.fetchone()["n"]
