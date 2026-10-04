"""
賽前特徵 v2（Phase C.5C）：以「本季這支球隊」為主體的時間尺度特徵
------------------------------------------------------------
與 pregame-v1（core/models/pregame_features.py）的差別：

  * v1 的滾動窗口跨賽季連續——季初的 r5/r10 其實是「上季最後幾場」的同一 franchise，名單可能已大換血。
    v2 的 season / l20 / l10 / l5 **只用本季比賽**；上季資訊另外以 prev_*（上季整季平均）提供，
    並附上「今年這支球隊與去年有多像」的名單延續性特徵，讓模型/收縮公式決定要信多少。
  * box-score 估計的 pace / ORtg / DRtg / net 一律命名為 est_pace / est_off_rtg / est_def_rtg / est_net_rtg
    （core/metrics.py box-v1 公式），避免與 stats.nba.com 官方 advanced 混淆。
  * 傷病缺陣機率不再用固定常數：InjuryCalibrator 以「已結束比賽」的賽前狀態 vs 實際是否上場，
    逐場 walk-forward 估計 P(absent | status, 報告距開賽時間)。某場比賽的特徵只會用到開賽前已結束比賽的統計。
  * 球員重要性只由賽前的上場分鐘 / 先發 / 輪替使用量估計（不使用正負值等結果型指標）。

核心不變量（與 v1 相同的結構性防護）：比賽依 game_time_utc 排序、同開賽時間為一組，
「先算這組的特徵，再把這組的結果併入狀態（含傷病校準統計）」。

決策時點（cutoff）：傷病只用 report_time_utc 嚴格早於 cutoff 的報告（cutoff 不可晚於開賽）。cutoff 由 make_cutoff() 產生：
  "early"   = 賽事日（ET）00:00，相當於台灣中午的每日排程，只看得到前一晚的報告
  "T-<m>"   = 開賽前 m 分鐘

欄位（side ∈ home / away）
  {side}_gp                         本季已賽場數（賽前）
  {side}_{m}_{w}                    m ∈ SERIES，w ∈ season / l20 / l10 / l5；只用本季；樣本不足為 NaN
                                    （season ≥1 場、l5 ≥3、l10 ≥5、l20 ≥10）
  {side}_{m}_prev, {side}_prev_n    上季整季平均（同 franchise）；沒有上季資料為 NaN
  league_{m}_prev                   上季聯盟平均（收縮的目標）
  {side}_ret_min_pct                本季已打分鐘中，上季也替本隊上場的球員占比
  {side}_ret_starter_min_pct        本季先發分鐘中，上季也替本隊上場的球員占比
  {side}_prev_min_returning_pct     上季本隊分鐘中，本季已替本隊上場的球員占比（季初會低估受傷未歸者）
  {side}_starter_continuity         本季預期先發（本季最近 10 場先發次數前 5）中，上季也是本隊預期先發的比例
  {side}_rotation_continuity        本季分鐘前 8 名與上季分鐘前 8 名的重疊比例
  {side}_roster_prior_pm48          本季上場球員（以本季分鐘加權）上季的收縮正負值 / 48 分鐘（名單版的上季先驗）
  {side}_rest_days, {side}_b2b, {side}_games_7d
  {side}_h2h_season_margin, h2h_season_n   本季與這個對手的交手平均分差（弱情境特徵）
  傷病（{side}_inj_*；inj_known=0 → 其餘為 NaN，不是 0）
  {side}_inj_known, _inj_report_age_min, _inj_n_out/_n_doubtful/_n_questionable/_n_probable
  {side}_inj_min_lost_role     Σ P(absent) × 球員自身近 10 次出賽平均分鐘（相對滿編）
  {side}_inj_min_lost_recent   Σ P(absent) × 球員在本隊本季最近 10 場的平均分鐘（DNP 記 0；相對近期常態，
                               長期缺陣者近期分鐘≈0，不會與近期戰績重複計算）
  {side}_inj_exp_starters_avail  預期先發 5 人的期望可出賽人數 Σ(1 − P(absent))
  {side}_inj_rotation_avail_pct  近期前 8 名輪替分鐘中，期望可出賽的比例
  {side}_inj_top3_absent_w       近期分鐘前 3 名的 Σ P(absent)
  {side}_roster_departed_n       本隊近 10 場上場、但之後已替「別隊」上場的球員數（交易 / 釋出；非模型特徵，資料品質用）

pregame-v2.1（C.5D）：與 v2 唯一的差別是「交易感知的名單對應」（trade_aware=True，預設）。
  球員最近一次出賽若是替別隊（賽前已知的 box score 事實），他就不再算入舊隊的「目前名單」：
  預期先發、近期輪替前 8 名、前 3 名、Not Listed 校準母體都排除他；他的重要性（role = 自身近 10 次出賽分鐘）
  跟著球員走，可用性屬於新隊（新隊的傷病報告列他時才會計入）。本季已打分鐘類（ret_min_pct 等）是歷史事實，不排除。
  C.5C 的評估（c5c_evaluate）以 trade_aware=False 保持可重現。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from ..injury_asof import InjuryIndex, TeamInjuryState, team_state_asof
from ..injury_history import NOT_LISTED
from ..timeutil import ET, ensure_utc, et_date
from .pregame_features import GameRecord, LeakageError

FEATURE_VERSION = "pregame-v2.1"
LEGACY_FEATURE_VERSION = "pregame-v2"          # trade_aware=False（C.5C 評估時的定義）

# box-v1 欄位 → v2 名稱（est_ = 由基本 box score 以公式估計，非官方 advanced）
EST_RENAME = {"pace": "est_pace", "ortg": "est_off_rtg", "drtg": "est_def_rtg", "net_rtg": "est_net_rtg"}
BOX_METRICS = ["pace", "ortg", "drtg", "net_rtg", "efg_pct", "ts_pct", "tov_pct", "orb_pct", "drb_pct", "ftr",
               "fg3a_rate"]
OPP_METRICS = ["efg_pct", "tov_pct", "orb_pct", "ftr"]

SERIES = ["est_pace", "est_off_rtg", "est_def_rtg", "est_net_rtg", "efg_pct", "ts_pct", "tov_pct", "orb_pct",
          "opp_efg_pct", "opp_tov_pct", "pts", "opp_pts", "margin", "win", "h1_pts", "h1_opp_pts", "h1_margin"]
WINDOWS = {"season": 1, "l20": 10, "l10": 5, "l5": 3}            # 窗口 → 最少本季場數
WINDOW_N = {"season": None, "l20": 20, "l10": 10, "l5": 5}

ROLE_MPG_MIN = 12.0          # 傷病校準母體：近 10 次出賽平均 ≥ 12 分鐘的球員
ROSTER_RECENT_GAMES = 5      # Not Listed 的母體只取本隊最近 5 場有上場者（排除已被交易走的球員）
PM_SHRINK_MIN = 500.0        # 球員上季正負值收縮：pm / (分鐘 + 500)（向 0 收縮的固定正則化，見報告）

# 校準的先驗（只在累積資料很少時有影響）：C.5B 的常數，Not Listed / Available 用小值
PRIOR_P_ABSENT = {"Out": 1.0, "Doubtful": 0.75, "Questionable": 0.5, "Probable": 0.15, "Available": 0.05,
                  NOT_LISTED: 0.03}
STATUS_ORDER = ["Out", "Doubtful", "Questionable", "Probable", "Available", NOT_LISTED]
M_STATUS = 10.0              # 狀態層 Beta 先驗強度（虛擬樣本數）
M_BUCKET = 20.0              # 狀態 × 報告時距層向狀態層收縮的強度
LEAD_BUCKET_HOURS = 3.0      # 報告距開賽 < 3h vs ≥ 3h


def make_cutoff(timing: str | int) -> Callable[[datetime], datetime]:
    """timing: "early"（ET 賽事日 00:00）、"T-<分鐘>"、或整數分鐘（同 T-<分鐘>）。"""
    if isinstance(timing, int):
        return lambda tip: ensure_utc(tip) - timedelta(minutes=timing)
    if timing == "early":
        def early(tip: datetime) -> datetime:
            d = et_date(ensure_utc(tip))
            return datetime.combine(d, time(0, 0), tzinfo=ET).astimezone(ensure_utc(tip).tzinfo)
        return early
    if timing.startswith("T-"):
        return make_cutoff(int(timing[2:]))
    raise ValueError(f"unknown timing {timing!r}")


def prev_season(season: str) -> str:
    y = int(season[:4]) - 1
    return f"{y}-{str(y + 1)[2:]}"


# ------------------------------------------------------------------ #
# 傷病缺陣機率：walk-forward 校準                                         #
# ------------------------------------------------------------------ #

@dataclass
class InjuryCalibrator:
    """P(absent | status, lead bucket)，只由「已併入狀態」（已結束）比賽的觀測估計。"""
    counts: dict[tuple[str, str], list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    status_counts: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    last_obs_utc: datetime | None = None
    n_obs: int = 0
    fixed: dict[str, float] | None = None    # 對照組：固定常數（C.5B P_ABSENT），不做校準
    season_start_tables: dict[str, dict] = field(default_factory=dict)   # 各賽季第一場開賽前的估計（報告用）
    log: list[tuple] = field(default_factory=list)   # (season, status, lead_h, 賽前預測 p, 實際缺陣) —— 報告用
    frozen: bool = False          # production inference：只讀 artifact 內的校準狀態，不可再學（observe 會拋例外）

    @staticmethod
    def bucket(lead_hours: float) -> str:
        return "lt3h" if lead_hours < LEAD_BUCKET_HOURS else "ge3h"

    def p_status(self, status: str) -> float:
        a, n = self.status_counts[status] if status in self.status_counts else (0, 0)
        prior = PRIOR_P_ABSENT.get(status, 0.0)
        return (a + M_STATUS * prior) / (n + M_STATUS)

    def p_absent(self, status: str, lead_hours: float) -> float:
        if self.fixed is not None:
            return self.fixed.get(status, 0.0)
        ps = self.p_status(status)
        key = (status, self.bucket(lead_hours))
        a, n = self.counts[key] if key in self.counts else (0, 0)
        return (a + M_BUCKET * ps) / (n + M_BUCKET)

    def observe(self, status: str, lead_hours: float, absent: bool, when: datetime, *, season: str = "",
                p_pred: float | None = None) -> None:
        if self.frozen:
            raise RuntimeError("InjuryCalibrator 已凍結（production inference 不可重新校準）")
        if p_pred is not None:
            self.log.append((season, status, lead_hours, p_pred, int(absent)))
        c = self.counts[(status, self.bucket(lead_hours))]
        c[0] += int(absent)
        c[1] += 1
        s = self.status_counts[status]
        s[0] += int(absent)
        s[1] += 1
        self.n_obs += 1
        self.last_obs_utc = when if self.last_obs_utc is None else max(self.last_obs_utc, when)

    # ---- artifact 序列化（defaultdict + lambda 不能 pickle，改存純 dict） ---- #
    def to_state(self) -> dict[str, Any]:
        return {"counts": {f"{s}|{b}": [int(v[0]), int(v[1])] for (s, b), v in self.counts.items()},
                "status_counts": {s: [int(v[0]), int(v[1])] for s, v in self.status_counts.items()},
                "last_obs_utc": self.last_obs_utc.isoformat() if self.last_obs_utc else None,
                "n_obs": int(self.n_obs), "fixed": dict(self.fixed) if self.fixed is not None else None,
                "prior_p_absent": dict(PRIOR_P_ABSENT), "m_status": M_STATUS, "m_bucket": M_BUCKET,
                "lead_bucket_hours": LEAD_BUCKET_HOURS}

    @classmethod
    def from_state(cls, st: dict[str, Any], *, frozen: bool = True) -> "InjuryCalibrator":
        # 常數若與存檔時不同，同一份計數會得到不同機率 → 視為不相容
        for k, cur in (("prior_p_absent", PRIOR_P_ABSENT), ("m_status", M_STATUS), ("m_bucket", M_BUCKET),
                       ("lead_bucket_hours", LEAD_BUCKET_HOURS)):
            if st.get(k) != cur:
                raise ValueError(f"InjuryCalibrator 狀態的 {k} 與目前程式不同：{st.get(k)!r} != {cur!r}")
        c = cls(fixed=st.get("fixed"))
        for key, v in st["counts"].items():
            s, b = key.rsplit("|", 1)
            c.counts[(s, b)] = [int(v[0]), int(v[1])]
        for s, v in st["status_counts"].items():
            c.status_counts[s] = [int(v[0]), int(v[1])]
        c.last_obs_utc = datetime.fromisoformat(st["last_obs_utc"]) if st.get("last_obs_utc") else None
        c.n_obs = int(st["n_obs"])
        c.frozen = frozen
        return c

    def table(self) -> dict[str, Any]:
        out = {}
        for s in STATUS_ORDER:
            a, n = self.status_counts.get(s, (0, 0))
            row = {"n": n, "absent": a, "raw_rate": (a / n) if n else None, "p_status": self.p_status(s)}
            for b in ("lt3h", "ge3h"):
                ab, nb = self.counts.get((s, b), (0, 0))
                row[f"n_{b}"], row[f"raw_{b}"] = nb, (ab / nb) if nb else None
                row[f"p_{b}"] = self.p_absent(s, 1.0 if b == "lt3h" else 10.0)
            out[s] = row
        return out


# ------------------------------------------------------------------ #
# 狀態                                                                 #
# ------------------------------------------------------------------ #

@dataclass
class _Line:
    player_id: int
    minutes: float
    started: bool


class _Team:
    def __init__(self) -> None:
        self.season: str | None = None
        self.cur: list[dict[str, float | None]] = []           # 本季每場的指標
        self.cur_lines: list[list[_Line]] = []                  # 本季每場的球員分鐘/先發
        self.cur_min: dict[int, float] = defaultdict(float)
        self.cur_starter_min: dict[int, float] = defaultdict(float)
        self.cur_starts: dict[int, int] = defaultdict(int)
        self.h2h: dict[int, list[float]] = defaultdict(list)    # 本季對手 → 分差
        self.prev: dict[str, Any] | None = None                  # 上季摘要
        self.last_time: datetime | None = None
        self.times: deque[datetime] = deque(maxlen=8)

    def ensure_season(self, season: str) -> None:
        """賽季切換：在算新賽季第一場的特徵「之前」把本季封存成上季摘要（只用已結束比賽）。"""
        if self.season == season:
            return
        if self.season is not None and self.season == prev_season(season) and self.cur:
            means = {m: _mean([r.get(m) for r in self.cur]) for m in SERIES}
            top8 = {pid for pid, _ in sorted(self.cur_min.items(), key=lambda kv: -kv[1])[:8]}
            starters = set(_top_starters(self.cur_starts, self.cur_min, 5))
            self.prev = {"means": means, "n": len(self.cur), "min": dict(self.cur_min), "top8": top8,
                         "starters": starters, "season": self.season}
        else:
            self.prev = None
        self.season = season
        self.cur, self.cur_lines = [], []
        self.cur_min, self.cur_starter_min = defaultdict(float), defaultdict(float)
        self.cur_starts = defaultdict(int)
        self.h2h = defaultdict(list)


def _mean(vals: Iterable[float | None]) -> float:
    xs = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return float(np.mean(xs)) if xs else float("nan")


def _top_starters(starts: dict[int, int], minutes: dict[int, float], k: int) -> list[int]:
    ranked = sorted((pid for pid in starts if starts[pid] > 0), key=lambda pid: (-starts[pid], -minutes.get(pid, 0.0)))
    return ranked[:k]


def _recent_lines(t: _Team, n: int = 10) -> list[list[_Line]]:
    return t.cur_lines[-n:]


def _recent_mpg(t: _Team, n: int = 10) -> dict[int, float]:
    """本季最近 n 場（DNP 記 0）每位球員的平均分鐘。"""
    games = _recent_lines(t, n)
    if not games:
        return {}
    tot: dict[int, float] = defaultdict(float)
    for g in games:
        for p in g:
            tot[p.player_id] += p.minutes
    return {pid: m / len(games) for pid, m in tot.items()}


def _expected_starters(t: _Team, keep: Callable[[int], bool] | None = None) -> list[int]:
    starts: dict[int, int] = defaultdict(int)
    mins: dict[int, float] = defaultdict(float)
    for g in _recent_lines(t, 10):
        for p in g:
            if keep is not None and not keep(p.player_id):
                continue
            starts[p.player_id] += int(p.started)
            mins[p.player_id] += p.minutes
    return _top_starters(starts, mins, 5)


# ------------------------------------------------------------------ #
# 建構                                                                 #
# ------------------------------------------------------------------ #

class _Builder:
    def __init__(self, derived, players, injury_index, cutoff_fn, fixed_p_absent=None, *, trade_aware=True,
                 calibrator: InjuryCalibrator | None = None):
        self.derived, self.players, self.index, self.cutoff_fn = derived, players, injury_index, cutoff_fn
        self.trade_aware = trade_aware
        self.player_team: dict[int, int] = {}          # 球員最近一次出賽的球隊（只由已併入的 box score 得知）
        self.teams: dict[int, _Team] = defaultdict(_Team)
        self.player_hist: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))   # 球員任一隊最近 10 次出賽分鐘
        self.player_season: dict[tuple[int, str], list[float]] = defaultdict(lambda: [0.0, 0.0])  # (pid, season) → [分鐘, 正負值]
        self.league: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
        self.calib = calibrator if calibrator is not None else InjuryCalibrator(fixed=fixed_p_absent)
        self.pending_obs: dict[int, list[tuple]] = {}   # game_id → [(team, pid, status, lead_h, 賽前 p)]
        self.seen_seasons: set[str] = set()

    # ---------------- features ---------------- #
    def on_team(self, tid: int) -> Callable[[int], bool]:
        """「目前仍屬於 tid」的判斷：球員最近一次出賽是替別隊 → 已離隊（交易 / 釋出）。trade_aware=False 時不過濾。"""
        if not self.trade_aware:
            return lambda pid: True
        return lambda pid: self.player_team.get(pid, tid) == tid

    def features(self, g: GameRecord, *, cutoff: datetime | None = None,
                 schedule_prior: dict[int, list[datetime]] | None = None) -> dict[str, Any]:
        """cutoff：傷病決策時點（預設由 cutoff_fn(開賽) 決定；production 傳入預測時間戳）。
        schedule_prior：{team_id: [開賽時間]}——本場之前「已排定但尚未完賽」的比賽（只有 production 會有）。
        賽程是賽前已知的，所以休息天數 / 背靠背 / 7 日場數照算；這些比賽的結果未知，不進入任何統計。"""
        tip = ensure_utc(g.game_time_utc)
        cutoff = self.cutoff_fn(tip) if cutoff is None else ensure_utc(cutoff)
        if cutoff > tip:
            raise LeakageError("cutoff 不可晚於開賽")
        if g.season not in self.seen_seasons:
            self.seen_seasons.add(g.season)
            self.calib.season_start_tables[g.season] = self.calib.table()
        row: dict[str, Any] = {"game_id": g.game_id, "season": g.season, "game_time_utc": tip,
                               "home_team_id": g.home_team_id, "away_team_id": g.away_team_id,
                               "inj_cutoff_utc": cutoff,
                               "calib_asof_utc": self.calib.last_obs_utc, "calib_n_obs": self.calib.n_obs}
        ps = prev_season(g.season)
        for m in SERIES:
            s, n = self.league[ps][m]
            row[f"league_{m}_prev"] = s / n if n else float("nan")
        obs: list[tuple[int, int, str, float]] = []
        for side, tid, opp, abbr in (("home", g.home_team_id, g.away_team_id, g.home_abbr),
                                     ("away", g.away_team_id, g.home_team_id, g.away_abbr)):
            t = self.teams[tid]
            t.ensure_season(g.season)
            p = f"{side}_"
            gp = len(t.cur)
            row[p + "gp"] = gp
            row[p + "asof_game_utc"] = t.last_time
            for m in SERIES:
                vals = [r.get(m) for r in t.cur]
                for w, minp in WINDOWS.items():
                    n = WINDOW_N[w]
                    row[f"{p}{m}_{w}"] = _mean(vals if n is None else vals[-n:]) if gp >= minp else float("nan")
                row[f"{p}{m}_prev"] = t.prev["means"][m] if t.prev else float("nan")
            row[p + "prev_n"] = t.prev["n"] if t.prev else 0
            row.update({p + k: v for k, v in self._continuity(t, g.season, tid).items()})
            # 賽程（含已排定未完賽的前一場：賽程賽前已知）
            prior = [ensure_utc(x) for x in (schedule_prior or {}).get(tid, []) if ensure_utc(x) < tip]
            if any(t.last_time is not None and x <= t.last_time for x in prior):
                raise ValueError("schedule_prior 只能包含最後一場已完賽比賽之後的場次")
            last_time = max(prior) if prior else t.last_time
            times = list(t.times) + prior
            row[p + "rest_days"] = (min(max((tip - last_time).total_seconds() / 86400.0 - 1, 0.0), 14.0)
                                    if last_time else float("nan"))
            row[p + "b2b"] = float(row[p + "rest_days"] < 0.5) if last_time else float("nan")
            row[p + "games_7d"] = float(sum(1 for x in times if (tip - x) <= timedelta(days=7)))
            hh = t.h2h.get(opp, [])
            row[p + "h2h_season_margin"] = float(np.mean(hh)) if hh else float("nan")
            row[p + "h2h_season_n"] = len(hh)
            # 傷病
            if self.index is not None:
                state = team_state_asof(self.index, team_abbr=abbr, game_date=et_date(tip), cutoff_utc=cutoff)
            else:
                state = TeamInjuryState(known=False)
            inj, team_obs = self._injury(state, t, tip, cutoff, tid)
            row.update({p + k: v for k, v in inj.items()})
            obs.extend(team_obs)
        self.pending_obs[g.game_id] = obs
        row["h2h_season_n"] = row["home_h2h_season_n"]
        row["h2h_season_margin"] = row["home_h2h_season_margin"]
        for k in ("home_h2h_season_n", "home_h2h_season_margin", "away_h2h_season_n", "away_h2h_season_margin"):
            row.pop(k)
        row["y_home_win"] = None if g.home_pts is None or g.away_pts is None else int(g.home_pts > g.away_pts)
        row["y_margin"] = None if g.home_pts is None or g.away_pts is None else g.home_pts - g.away_pts
        row["y_total"] = None if g.home_pts is None or g.away_pts is None else g.home_pts + g.away_pts
        row["y_h1_margin"] = None if g.home_h1 is None or g.away_h1 is None else g.home_h1 - g.away_h1
        row["y_h1_total"] = None if g.home_h1 is None or g.away_h1 is None else g.home_h1 + g.away_h1
        return row

    def _continuity(self, t: _Team, season: str, tid: int) -> dict[str, float]:
        nan = float("nan")
        out = {"ret_min_pct": nan, "ret_starter_min_pct": nan, "prev_min_returning_pct": nan,
               "starter_continuity": nan, "rotation_continuity": nan, "roster_prior_pm48": nan}
        total = sum(t.cur_min.values())
        if total > 0:
            # 名單版的上季先驗：本季上場球員（本季分鐘加權）上季的收縮正負值（任一隊）
            ps = prev_season(season)
            acc = 0.0
            for pid, mins in t.cur_min.items():
                pmin, ppm = self.player_season.get((pid, ps), (0.0, 0.0))
                acc += (mins / total) * 48.0 * ppm / (pmin + PM_SHRINK_MIN)
            out["roster_prior_pm48"] = acc
        if t.prev is None or total <= 0:
            return out
        prev_min = t.prev["min"]
        out["ret_min_pct"] = sum(m for pid, m in t.cur_min.items() if pid in prev_min) / total
        st_total = sum(t.cur_starter_min.values())
        if st_total > 0:
            out["ret_starter_min_pct"] = sum(m for pid, m in t.cur_starter_min.items() if pid in prev_min) / st_total
        prev_total = sum(prev_min.values())
        if prev_total > 0:
            out["prev_min_returning_pct"] = sum(m for pid, m in prev_min.items() if pid in t.cur_min) / prev_total
        exp = _expected_starters(t, self.on_team(tid))
        if exp and t.prev["starters"]:
            out["starter_continuity"] = sum(1 for pid in exp if pid in t.prev["starters"]) / len(exp)
        cur_top8 = [pid for pid, _ in sorted(t.cur_min.items(), key=lambda kv: -kv[1])[:8]]
        if cur_top8 and t.prev["top8"]:
            out["rotation_continuity"] = sum(1 for pid in cur_top8 if pid in t.prev["top8"]) / len(cur_top8)
        return out

    def _injury(self, state: TeamInjuryState, t: _Team, tip: datetime, cutoff: datetime, tid: int):
        nan = float("nan")
        keys = ["inj_known", "inj_report_age_min", "inj_report_lead_h", "inj_n_out", "inj_n_doubtful",
                "inj_n_questionable", "inj_n_probable", "inj_min_lost_role", "inj_min_lost_recent",
                "inj_exp_starters_avail", "inj_rotation_avail_pct", "inj_top3_absent_w"]
        out = {k: nan for k in keys}
        keep = self.on_team(tid)
        recent_all = _recent_mpg(t)
        out["roster_departed_n"] = float(sum(1 for pid in recent_all if not keep(pid))) if self.trade_aware else 0.0
        out["inj_known"] = 1.0 if state.known else 0.0
        if not state.known:
            return out, []
        lead_h = (tip - state.report_time_utc).total_seconds() / 3600.0
        out["inj_report_age_min"] = (cutoff - state.report_time_utc).total_seconds() / 60.0
        out["inj_report_lead_h"] = lead_h
        listed = {p.player_id: p.status for p in state.players if p.player_id is not None}
        counts = defaultdict(int)
        for p in state.players:
            counts[p.status] += 1
        out.update(inj_n_out=float(counts["Out"]), inj_n_doubtful=float(counts["Doubtful"]),
                   inj_n_questionable=float(counts["Questionable"]), inj_n_probable=float(counts["Probable"]))

        def pabs(pid: int) -> float:
            return self.calib.p_absent(listed.get(pid, NOT_LISTED), lead_h)

        def role(pid: int) -> float | None:
            h = self.player_hist.get(pid)
            return float(np.mean(h)) if h else None

        recent = {pid: m for pid, m in recent_all.items() if keep(pid)}
        lost_role = lost_recent = 0.0
        for pid, status in listed.items():
            pa = self.calib.p_absent(status, lead_h)
            r = role(pid)
            if r is not None:
                lost_role += pa * r
            lost_recent += pa * recent.get(pid, 0.0)
        out["inj_min_lost_role"], out["inj_min_lost_recent"] = lost_role, lost_recent
        if recent:
            exp = _expected_starters(t, keep)
            if exp:
                out["inj_exp_starters_avail"] = sum(1.0 - pabs(pid) for pid in exp)
            top8 = sorted(recent, key=lambda pid: -recent[pid])[:8]
            denom = sum(recent[pid] for pid in top8)
            if denom > 0:
                out["inj_rotation_avail_pct"] = sum((1.0 - pabs(pid)) * recent[pid] for pid in top8) / denom
            out["inj_top3_absent_w"] = sum(pabs(pid) for pid in top8[:3])
        # 校準觀測（比賽結束、併入狀態時才計入）：名單上的角色球員 + 近期輪替中未被列出的球員
        obs = []
        for pid, status in listed.items():
            r = role(pid)
            if r is not None and r >= ROLE_MPG_MIN:
                obs.append((tid, pid, status, lead_h, self.calib.p_absent(status, lead_h)))
        on_roster = {p.player_id for g in _recent_lines(t, ROSTER_RECENT_GAMES) for p in g if keep(p.player_id)}
        for pid in on_roster:
            r = role(pid)
            if pid not in listed and r is not None and r >= ROLE_MPG_MIN:
                obs.append((tid, pid, NOT_LISTED, lead_h, self.calib.p_absent(NOT_LISTED, lead_h)))
        return out, obs

    # ---------------- absorb ---------------- #
    def absorb(self, g: GameRecord) -> None:
        tip = ensure_utc(g.game_time_utc)
        lines: dict[int, list[_Line]] = defaultdict(list)
        pms: dict[int, float] = {}
        for rec in self.players.get(g.game_id, []):
            tid, pid, minutes, started = rec[0], rec[1], float(rec[2] or 0.0), bool(rec[3])
            if minutes <= 0:
                continue
            lines[tid].append(_Line(pid, minutes, started))
            pms[pid] = float(rec[4]) if len(rec) > 4 and rec[4] is not None else 0.0
        has_box = bool(lines)
        # 傷病校準：賽前狀態 vs 實際是否上場（沒有球員 box 的場次不計）
        for tid, pid, status, lead_h, p_pred in self.pending_obs.pop(g.game_id, []):
            if has_box:
                played = any(p.player_id == pid for p in lines.get(tid, []))
                self.calib.observe(status, lead_h, not played, tip, season=g.season, p_pred=p_pred)
        for tid, opp, pts, opp_pts, h1, h1o in (
                (g.home_team_id, g.away_team_id, g.home_pts, g.away_pts, g.home_h1, g.away_h1),
                (g.away_team_id, g.home_team_id, g.away_pts, g.home_pts, g.away_h1, g.home_h1)):
            t = self.teams[tid]
            t.ensure_season(g.season)
            mine, theirs = self.derived.get((g.game_id, tid)), self.derived.get((g.game_id, opp))
            rec: dict[str, float | None] = {}
            for m in BOX_METRICS:
                rec[EST_RENAME.get(m, m)] = mine.get(m) if mine else None
            for m in OPP_METRICS:
                rec[f"opp_{m}"] = theirs.get(m) if theirs else None
            rec["pts"], rec["opp_pts"] = pts, opp_pts
            rec["margin"] = None if pts is None or opp_pts is None else pts - opp_pts
            rec["win"] = None if rec["margin"] is None else float(rec["margin"] > 0)
            rec["h1_pts"], rec["h1_opp_pts"] = h1, h1o
            rec["h1_margin"] = None if h1 is None or h1o is None else h1 - h1o
            t.cur.append(rec)
            for m in SERIES:
                v = rec.get(m)
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    acc = self.league[g.season][m]
                    acc[0] += float(v)
                    acc[1] += 1
            if rec["margin"] is not None:
                t.h2h[opp].append(float(rec["margin"]))
            t.last_time = tip
            t.times.append(tip)
            ls = lines.get(tid, [])
            if ls:
                t.cur_lines.append(ls)
                for p in ls:
                    t.cur_min[p.player_id] += p.minutes
                    if p.started:
                        t.cur_starts[p.player_id] += 1
                        t.cur_starter_min[p.player_id] += p.minutes
        for tid, ls in lines.items():
            for p in ls:
                self.player_team[p.player_id] = tid
                self.player_hist[p.player_id].append(p.minutes)
                acc = self.player_season[(p.player_id, g.season)]
                acc[0] += p.minutes
                acc[1] += pms.get(p.player_id, 0.0)


def build_temporal_features(
    games: Iterable[GameRecord],
    derived: dict[tuple[int, int], dict[str, float | None]],
    players: dict[int, list[tuple]],
    injury_index: InjuryIndex | None = None,
    *,
    timing: str | int = "T-60",
    return_calibrator: bool = False,
    fixed_p_absent: dict[str, float] | None = None,
    trade_aware: bool = True,
):
    """
    games / derived 同 v1；players: {game_id: [(team_id, player_id, minutes, started[, plus_minus])]}。
    timing: 傷病決策時點，見 make_cutoff()。
    fixed_p_absent: 只供對照實驗——以固定常數取代 walk-forward 校準。
    trade_aware: pregame-v2.1 的交易感知名單對應（預設）；False = C.5C 評估時的 pregame-v2 定義。
    """
    b = _Builder(derived, players, injury_index, make_cutoff(timing), fixed_p_absent, trade_aware=trade_aware)
    ordered = sorted(games, key=lambda g: (g.game_time_utc, g.game_id))
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j].game_time_utc == ordered[i].game_time_utc:
            j += 1
        group = ordered[i:j]
        for g in group:                     # 1) 先算整組特徵（狀態只含嚴格更早的比賽）
            rows.append(b.features(g))
        for g in group:                     # 2) 再併入結果（含傷病校準觀測）
            b.absorb(g)
        i = j
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["game_time_utc", "game_id"]).reset_index(drop=True)
    return (df, b.calib) if return_calibrator else df


def assert_no_leakage(df: pd.DataFrame) -> None:
    for side in ("home", "away"):
        col = df[f"{side}_asof_game_utc"]
        bad = df[col.notna() & (col >= df["game_time_utc"])]
        if len(bad):
            raise LeakageError(f"{side}: {len(bad)} 列用到不早於開賽時間的比賽，例如 game_id={bad.iloc[0]['game_id']}")
        age = df[f"{side}_inj_report_age_min"].dropna()
        if (age <= 0).any():
            raise LeakageError(f"{side}: 傷病報告時間不早於決策時點")
    if (df["inj_cutoff_utc"] > df["game_time_utc"]).any():
        raise LeakageError("決策時點晚於開賽")
    c = df["calib_asof_utc"]
    if (c.notna() & (c >= df["game_time_utc"])).any():
        raise LeakageError("傷病校準用到不早於開賽時間的觀測")


NON_FEATURE = {"game_id", "season", "game_time_utc", "home_team_id", "away_team_id", "inj_cutoff_utc",
               "calib_asof_utc", "calib_n_obs", "home_roster_departed_n", "away_roster_departed_n"}


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in NON_FEATURE and not c.startswith("y_")
            and not c.endswith("_asof_game_utc")]
