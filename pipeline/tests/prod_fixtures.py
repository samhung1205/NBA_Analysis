"""C.5D 測試共用：決定性的合成聯盟（多季、box score 衍生指標、球員分鐘、傷病快照）"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np

from core.injury_asof import InjuryIndex, PlayerState, Snapshot
from core.models.pregame_features import GameRecord
from core.production.features import HistoryInputs, ScheduledGame
from core.timeutil import ET, UTC

SEASONS = ["2021-22", "2022-23", "2023-24"]
N_TEAMS = 6
STAR_K = 0                      # 每隊 k=0 的球員是主力（分鐘最多）


def abbr(tid: int) -> str:
    return f"T{tid}"


def pid_of(tid: int, k: int, season_idx: int = 0) -> int:
    """每季換掉一位第 8 順位球員（名單延續性 < 1）。"""
    return tid * 100 + k + (season_idx * 10 if k == 7 else 0)


MINUTES = [36.0, 34.0, 32.0, 30.0, 28.0, 24.0, 20.0, 18.0, 16.0, 12.0]


def snapshot(sid: int, when: datetime, tip: datetime, teams: list[int], listed: dict[int, list[tuple[int, str]]]
             ) -> Snapshot:
    iso = tip.astimezone(ET).date().isoformat()
    cov = {iso: {abbr(t): {"nys": False, "implied": not listed.get(t)} for t in teams}}
    entries = {(iso, abbr(t)): [PlayerState(pid, f"P{pid}", status) for pid, status in listed[t]]
               for t in teams if listed.get(t)}
    return Snapshot(sid, when, cov, entries)


def synthetic_league(seed: int = 5, games_per_season: int = 120, seasons=SEASONS, injuries: bool = True,
                     gap_hours: int = 12) -> HistoryInputs:
    rng = np.random.default_rng(seed)
    strength = rng.normal(0, 5, N_TEAMS)
    games, stages, derived, players, snaps = [], {}, {}, {}, []
    gid = sid = 0
    for si, season in enumerate(seasons):
        strength = 0.6 * strength + rng.normal(0, 3, N_TEAMS)
        start = datetime(2021 + si, 10, 20, 23, 0, tzinfo=UTC)
        for i in range(games_per_season):
            h, a = (int(x) + 1 for x in rng.choice(N_TEAMS, 2, replace=False))
            gid += 1
            tip = start + timedelta(hours=gap_hours * i)
            # 傷病：約 1/4 的比賽主隊主力 Out（且真的沒上場）、1/6 客隊某角色球員 Questionable
            listed: dict[int, list[tuple[int, str]]] = {h: [], a: []}
            star_out = injuries and rng.random() < 0.25
            if star_out:
                listed[h].append((pid_of(h, STAR_K, si), "Out"))
            q_pid = pid_of(a, 3, si)
            q_play = True
            if injuries and rng.random() < 0.17:
                listed[a].append((q_pid, "Questionable"))
                q_play = rng.random() < 0.6
            if injuries:
                sid += 1
                snaps.append(snapshot(sid, datetime.combine(tip.astimezone(ET).date() - timedelta(days=1),
                                                            time(18, 0), tzinfo=ET).astimezone(UTC),
                                      tip, [h, a], {t: [x for x in v if x[1] != "Out"] for t, v in listed.items()}))
                sid += 1
                snaps.append(snapshot(sid, tip - timedelta(minutes=90), tip, [h, a], listed))
            m = strength[h - 1] - strength[a - 1] + 2.5 - (6.0 if star_out else 0.0) + rng.normal(0, 12)
            tot = 222 + rng.normal(0, 18)
            hp, ap = int(round((tot + m) / 2)), int(round((tot - m) / 2))
            if hp == ap:
                hp += 1
            h1h, h1a = hp // 2, ap // 2
            games.append(GameRecord(gid, season, tip, h, a, abbr(h), abbr(a), hp, ap, h1h, h1a))
            stages[gid] = "playoffs" if i >= games_per_season - 6 else "regular"
            for tid, pts, opp in ((h, hp, ap), (a, ap, hp)):
                poss = 99 + rng.normal(0, 3)
                derived[(gid, tid)] = {"pace": poss, "ortg": 100 * pts / poss, "drtg": 100 * opp / poss,
                                       "net_rtg": 100 * (pts - opp) / poss, "efg_pct": 0.54 + rng.normal(0, 0.03),
                                       "ts_pct": 0.58, "tov_pct": 0.13 + rng.normal(0, 0.01),
                                       "orb_pct": 0.25 + rng.normal(0, 0.02), "drb_pct": 0.75, "ftr": 0.2,
                                       "fg3a_rate": 0.4}
            lines = []
            for tid in (h, a):
                for k, mins in enumerate(MINUTES):
                    pid = pid_of(tid, k, si)
                    if tid == h and k == STAR_K and star_out:
                        continue
                    if tid == a and pid == q_pid and not q_play:
                        continue
                    lines.append((tid, pid, mins, k < 5, float(rng.normal(0, 5))))
            players[gid] = lines
    return HistoryInputs(games, stages, derived, players, InjuryIndex(merge_snapshots(snaps)))


def merge_snapshots(snaps: list[Snapshot]) -> list[Snapshot]:
    """同一時間點的報告合併成一份（官方報告一個時間點只有一份，DB 也以 report_time 唯一）。"""
    by_time: dict[datetime, Snapshot] = {}
    for s in snaps:
        cur = by_time.get(s.report_time_utc)
        if cur is None:
            by_time[s.report_time_utc] = Snapshot(len(by_time) + 1, s.report_time_utc,
                                                  {k: dict(v) for k, v in s.coverage.items()}, dict(s.entries))
            continue
        for iso, teams in s.coverage.items():
            cur.coverage.setdefault(iso, {}).update(teams)
        for k, v in s.entries.items():
            cur.entries[k] = cur.entries.get(k, []) + list(v)
    return list(by_time.values())


def last_tip(h: HistoryInputs) -> datetime:
    return max(g.game_time_utc for g in h.games)


def scheduled(game_id: int, tip: datetime, home: int, away: int, season: str = "2023-24",
              stage: str = "regular") -> ScheduledGame:
    return ScheduledGame(game_id, season, stage, tip, home, away, abbr(home), abbr(away), f"S{game_id}")


def as_scheduled(g: GameRecord, stage: str = "regular") -> ScheduledGame:
    return ScheduledGame(g.game_id, g.season, stage, g.game_time_utc, g.home_team_id, g.away_team_id,
                         g.home_abbr, g.away_abbr)


# ------------------------------------------------------------------ #
# DB 寫入（暫存 schema；id 與合成資料一致）                                 #
# ------------------------------------------------------------------ #

def seed_db(db, h: HistoryInputs, extra_games: list[ScheduledGame] = ()) -> None:
    from core import metrics
    with db.cursor() as cur:
        tids = sorted({g.home_team_id for g in h.games} | {g.away_team_id for g in h.games})
        cur.executemany("INSERT INTO teams (id, nba_team_id, abbr, name) VALUES (%s, %s, %s, %s)",
                        [(t, 1610612700 + t, abbr(t), f"Team {t}") for t in tids])
        pids = sorted({ln[1] for lines in h.players.values() for ln in lines})
        cur.executemany("INSERT INTO players (id, nba_player_id, name, team_id) VALUES (%s, %s, %s, %s)",
                        [(p, 9_000_000 + p, f"P{p}", p // 100) for p in pids])
        cur.executemany(
            """INSERT INTO games (id, nba_game_id, season, season_stage, date_utc, home_team_id, away_team_id, status,
                                  home_pts, away_pts, home_h1, away_h1)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'final', %s, %s, %s, %s)""",
            [(g.game_id, f"H{g.game_id}", g.season, h.stages[g.game_id], g.game_time_utc, g.home_team_id,
              g.away_team_id, g.home_pts, g.away_pts, g.home_h1, g.away_h1) for g in h.games])
        for sg in extra_games:
            add_scheduled(cur, sg)
        cols = list(next(iter(h.derived.values())))
        cur.executemany(
            f"INSERT INTO team_game_derived (game_id, team_id, formula_version, {', '.join(cols)}) "
            f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(cols))})",
            [(k[0], k[1], metrics.FORMULA_VERSION, *[v[c] for c in cols]) for k, v in h.derived.items()])
        cur.executemany(
            "INSERT INTO player_game_stats (game_id, player_id, team_id, min, started, plus_minus) VALUES (%s,%s,%s,%s,%s,%s)",
            [(gid, ln[1], ln[0], ln[2], int(ln[3]), ln[4]) for gid, lines in h.players.items() for ln in lines])
        add_snapshots(cur, h.injury_index.snapshots)


def add_scheduled(cur, sg: ScheduledGame, **over) -> None:
    vals = {"home_pts": None, "away_pts": None, "status": sg.status, **over}
    cur.execute(
        """INSERT INTO games (id, nba_game_id, season, season_stage, date_utc, home_team_id, away_team_id, status,
                              home_pts, away_pts)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (sg.game_id, sg.nba_game_id or f"S{sg.game_id}", sg.season, sg.season_stage, sg.game_time_utc,
         sg.home_team_id, sg.away_team_id, vals["status"], vals["home_pts"], vals["away_pts"]))


def add_snapshots(cur, snaps) -> None:
    from psycopg.types.json import Jsonb
    snaps = list(snaps)
    if not snaps:
        return
    cur.executemany("INSERT INTO injury_reports (id, report_time_utc, coverage) VALUES (%s, %s, %s)",
                    [(s.id, s.report_time_utc, Jsonb(s.coverage)) for s in snaps])
    rows = [(s.id, iso, int(ab[1:]), p.player_id, p.player_name, p.status)
            for s in snaps for (iso, ab), plist in s.entries.items() for p in plist]
    if rows:
        cur.executemany("INSERT INTO injury_report_entries (report_id, game_date, team_id, player_id, player_name, status)"
                        " VALUES (%s, %s, %s, %s, %s, %s)", rows)


def add_snapshot(cur, s) -> None:
    add_snapshots(cur, [s])
