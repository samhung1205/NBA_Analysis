"""
賽前特徵底座（Phase C.5B）：滾動進階指標 / 先發與輪替分鐘 / 傷病衝擊
------------------------------------------------------------
只建立「可供下一階段 ML 使用的資料層」，不調模型。核心不變量：

  **一場比賽的特徵只能使用 game_time_utc 嚴格早於該場開賽（且報告時間早於 cutoff）的資料。**

做法：把比賽依 game_time_utc 排序、同一開賽時間的比賽分成一組；對每一組「先算特徵、再把這組的結果
併入狀態」。特徵計算當下，狀態裡只有嚴格更早的比賽——洩漏在結構上不可能，而不是靠事後 shift。
此外每列帶有 {side}_asof_game_utc（用到的最新一場歷史比賽時間），assert_no_leakage() 會逐列檢查。

特徵（side = home / away；N = 5, 10 為「該隊過去 N 場」，跨賽季連續計算，不足 3 場為 NaN）
  {side}_{m}_r{N}        m ∈ pace, ortg, drtg, net_rtg, efg_pct, ts_pct, tov_pct, orb_pct, drb_pct, ftr, fg3a_rate
                         （box-v1 衍生指標，見 core/metrics.py）。注意：pace / ortg / drtg / net_rtg 是由基本 box score
                         以公式「估計」的值，不是 stats.nba.com 官方 advanced；v2（temporal_features.py）已改名為
                         est_pace / est_off_rtg / est_def_rtg / est_net_rtg。v1 欄名保留以免舊輸出檔失效。
                         v1 的窗口跨賽季連續（季初 r5/r10 = 上季最後幾場），C.5C 改用 v2 的本季窗口 + 上季先驗。
  {side}_opp_{m}_r{N}    m ∈ efg_pct, tov_pct, ftr, orb_pct：對手在該隊比賽中的表現（防守面）
  {side}_starter_min_r5  該隊過去 5 場先發球員的平均上場分鐘
  {side}_rot_size_r10    過去 10 場平均每場上場 ≥10 分鐘的球員數
  {side}_top8_min_share_r10  過去 10 場前 8 名球員分鐘佔球隊總分鐘比
  {side}_bench_min_share_r10 過去 10 場板凳（非先發）分鐘佔比
  {side}_games_prior, {side}_rest_days, {side}_season_game_no
  傷病（as-of 賽前最後已知報告，見 core/injury_asof.py；decision_offset_min 可模擬「開賽前 X 分鐘做決策」）
  {side}_inj_known         開賽前是否有已申報報告涵蓋該隊（0/1；0 = 無資訊，不等於健康）
  {side}_inj_report_age_min  最後涵蓋報告距 cutoff 的分鐘
  {side}_inj_n_out / _n_doubtful / _n_questionable
  {side}_inj_min_lost      Σ 缺陣機率 × 該球員健康時平均分鐘（過去最多 10 場）——分鐘加權傷病衝擊
  {side}_inj_min_lost_share  上式 / 240
  {side}_exp_starters_absent_w   預期先發（過去 10 場先發次數最多的 5 人）的加權缺陣人數
  {side}_exp_starters_n_out      預期先發中官方列 Out 的人數
  {side}_exp_starters_min_lost_share  預期先發缺陣造成的分鐘損失佔比
  *_diff                   主 − 客的差（部分指標）
  y_*                      標籤（home_win / margin / total / h1_margin / h1_total）——僅供訓練，絕非特徵
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .. import injury_asof, metrics
from ..injury_asof import P_ABSENT, InjuryIndex, team_state_asof
from ..timeutil import et_date, ensure_utc

FEATURE_VERSION = "pregame-v1"
WINDOWS = (5, 10)
MIN_PERIODS = 3
TEAM_METRICS = ["pace", "ortg", "drtg", "net_rtg", "efg_pct", "ts_pct", "tov_pct", "orb_pct", "drb_pct", "ftr",
                "fg3a_rate"]
OPP_METRICS = ["efg_pct", "tov_pct", "ftr", "orb_pct"]
DIFF_METRICS = ["ortg", "drtg", "net_rtg", "pace", "efg_pct", "tov_pct"]


class LeakageError(AssertionError):
    pass


@dataclass
class PlayerLine:
    player_id: int
    minutes: float
    started: bool


@dataclass
class GameRecord:
    """輸入：一場已結束的比賽（歷史狀態只會在輪到它之後才被使用）。"""
    game_id: int
    season: str
    game_time_utc: datetime
    home_team_id: int
    away_team_id: int
    home_abbr: str
    away_abbr: str
    home_pts: int | None
    away_pts: int | None
    home_h1: int | None = None
    away_h1: int | None = None


class _TeamState:
    __slots__ = ("hist", "last_time", "season", "season_n", "player_games")

    def __init__(self) -> None:
        self.hist: list[dict[str, float | None]] = []             # 每場的指標 dict（含 opp_*）
        self.last_time: datetime | None = None
        self.season: str | None = None
        self.season_n = 0
        self.player_games: deque[list[PlayerLine]] = deque(maxlen=10)   # 過去 10 場先發/分鐘


def _nanmean(vals: list[float | None]) -> float:
    xs = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return float(np.mean(xs)) if len(xs) >= MIN_PERIODS else float("nan")


def _window_mean(hist: list[dict], key: str, n: int) -> float:
    return _nanmean([h.get(key) for h in hist[-n:]])


def _rotation_features(team: _TeamState) -> dict[str, float]:
    out = {"starter_min_r5": float("nan"), "rot_size_r10": float("nan"),
           "top8_min_share_r10": float("nan"), "bench_min_share_r10": float("nan")}
    games = list(team.player_games)
    if len(games) >= MIN_PERIODS:
        last5 = games[-5:]
        starter_mins = [p.minutes for g in last5 for p in g if p.started]
        if starter_mins:
            out["starter_min_r5"] = float(np.mean(starter_mins))
        out["rot_size_r10"] = float(np.mean([sum(1 for p in g if p.minutes >= 10) for g in games]))
        shares, bench = [], []
        for g in games:
            total = sum(p.minutes for p in g)
            if total <= 0:
                continue
            top8 = sum(sorted((p.minutes for p in g), reverse=True)[:8])
            shares.append(top8 / total)
            bench.append(sum(p.minutes for p in g if not p.started) / total)
        if shares:
            out["top8_min_share_r10"], out["bench_min_share_r10"] = float(np.mean(shares)), float(np.mean(bench))
    return out


def _expected_starters(team: _TeamState) -> list[tuple[int, float]]:
    """過去 10 場先發次數最多的 5 人（同分比平均分鐘）→ [(player_id, 平均分鐘)]。"""
    starts: dict[int, int] = defaultdict(int)
    mins: dict[int, list[float]] = defaultdict(list)
    for g in team.player_games:
        for p in g:
            starts[p.player_id] += int(p.started)
            mins[p.player_id].append(p.minutes)
    ranked = sorted(starts, key=lambda pid: (-starts[pid], -float(np.mean(mins[pid]))))[:5]
    return [(pid, float(np.mean(mins[pid]))) for pid in ranked if starts[pid] > 0]


def _injury_features(state: injury_asof.TeamInjuryState, team: _TeamState, cutoff: datetime,
                     healthy_mpg) -> dict[str, float]:
    nan = float("nan")
    out = {"inj_known": 1.0 if state.known else 0.0, "inj_report_age_min": nan, "inj_n_out": nan,
           "inj_n_doubtful": nan, "inj_n_questionable": nan, "inj_min_lost": nan, "inj_min_lost_share": nan,
           "exp_starters_absent_w": nan, "exp_starters_n_out": nan, "exp_starters_min_lost_share": nan}
    if not state.known:
        return out
    out["inj_report_age_min"] = (cutoff - state.report_time_utc).total_seconds() / 60.0
    by_status = defaultdict(int)
    lost = 0.0
    for p in state.players:
        by_status[p.status] += 1
        pa = P_ABSENT.get(p.status, 0.0)
        if pa > 0 and p.player_id is not None:
            mpg = healthy_mpg(p.player_id)
            if mpg is not None:
                lost += pa * mpg
    out.update(inj_n_out=float(by_status["Out"]), inj_n_doubtful=float(by_status["Doubtful"]),
               inj_n_questionable=float(by_status["Questionable"]), inj_min_lost=lost,
               inj_min_lost_share=lost / 240.0)
    exp = _expected_starters(team)
    if exp:
        absent_w = n_out = share = 0.0
        for pid, mpg in exp:
            status = state.status_of(pid)
            pa = P_ABSENT.get(status, 0.0)
            absent_w += pa
            n_out += status == "Out"
            share += pa * mpg / 240.0
        out.update(exp_starters_absent_w=absent_w, exp_starters_n_out=float(n_out),
                   exp_starters_min_lost_share=share)
    return out


def build_pregame_features(
    games: Iterable[GameRecord],
    derived: dict[tuple[int, int], dict[str, float | None]],
    players: dict[int, list[tuple[int, int, float, bool]]],
    injury_index: InjuryIndex | None = None,
    *,
    decision_offset_min: int = 0,
    windows: tuple[int, ...] = WINDOWS,
) -> pd.DataFrame:
    """
    games    已結束的賽事（任意順序）。
    derived  {(game_id, team_id): box-v1 指標 dict}。
    players  {game_id: [(team_id, player_id, minutes, started), ...]}。
    injury_index  傷病快照索引（None → 傷病特徵為 NaN、inj_known=0）。
    decision_offset_min  模擬在開賽前 X 分鐘做決策：傷病只用 report_time < 開賽 − X 分鐘的報告。
    """
    ordered = sorted(games, key=lambda g: (g.game_time_utc, g.game_id))
    teams: dict[int, _TeamState] = defaultdict(_TeamState)
    player_hist: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
    rows: list[dict[str, Any]] = []

    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j].game_time_utc == ordered[i].game_time_utc:
            j += 1
        group = ordered[i:j]
        # ---- 1) 先算這組比賽的特徵：此時狀態只含嚴格更早的比賽 ---- #
        for g in group:
            rows.append(_features_for_game(g, teams, player_hist, injury_index, decision_offset_min, windows))
        # ---- 2) 再把這組比賽併入狀態 ---- #
        for g in group:
            _absorb(g, teams, player_hist, derived, players)
        i = j
    df = pd.DataFrame(rows)
    return df.sort_values(["game_time_utc", "game_id"]).reset_index(drop=True) if not df.empty else df


def _features_for_game(g: GameRecord, teams, player_hist, injury_index, offset_min, windows) -> dict[str, Any]:
    tip = ensure_utc(g.game_time_utc)
    cutoff = tip - timedelta(minutes=offset_min)
    row: dict[str, Any] = {
        "game_id": g.game_id, "season": g.season, "game_time_utc": tip,
        "home_team_id": g.home_team_id, "away_team_id": g.away_team_id,
    }

    def healthy_mpg(pid: int) -> float | None:
        h = player_hist.get(pid)
        return float(np.mean(h)) if h and len(h) >= MIN_PERIODS else None

    for side, tid, abbr in (("home", g.home_team_id, g.home_abbr), ("away", g.away_team_id, g.away_abbr)):
        t = teams[tid]
        p = f"{side}_"
        row[p + "games_prior"] = len(t.hist)
        row[p + "asof_game_utc"] = t.last_time
        row[p + "rest_days"] = (min(max((tip - t.last_time).total_seconds() / 86400.0 - 1, 0.0), 14.0)
                                if t.last_time else float("nan"))
        row[p + "season_game_no"] = t.season_n if t.season == g.season else 0
        for n in windows:
            for m in TEAM_METRICS:
                row[f"{p}{m}_r{n}"] = _window_mean(t.hist, m, n)
            for m in OPP_METRICS:
                row[f"{p}opp_{m}_r{n}"] = _window_mean(t.hist, f"opp_{m}", n)
        for k, v in _rotation_features(t).items():
            row[p + k] = v
        if injury_index is not None:
            state = team_state_asof(injury_index, team_abbr=abbr, game_date=et_date(tip), cutoff_utc=cutoff)
            inj = _injury_features(state, t, cutoff, healthy_mpg)
        else:
            inj = _injury_features(injury_asof.TeamInjuryState(known=False), t, cutoff, healthy_mpg)
        for k, v in inj.items():
            row[p + k] = v

    for n in windows:
        for m in DIFF_METRICS:
            row[f"{m}_r{n}_diff"] = row[f"home_{m}_r{n}"] - row[f"away_{m}_r{n}"]
    row["inj_min_lost_share_diff"] = row["home_inj_min_lost_share"] - row["away_inj_min_lost_share"]
    row["exp_starters_absent_w_diff"] = row["home_exp_starters_absent_w"] - row["away_exp_starters_absent_w"]

    row["y_home_win"] = None if g.home_pts is None or g.away_pts is None else int(g.home_pts > g.away_pts)
    row["y_margin"] = None if g.home_pts is None or g.away_pts is None else g.home_pts - g.away_pts
    row["y_total"] = None if g.home_pts is None or g.away_pts is None else g.home_pts + g.away_pts
    row["y_h1_margin"] = None if g.home_h1 is None or g.away_h1 is None else g.home_h1 - g.away_h1
    row["y_h1_total"] = None if g.home_h1 is None or g.away_h1 is None else g.home_h1 + g.away_h1
    return row


def _absorb(g: GameRecord, teams, player_hist, derived, players) -> None:
    """把一場已結束比賽併入狀態（只在該場開賽時間組的特徵都算完之後呼叫）。"""
    tip = ensure_utc(g.game_time_utc)
    by_team: dict[int, list[PlayerLine]] = defaultdict(list)
    for tid, pid, minutes, started in players.get(g.game_id, []):
        by_team[tid].append(PlayerLine(pid, float(minutes or 0.0), bool(started)))
    for tid, opp in ((g.home_team_id, g.away_team_id), (g.away_team_id, g.home_team_id)):
        t = teams[tid]
        mine, theirs = derived.get((g.game_id, tid)), derived.get((g.game_id, opp))
        rec: dict[str, float | None] = {}
        for m in TEAM_METRICS:
            rec[m] = mine.get(m) if mine else None
        for m in OPP_METRICS:
            rec[f"opp_{m}"] = theirs.get(m) if theirs else None
        t.hist.append(rec)
        t.season_n = t.season_n + 1 if t.season == g.season else 1
        t.season = g.season
        t.last_time = tip
        lines = by_team.get(tid, [])
        if lines:
            t.player_games.append(lines)
    for lines in by_team.values():
        for p in lines:
            if p.minutes > 0:
                player_hist[p.player_id].append(p.minutes)


def assert_no_leakage(df: pd.DataFrame) -> None:
    """逐列檢查：每隊用到的最新歷史比賽時間必須嚴格早於該場開賽時間。"""
    for side in ("home", "away"):
        col = df[f"{side}_asof_game_utc"]
        bad = df[col.notna() & (col >= df["game_time_utc"])]
        if len(bad):
            raise LeakageError(f"{side}: {len(bad)} 列用到不早於開賽時間的資料，例如 game_id={bad.iloc[0]['game_id']}")
    inj_age = [c for c in df.columns if c.endswith("inj_report_age_min")]
    for c in inj_age:
        if (df[c].dropna() <= 0).any():
            raise LeakageError(f"{c}: 傷病報告時間不早於 cutoff")


FEATURE_PREFIXES_EXCLUDED = ("y_",)


def feature_columns(df: pd.DataFrame) -> list[str]:
    """可餵給模型的欄位（排除 id、時間、標籤與 asof 稽核欄）。"""
    skip = {"game_id", "season", "game_time_utc", "home_team_id", "away_team_id"}
    return [c for c in df.columns if c not in skip and not c.startswith(FEATURE_PREFIXES_EXCLUDED)
            and not c.endswith("_asof_game_utc")]


# ------------------------------------------------------------------ #
# DB 載入                                                              #
# ------------------------------------------------------------------ #

def load_inputs(cur) -> tuple[list[GameRecord], dict, dict]:
    """載入「全部賽季」的歷史輸入（滾動窗口需要跨賽季的前期資料）；要輸出哪些賽季由呼叫端篩選結果列。"""
    cur.execute(
        """
        SELECT g.id, g.season, g.date_utc, g.home_team_id, g.away_team_id, ht.abbr AS h_abbr, at.abbr AS a_abbr,
               g.home_pts, g.away_pts, g.home_h1, g.away_h1
          FROM games g JOIN teams ht ON ht.id = g.home_team_id JOIN teams at ON at.id = g.away_team_id
         WHERE g.status = 'final' AND g.season_stage IN ('regular', 'playin', 'playoffs')
           AND g.home_pts IS NOT NULL AND g.away_pts IS NOT NULL
         ORDER BY g.date_utc, g.id""")
    games = [GameRecord(r["id"], r["season"], ensure_utc(r["date_utc"]), r["home_team_id"], r["away_team_id"],
                        r["h_abbr"], r["a_abbr"], r["home_pts"], r["away_pts"], r["home_h1"], r["away_h1"])
             for r in cur.fetchall()]
    cur.execute(
        "SELECT game_id, team_id, " + ", ".join(TEAM_METRICS) + " FROM team_game_derived WHERE formula_version = %s",
        (metrics.FORMULA_VERSION,))
    derived = {(r["game_id"], r["team_id"]): {m: r[m] for m in TEAM_METRICS} for r in cur.fetchall()}
    cur.execute("SELECT game_id, team_id, player_id, min, started FROM player_game_stats")
    players: dict[int, list] = defaultdict(list)
    for r in cur.fetchall():
        players[r["game_id"]].append((r["team_id"], r["player_id"], r["min"], bool(r["started"])))
    return games, derived, players
