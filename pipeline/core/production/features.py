"""
未開賽比賽的賽前特徵（C.5D）＋ 訓練 / 推論共用的「模型輸入表」組裝
------------------------------------------------------------
production 模型的輸入 = pregame-v2.1（temporal_features）+ Phase C 比賽層級特徵（ml_features）+ Elo。
三者在訓練時都是「對歷史比賽」用 walk-forward 算出來的；這裡提供**不需要該場結果**的推論路徑：

  * pregame-v2.1：同一個 _Builder，先把「預測時間戳之前已結束」的比賽依時間 absorb() 進狀態，
    再對未開賽比賽呼叫 features()（features 只讀狀態，不需要、也拿不到該場結果）。傷病 cutoff = 預測時間戳；
    傷病校準器是 artifact 內凍結的狀態（不可再學）。
  * Phase C（elo_diff 之外的 b2b/rest/form10/margin10/total_est/h1_total_est/games_7d）：PhaseCState 以每隊已結束
    比賽的序列重現 ml_features.build_features 的 shift(1)+rolling 語意（不靠「塞一列假結果再跑 pandas」）。
  * Elo：models/elo_state 重播。

**不得使用**：該場比分 / box score、開賽後的傷病報告、評測專用欄位（elo_p 等）、未來的名單資訊
（players.team_id 是「現在」的球隊，任何特徵都不讀它）。

已排定但尚未完賽的「前一場」（例如背靠背的第一場還沒打）：賽程是賽前已知的，休息 / 背靠背 / 7 日場數照算；
它的結果未知，所以近況類統計停在最後一場已完賽比賽，並在資料品質旗標標示 pending_prior_game。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd

from ..injury_asof import InjuryIndex
from ..models import temporal_features as tf
from ..models import temporal_model as tm
from ..models.elo_state import EloState
from ..models.ml_features import WIN_FEATURES as PHASE_C_WIN_FEATURES
from ..models.pregame_features import GameRecord, LeakageError
from ..timeutil import ensure_utc

PHASE_C_STAGES = ("regular", "playoffs")                 # ml_features.load_games 的賽事集合（不含附加賽）
HISTORY_STAGES = ("regular", "playin", "playoffs")       # pregame_features.load_inputs / backtest 的賽事集合
V2_SCHEDULE_COLS = [f"{s}_{c}" for s in ("home", "away") for c in ("rest_days", "b2b", "games_7d")]
PHASE_C_ROLL = {"form10": "win", "margin10": "margin", "pf10": "pf", "pa10": "pa", "h1f10": "h1f", "h1a10": "h1a"}
# 模型輸入表需要的 Phase C 欄位（production 特徵組 + add_model_columns 會引用者）
PHASE_C_COLUMNS = ["home_rest_days", "away_rest_days", "home_b2b", "away_b2b", "home_games_7d", "away_games_7d",
                   "home_form10", "away_form10", "home_margin10", "away_margin10", "total_est", "h1_total_est",
                   "is_playoffs", "rest_diff", "b2b_diff", "form10_diff", "margin10_diff"]


# ------------------------------------------------------------------ #
# 輸入                                                                 #
# ------------------------------------------------------------------ #

@dataclass
class HistoryInputs:
    """已結束比賽（status=final、有比分）與其 box score 衍生資料、傷病快照索引。"""
    games: list[GameRecord]
    stages: dict[int, str]
    derived: dict[tuple[int, int], dict]
    players: dict[int, list[tuple]]
    injury_index: InjuryIndex
    as_of: datetime | None = None            # 由 DB 載入時的時間戳（只收開賽早於它的比賽）

    def before(self, cutoff: datetime) -> "HistoryInputs":
        """只保留開賽嚴格早於 cutoff 的比賽、報告嚴格早於 cutoff 的傷病快照（測試 / 重現用）。"""
        cutoff = ensure_utc(cutoff)
        games = [g for g in self.games if ensure_utc(g.game_time_utc) < cutoff]
        keep = {g.game_id for g in games}
        snaps = [s for s in self.injury_index.snapshots if s.report_time_utc < cutoff]
        return HistoryInputs(games, {k: v for k, v in self.stages.items() if k in keep},
                             {k: v for k, v in self.derived.items() if k[0] in keep},
                             {k: v for k, v in self.players.items() if k in keep}, InjuryIndex(snaps), cutoff)


@dataclass
class ScheduledGame:
    game_id: int
    season: str
    season_stage: str
    game_time_utc: datetime
    home_team_id: int
    away_team_id: int
    home_abbr: str
    away_abbr: str
    nba_game_id: str | None = None
    status: str = "scheduled"

    def record(self) -> GameRecord:
        """沒有比分的 GameRecord（features() 不會讀比分；y_* 因此為 None）。"""
        return GameRecord(self.game_id, self.season, ensure_utc(self.game_time_utc), self.home_team_id,
                          self.away_team_id, self.home_abbr, self.away_abbr, None, None, None, None)


# ------------------------------------------------------------------ #
# Phase C 比賽層級特徵（ml_features.build_features 的逐隊狀態版）           #
# ------------------------------------------------------------------ #

@dataclass
class _PCRow:
    time: datetime
    game_id: int
    vals: dict[str, float]


@dataclass
class PhaseCState:
    rows: dict[int, list[_PCRow]] = field(default_factory=lambda: defaultdict(list))

    def absorb(self, g: GameRecord, stage: str) -> None:
        if stage not in PHASE_C_STAGES:
            return
        for tid, pf, pa, h1f, h1a in ((g.home_team_id, g.home_pts, g.away_pts, g.home_h1, g.away_h1),
                                      (g.away_team_id, g.away_pts, g.home_pts, g.away_h1, g.home_h1)):
            v = {"pf": float(pf), "pa": float(pa), "win": float(pf > pa), "margin": float(pf - pa),
                 "h1f": float("nan") if h1f is None else float(h1f),
                 "h1a": float("nan") if h1a is None else float(h1a)}
            self.rows[tid].append(_PCRow(ensure_utc(g.game_time_utc), g.game_id, v))

    def team(self, tid: int, tip: datetime, prior: Iterable[datetime] = ()) -> dict[str, float]:
        """某隊在 tip 開賽前的 Phase C 特徵。prior = 已排定未完賽的較早場次（只影響賽程類）。"""
        tip = ensure_utc(tip)
        hist = [r for r in self.rows.get(tid, []) if r.time < tip]
        times = [r.time for r in hist] + sorted(ensure_utc(x) for x in prior if ensure_utc(x) < tip)
        out: dict[str, float] = {}
        if times:
            gap = (tip - max(times)).total_seconds() / 86400.0
            out["rest_days"] = float(min(max(gap - 1.0, 0.0), 7.0))
            out["b2b"] = float(out["rest_days"] == 0)
        else:
            out["rest_days"] = out["b2b"] = float("nan")
        out["games_7d"] = float(sum(1 for x in times if (tip - x).total_seconds() <= 7 * 86400))
        last10 = hist[-10:]
        for name, key in PHASE_C_ROLL.items():
            xs = [r.vals[key] for r in last10 if not math.isnan(r.vals[key])]
            out[name] = float(np.mean(xs)) if len(xs) >= 3 else float("nan")
        return out


def phase_c_row(st: PhaseCState, g: ScheduledGame | GameRecord, stage: str, tip: datetime,
                prior: dict[int, list[datetime]] | None = None) -> dict[str, Any]:
    prior = prior or {}
    h = st.team(g.home_team_id, tip, prior.get(g.home_team_id, ()))
    a = st.team(g.away_team_id, tip, prior.get(g.away_team_id, ()))
    row: dict[str, Any] = {"game_id": g.game_id}
    for side, d in (("home", h), ("away", a)):
        for k in ("rest_days", "b2b", "games_7d", "form10", "margin10"):
            row[f"{side}_{k}"] = d[k]
    row["total_est"] = (h["pf10"] + h["pa10"] + a["pf10"] + a["pa10"]) / 2
    row["h1_total_est"] = (h["h1f10"] + h["h1a10"] + a["h1f10"] + a["h1a10"]) / 2
    row["is_playoffs"] = int(stage == "playoffs")
    row["rest_diff"] = h["rest_days"] - a["rest_days"]
    row["b2b_diff"] = (0.0 if math.isnan(h["b2b"]) else h["b2b"]) - (0.0 if math.isnan(a["b2b"]) else a["b2b"])
    row["form10_diff"] = h["form10"] - a["form10"]
    row["margin10_diff"] = h["margin10"] - a["margin10"]
    return row


# ------------------------------------------------------------------ #
# 模型輸入表（訓練與推論共用）                                             #
# ------------------------------------------------------------------ #

def model_frame(v2: pd.DataFrame, phase_c: pd.DataFrame, elo: pd.DataFrame) -> pd.DataFrame:
    """v2 特徵 + Phase C 特徵 + Elo → 一列一場。與 c5c_evaluate.assemble 的模型欄位語意相同
    （v2 與 Phase C 同名的賽程欄位，v2 版加 _v2 後綴），但不含 y_{side}_*（只在訓練時另外加）與評測專用欄位。
    只保留 Phase C 賽事集合（inner join）與有 Elo 的比賽。"""
    pc = phase_c[["game_id"] + PHASE_C_COLUMNS]
    out = v2.rename(columns={c: c + "_v2" for c in V2_SCHEDULE_COLS})
    out = out.merge(pc, on="game_id", how="inner")
    out = out.merge(elo[["game_id", "elo_home", "elo_away"]], on="game_id", how="left")
    out["elo_diff"] = out["elo_home"] - out["elo_away"]
    return out.dropna(subset=["elo_diff"]).sort_values(["game_time_utc", "game_id"]).reset_index(drop=True)


def side_targets(frame: pd.DataFrame, games: dict[int, GameRecord], derived: dict) -> pd.DataFrame:
    """y_{side}_{m}：每隊該場實際值，只供收縮/先驗參數擬合（與 c5c_evaluate.assemble 相同定義）。"""
    rename = {v: k for k, v in tf.EST_RENAME.items()}
    real: dict[str, list] = {f"y_{s}_{m}": [] for s in tm.SIDES for m in tm.BLEND_METRICS}
    for gid in frame["game_id"]:
        g = games[gid]
        for side, tid, pts, opts, h1, h1o in (
                ("home", g.home_team_id, g.home_pts, g.away_pts, g.home_h1, g.away_h1),
                ("away", g.away_team_id, g.away_pts, g.home_pts, g.away_h1, g.home_h1)):
            d = derived.get((gid, tid)) or {}
            for m in tm.BLEND_METRICS:
                if m in rename:
                    v = d.get(rename[m])
                elif m == "pts":
                    v = pts
                elif m == "opp_pts":
                    v = opts
                elif m == "margin":
                    v = pts - opts
                elif m == "h1_pts":
                    v = h1
                elif m == "h1_opp_pts":
                    v = h1o
                else:
                    v = None if h1 is None or h1o is None else h1 - h1o
                real[f"y_{side}_{m}"].append(np.nan if v is None else float(v))
    return frame.assign(**real)


class FillsError(KeyError):
    pass


def finalize(frame: pd.DataFrame, blend_params: dict, fills: dict[str, float]) -> pd.DataFrame:
    """推論用：套用 artifact 內的收縮參數與補值常數。補值常數缺任何一個 → 例外（不可在推論時重新計算）。"""
    out, used = tm.add_model_columns(tm.apply_blends(frame, blend_params), dict(fills))
    extra = set(used) - set(fills)
    if extra:
        raise FillsError(f"artifact 缺少補值常數 {sorted(extra)}（推論不可由推論資料重新計算）")
    return out


# ------------------------------------------------------------------ #
# 推論：未開賽比賽的特徵                                                   #
# ------------------------------------------------------------------ #

@dataclass
class UpcomingContext:
    """一次 absorb 完歷史後，可對多場未開賽比賽（不同 profile 的凍結校準器）計算特徵。"""
    builder: Any
    phase_c: PhaseCState
    elo: EloState
    history_last_utc: datetime | None
    n_history: int


def prepare_context(history: HistoryInputs, as_of: datetime, *, trade_aware: bool = True) -> UpcomingContext:
    """把開賽嚴格早於 as_of 的已結束比賽依時間併入狀態（只 absorb，不計算歷史特徵、不更新校準器）。"""
    as_of = ensure_utc(as_of)
    games = sorted(history.games, key=lambda g: (g.game_time_utc, g.game_id))
    late = [g.game_id for g in games if ensure_utc(g.game_time_utc) >= as_of]
    if late:
        raise LeakageError(f"歷史輸入含開賽不早於預測時間戳的比賽：{late[:5]}")
    frozen = tf.InjuryCalibrator(frozen=True)
    b = tf._Builder(history.derived, history.players, history.injury_index, tf.make_cutoff("T-60"),
                    trade_aware=trade_aware, calibrator=frozen)
    pc, el = PhaseCState(), EloState()
    for g in games:
        if g.home_pts is None or g.away_pts is None:
            raise ValueError(f"歷史輸入含未結束比賽 game_id={g.game_id}")
        b.absorb(g)
        st = history.stages.get(g.game_id, "regular")
        pc.absorb(g, st)
        el.absorb(g.home_team_id, g.away_team_id, g.season, g.home_pts, g.away_pts)
    last = ensure_utc(games[-1].game_time_utc) if games else None
    return UpcomingContext(b, pc, el, last, len(games))


def upcoming_rows(ctx: UpcomingContext, upcoming: list[ScheduledGame], as_of: datetime, calibrator,
                  pending: list[ScheduledGame] | None = None) -> pd.DataFrame:
    """對未開賽比賽算 v2 + Phase C + Elo 的模型輸入表（尚未套收縮參數）。
    calibrator：artifact 內凍結的 InjuryCalibrator；pending：已排定但尚未完賽的其他比賽（只用其開賽時間）。"""
    as_of = ensure_utc(as_of)
    if not getattr(calibrator, "frozen", False):
        raise ValueError("推論必須使用凍結的傷病校準器")
    ctx.builder.calib = calibrator
    pending = pending or []
    v2_last = {tid: t.last_time for tid, t in ctx.builder.teams.items() if t.last_time is not None}
    pc_last = {tid: rows[-1].time for tid, rows in ctx.phase_c.rows.items() if rows}
    schedule_prior = scheduled_prior_map(upcoming, pending, v2_last, stages=HISTORY_STAGES)
    pc_prior = scheduled_prior_map(upcoming, pending, pc_last, stages=PHASE_C_STAGES)
    v2_rows, pc_rows, elo_rows = [], [], []
    for sg in upcoming:
        tip = ensure_utc(sg.game_time_utc)
        if tip <= as_of:
            raise LeakageError(f"game_id={sg.game_id} 已開賽（{tip.isoformat()} ≤ 預測時間 {as_of.isoformat()}）")
        prior = schedule_prior.get(sg.game_id, {})
        row = ctx.builder.features(sg.record(), cutoff=as_of, schedule_prior=prior)
        ctx.builder.pending_obs.pop(sg.game_id, None)       # 推論不收集校準觀測
        row["season_stage"] = sg.season_stage
        v2_rows.append(row)
        pc_rows.append(phase_c_row(ctx.phase_c, sg, sg.season_stage, tip, pc_prior.get(sg.game_id, {})))
        eh, ea = ctx.elo.pregame(sg.home_team_id, sg.away_team_id, sg.season)
        elo_rows.append({"game_id": sg.game_id, "elo_home": eh, "elo_away": ea})
    v2 = pd.DataFrame(v2_rows)
    for side in ("home", "away"):
        bad = v2[v2[f"{side}_asof_game_utc"].notna() & (v2[f"{side}_asof_game_utc"] >= v2["game_time_utc"])]
        if len(bad):
            raise LeakageError(f"{side}: 用到不早於開賽的比賽")
        age = v2[f"{side}_inj_report_age_min"].dropna()
        if (age <= 0).any():
            raise LeakageError("傷病報告不早於預測時間戳")
    return model_frame(v2, pd.DataFrame(pc_rows), pd.DataFrame(elo_rows))


def scheduled_prior_map(upcoming: list[ScheduledGame], pending: list[ScheduledGame],
                        last_final: dict[int, datetime], *, stages: Iterable[str] = HISTORY_STAGES
                        ) -> dict[int, dict[int, list[datetime]]]:
    """每場未開賽比賽、每隊：在它之前、最後一場已完賽比賽之後、尚未完賽的場次（含其他 upcoming 比賽）。
    stages：哪些賽事階段算數（v2 含附加賽；Phase C 的賽程欄位不含附加賽，與訓練時的賽事集合一致）。
    last_final：各隊最後一場已完賽（且屬於同一個賽事集合）的開賽時間。"""
    stages = set(stages)
    sched = [o for o in pending + upcoming if o.season_stage in stages]
    out: dict[int, dict[int, list[datetime]]] = {}
    for g in upcoming:
        tip = ensure_utc(g.game_time_utc)
        per: dict[int, list[datetime]] = {}
        for tid in (g.home_team_id, g.away_team_id):
            lf = last_final.get(tid)
            xs = sorted({ensure_utc(o.game_time_utc) for o in sched
                         if o.game_id != g.game_id and tid in (o.home_team_id, o.away_team_id)
                         and ensure_utc(o.game_time_utc) < tip and (lf is None or ensure_utc(o.game_time_utc) > lf)
                         and tip - ensure_utc(o.game_time_utc) <= timedelta(days=8)})
            if xs:
                per[tid] = xs
        out[g.game_id] = per
    return out


# ------------------------------------------------------------------ #
# 訓練：歷史比賽的模型輸入表                                                #
# ------------------------------------------------------------------ #

def phase_c_frame(history: HistoryInputs, elo_before: dict[int, tuple[float, float]]) -> pd.DataFrame:
    """歷史 Phase C 特徵：直接用 ml_features.build_features（訓練路徑與 C.5C 相同的 pandas 實作）。"""
    from ..models.ml_features import build_features
    rows = []
    for g in history.games:
        st = history.stages.get(g.game_id, "regular")
        if st not in PHASE_C_STAGES:
            continue
        eh, ea = elo_before[g.game_id]
        rows.append({"game_id": g.game_id, "season": g.season, "season_stage": st, "date_utc": g.game_time_utc,
                     "home_team_id": g.home_team_id, "away_team_id": g.away_team_id, "home_pts": g.home_pts,
                     "away_pts": g.away_pts, "home_h1": g.home_h1, "away_h1": g.away_h1,
                     "elo_home": eh, "elo_away": ea})
    df = pd.DataFrame(rows)
    df["date_utc"] = pd.to_datetime(df["date_utc"], utc=True)
    for c in ("home_h1", "away_h1"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return build_features(df)


def elo_frame(elo_before: dict[int, tuple[float, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"game_id": k, "elo_home": v[0], "elo_away": v[1]} for k, v in elo_before.items()])


def build_training_frames(history: HistoryInputs) -> dict[str, tuple[pd.DataFrame, Any]]:
    """{profile: (含 y_{side}_* 的模型輸入表, walk-forward 傷病校準器)}；Phase C / Elo 與 profile 無關只算一次。"""
    from ..models import elo_state
    from . import spec
    games_by_id = {g.game_id: g for g in history.games}
    _, elo_before = elo_state.replay(history.games)
    pc = phase_c_frame(history, elo_before)
    elo = elo_frame(elo_before)
    out = {}
    for name, timing in spec.PROFILES.items():
        v2, cal = tf.build_temporal_features(history.games, history.derived, history.players, history.injury_index,
                                             timing=timing, return_calibrator=True)
        tf.assert_no_leakage(v2)
        out[name] = (side_targets(model_frame(v2, pc, elo), games_by_id, history.derived), cal)
    return out


__all__ = ["HistoryInputs", "ScheduledGame", "PhaseCState", "phase_c_row", "model_frame", "side_targets",
           "finalize", "prepare_context", "upcoming_rows", "scheduled_prior_map", "phase_c_frame", "elo_frame",
           "PHASE_C_WIN_FEATURES"]
