"""
execution-v1 portfolio controller + 決策 / 理論執行 / 結算 / bankroll ledger（純函式；不連 DB）
------------------------------------------------------------
D.3 size_pricings 是「某一時刻」的 static portfolio；真實執行有先後。execution-v1 包裝：

  每個 betting day（Asia/Taipei）：
    1. day_start_bankroll = 前一日結束 bankroll（= 起始 + 已結算損益 − 尚未結算的 stake；見 available_bankroll）
    2. 當日所有 stake 金額 = day_start_bankroll × stake_fraction（同一天內基準固定）
    3. 依 decision_time 排序；同一 decision_time 的比賽 = 同一批次（一起交給 D.3 sizing，與 game_id 無關）
    4. 批次的 D.3 final_stake_fraction 合計 > 剩餘當日額度（risk-v1 8% − 已 committed）→ 批次內等比例縮放
       （committed stake 占用額度；當日中途已結算的比賽不釋放、不增加額度）
    5. 全部結算後 day_end_bankroll = day_start_bankroll + Σ 當日損益；次日以此為基準
  只執行 D.3 actionable 且 final_stake_fraction > 0 的 outcome；risk-v1 參數不變。

每場比賽都有一筆 decision（含 no_bet 與原因）；decision 一旦產生不重做（避免 selection bias）。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable

from ..pricing import engine as pricing_engine
from ..sizing import engine as sizing_engine
from ..sizing.job import size_pricings
from ..sizing.policy import RISK_V1, SIZING_VERSION, RiskPolicy
from ..timeutil import ensure_utc, parse_utc
from . import asof, settlement
from .policy import EXECUTION_V1, ExecutionPolicy, StrategyScope, strategy_id

BET = "bet"
NO_BET = "no_bet"
BUDGET_EPS = 1e-12


# ------------------------------------------------------------------ #
# 資料結構                                                              #
# ------------------------------------------------------------------ #

@dataclass
class Wager:
    """一筆理論執行的 paper bet（依當時 snapshot / pricing outcome；結算時不重查目前盤口）。"""
    strategy_id: str
    game_id: int
    betting_day: date
    decision_time: datetime
    scheduled_tipoff: datetime
    odds_snapshot_id: int
    prediction_id: int | None
    source: str
    bookmaker: str
    market: str
    market_type: str
    period: str
    outcome_set: str
    side: str
    line: float | None
    display_line: float | None
    model_target: str
    model_threshold: float
    comparator: str
    settlement_rule: str
    decimal_odds: float
    p_win: float
    p_push: float
    p_loss: float
    ev_per_unit: float
    sizing_final_stake_fraction: float          # D.3 risk-v1 static 輸出
    execution_scale_factor: float               # execution-v1 剩餘當日額度縮放（≤ 1）
    stake_fraction: float                       # = sizing_final × execution_scale（day_start_bankroll 的比例）
    stake_units: float                          # = day_start_bankroll × stake_fraction（normalized bankroll 單位）
    expected_profit_units: float                # = stake_units × ev_per_unit（ex-ante）
    settlement_status: str = settlement.PENDING
    settlement_reason: str | None = None
    actual_value: float | None = None
    home_score: int | None = None
    away_score: int | None = None
    profit_units: float | None = None
    resolved_at: datetime | None = None         # 結果已知時間（歷史：tipoff + 4h；prospective：settled_at）

    @property
    def resolved(self) -> bool:
        return self.settlement_status in settlement.TERMINAL

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("betting_day", "decision_time", "scheduled_tipoff", "resolved_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


@dataclass
class GameDecision:
    strategy_id: str
    game_id: int
    betting_day: date
    scheduled_tipoff: datetime
    decision_time: datetime
    evaluated_at: datetime | None
    decision_status: str
    no_bet_reason: str | None
    blockers: dict[str, Any]
    odds_snapshot_ids: list[int]
    prediction_id: int | None
    prediction_available_at: datetime | None
    artifact_version: str | None
    model_version: str | None
    distribution_version: str | None
    pricing_fingerprint: str
    sizing_fingerprint: str
    sizing: list[dict[str, Any]]                 # 每個 outcome 的 D.3 結果摘要（含不可下注的）
    day_start_bankroll: float
    committed_fraction_before: float
    remaining_day_fraction_before: float
    execution_scale_factor: float
    total_stake_fraction: float
    total_stake_units: float
    wagers: list[Wager] = field(default_factory=list)

    def to_dict(self, *, with_wagers: bool = True) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "wagers"}
        for k in ("betting_day", "scheduled_tipoff", "decision_time", "evaluated_at", "prediction_available_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        if with_wagers:
            d["wagers"] = [w.to_dict() for w in self.wagers]
        return d


@dataclass
class DayState:
    betting_day: date
    day_start_bankroll: float
    committed_fraction: float = 0.0             # Σ 已 committed 的 stake_fraction（day_start 比例）


@dataclass
class DayLedger:
    betting_day: date
    day_start_bankroll: float
    n_decisions: int
    n_bets: int
    stake_fraction: float
    staked_units: float
    day_profit: float                           # Σ 已結算損益
    unresolved_stake: float                     # pending / ungradable 的 stake（不猜結果，不入帳）
    day_end_bankroll: float | None              # 全部結算後才有（= day_start + day_profit）
    day_close_bankroll: float                   # = day_start + day_profit − unresolved_stake（未結算時的保守值）
    closed: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["betting_day"] = self.betting_day.isoformat()
        return d


# ------------------------------------------------------------------ #
# 批次決策（D.3 sizing → execution-v1 sequential daily budget）          #
# ------------------------------------------------------------------ #

def _sizing_summary(r: sizing_engine.SizingResult) -> dict[str, Any]:
    return {"odds_snapshot_id": r.odds_snapshot_id, "market": r.market, "side": r.side, "line": r.line,
            "decimal_odds": r.decimal_odds, "ev_per_unit": r.ev_per_unit, "edge_vs_fair": r.edge_vs_fair,
            "full_kelly_fraction": r.full_kelly_fraction, "single_bet_capped_fraction": r.single_bet_capped_fraction,
            "final_stake_fraction": r.final_stake_fraction, "qualification_status": r.qualification_status,
            "actionable": r.actionable, "reasons": list(r.reasons), "warnings": list(r.warnings),
            "last_seen_age_seconds": r.last_seen_age_seconds}


def _fingerprint(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _derive_reason(results: list[sizing_engine.SizingResult]) -> str:
    """沒有執行任何 outcome 時的主要原因（D.3 狀態 → no-bet reason；固定優先序）。"""
    st = {r.qualification_status for r in results}
    if any(r.actionable for r in results):
        return asof.PORTFOLIO_CAP
    if sizing_engine.STALE_QUOTE in st:
        return asof.STALE_ODDS
    if sizing_engine.MUTUALLY_EXCLUSIVE in st:
        return asof.MUTUALLY_EXCLUSIVE
    if sizing_engine.NO_POSITIVE_EV in st:
        return asof.NO_POSITIVE_EV
    if st & {sizing_engine.INVALID_PROBABILITY, sizing_engine.UNAVAILABLE}:
        return asof.DATA_UNAVAILABLE
    if sizing_engine.UNSUPPORTED_SETTLEMENT in st:
        return asof.UNSUPPORTED_MARKET
    if sizing_engine.MARKET_NOT_OPEN in st:
        return asof.MARKET_NOT_OPEN
    if sizing_engine.NO_PREDICTION in st:
        return asof.NO_PREDICTION
    return asof.DATA_UNAVAILABLE


def execute_batch(recons: list[asof.Reconstruction], as_of: datetime, day: DayState, *, sid: str,
                  risk: RiskPolicy = RISK_V1, evaluated_at: datetime | None = None) -> list[GameDecision]:
    """同一 decision_time 的比賽：D.3 static sizing → 剩餘當日額度縮放 → 每場一筆 decision。會更新 day.committed_fraction。"""
    as_of = ensure_utc(as_of)
    if not recons:
        return []
    if any(r.decision_time != as_of or r.betting_day != day.betting_day for r in recons):
        raise ValueError("批次內比賽必須同一 decision_time、同一 betting day")
    pricings = [mp for r in recons for mp in r.pricings]
    starts = {r.game_id: r.scheduled_tipoff for r in recons}
    results = size_pricings(pricings, starts, as_of, policy=risk) if pricings else []
    by_game: dict[int, list[sizing_engine.SizingResult]] = {}
    for x in results:
        by_game.setdefault(x.game_id, []).append(x)
    outcome_spec = {(mp.odds_snapshot_id, o.side): (mp, o) for mp in pricings for o in mp.outcomes}

    rec_by_game = {r.game_id: r for r in recons}
    executable = [x for x in results if x.actionable and x.final_stake_fraction > 0
                  and rec_by_game[x.game_id].pre_reason is None]
    exec_ids = {id(x) for x in executable}
    batch_total = math.fsum(x.final_stake_fraction for x in executable)
    committed_before = day.committed_fraction
    remaining = max(0.0, risk.max_day_fraction - committed_before)
    if remaining <= BUDGET_EPS:
        scale = 0.0
    elif batch_total <= remaining:
        scale = 1.0
    else:
        scale = remaining / batch_total

    decisions = []
    for rec in sorted(recons, key=lambda r: r.game_id):
        rs = by_game.get(rec.game_id, [])
        if rec.pre_reason is not None and any(x.actionable for x in rs):
            raise AssertionError(f"game {rec.game_id}：gate {rec.pre_reason} 但 D.3 有 actionable outcome（不一致）")
        wagers = []
        for x in rs:
            if id(x) not in exec_ids or scale <= 0.0:
                continue
            mp, o = outcome_spec[(x.odds_snapshot_id, x.side)]
            frac = x.final_stake_fraction * scale
            stake = day.day_start_bankroll * frac
            wagers.append(Wager(
                strategy_id=sid, game_id=rec.game_id, betting_day=rec.betting_day, decision_time=as_of,
                scheduled_tipoff=rec.scheduled_tipoff, odds_snapshot_id=int(x.odds_snapshot_id),
                prediction_id=x.prediction_id, source=x.source, bookmaker=x.bookmaker, market=x.market,
                market_type=x.market_type, period=x.period, outcome_set=x.outcome_set, side=x.side, line=x.line,
                display_line=x.display_line, model_target=o.model_target, model_threshold=o.model_threshold,
                comparator=o.comparator, settlement_rule=x.settlement_rule, decimal_odds=x.decimal_odds,
                p_win=x.p_win, p_push=x.p_push, p_loss=x.p_loss, ev_per_unit=x.ev_per_unit,
                sizing_final_stake_fraction=x.final_stake_fraction, execution_scale_factor=scale,
                stake_fraction=frac, stake_units=stake, expected_profit_units=stake * x.ev_per_unit))
        if rec.pre_reason is not None:
            status, reason = NO_BET, rec.pre_reason
        elif wagers:
            status, reason = BET, None
        else:
            status, reason = NO_BET, _derive_reason(rs)
        pred = rec.prediction
        mps = [mp for mp in rec.pricings if mp.distribution_version]
        sizing_rows = [_sizing_summary(x) for x in rs]
        decisions.append(GameDecision(
            strategy_id=sid, game_id=rec.game_id, betting_day=rec.betting_day, scheduled_tipoff=rec.scheduled_tipoff,
            decision_time=as_of, evaluated_at=ensure_utc(evaluated_at) if evaluated_at else None,
            decision_status=status, no_bet_reason=reason, blockers=dict(rec.blockers),
            odds_snapshot_ids=list(rec.odds_snapshot_ids), prediction_id=pred.prediction_id if pred else None,
            prediction_available_at=pred.available_at if pred else None, artifact_version=rec.artifact_version,
            model_version=pred.model_version if pred else None,
            distribution_version=mps[0].distribution_version if mps else None,
            pricing_fingerprint=rec.pricing_fingerprint(),
            sizing_fingerprint=_fingerprint([x.to_dict() for x in rs]), sizing=sizing_rows,
            day_start_bankroll=day.day_start_bankroll, committed_fraction_before=committed_before,
            remaining_day_fraction_before=remaining, execution_scale_factor=scale if wagers else 0.0,
            total_stake_fraction=math.fsum(w.stake_fraction for w in wagers),
            total_stake_units=math.fsum(w.stake_units for w in wagers), wagers=wagers))
    day.committed_fraction = committed_before + math.fsum(d.total_stake_fraction for d in decisions)
    return decisions


def missed_decision(game: dict, *, sid: str, scope: StrategyScope, day: DayState, policy: ExecutionPolicy,
                    risk: RiskPolicy, evaluated_at: datetime) -> GameDecision:
    """prospective：job 晚於 T 超過 max_evaluation_lag → 仍記錄 decision（no_bet），不事後補做。"""
    tip = ensure_utc(parse_utc(game["date_utc"]))
    t = asof.decision_time(tip, policy)
    lag = (ensure_utc(evaluated_at) - t).total_seconds()
    return GameDecision(
        strategy_id=sid, game_id=int(game["id"]), betting_day=asof.betting_day(tip), scheduled_tipoff=tip,
        decision_time=t, evaluated_at=ensure_utc(evaluated_at), decision_status=NO_BET,
        no_bet_reason=asof.DECISION_WINDOW_MISSED, blockers={"evaluation_lag_seconds": lag,
                                                             "max_evaluation_lag_seconds":
                                                                 policy.max_evaluation_lag.total_seconds()},
        odds_snapshot_ids=[], prediction_id=None, prediction_available_at=None, artifact_version=None,
        model_version=None, distribution_version=None, pricing_fingerprint=_fingerprint([]),
        sizing_fingerprint=_fingerprint([]), sizing=[], day_start_bankroll=day.day_start_bankroll,
        committed_fraction_before=day.committed_fraction,
        remaining_day_fraction_before=max(0.0, risk.max_day_fraction - day.committed_fraction),
        execution_scale_factor=0.0, total_stake_fraction=0.0, total_stake_units=0.0)


# ------------------------------------------------------------------ #
# Bankroll                                                             #
# ------------------------------------------------------------------ #

def available_bankroll(starting: float, prior_wagers: Iterable[Wager], at: datetime) -> float:
    """at 時點可用 bankroll = 起始 + Σ（at 以前已結算）損益 − Σ（at 時仍未結算）stake。
    前一日全部結算 → 等於前一日 day_end_bankroll；未結算的 stake 視為仍在外（不猜結果、不放大後面的注碼）。"""
    at = ensure_utc(at)
    pnl, out = [], []
    for w in prior_wagers:
        if w.resolved and w.resolved_at is not None and ensure_utc(w.resolved_at) <= at:
            pnl.append(w.profit_units)
        else:
            out.append(w.stake_units)
    return float(starting) + math.fsum(pnl) - math.fsum(out)


def settle_wager(w: Wager, game: dict, *, policy: ExecutionPolicy, resolved_at: datetime | None,
                 decision_scheduled_tipoff: datetime | None = None) -> Wager:
    s = settlement.settle(w.__dict__, game, decision_scheduled_tipoff=decision_scheduled_tipoff, policy=policy)
    w.settlement_status, w.settlement_reason = s.status, s.reason
    w.actual_value, w.home_score, w.away_score, w.profit_units = s.actual_value, s.home_score, s.away_score, \
        s.profit_units
    w.resolved_at = ensure_utc(resolved_at) if (s.terminal and resolved_at is not None) else None
    return w


def day_ledgers(decisions: list[GameDecision]) -> list[DayLedger]:
    by_day: dict[date, list[GameDecision]] = {}
    for d in decisions:
        by_day.setdefault(d.betting_day, []).append(d)
    out = []
    for day in sorted(by_day):
        ds = by_day[day]
        starts = {d.day_start_bankroll for d in ds}
        if len(starts) != 1:
            raise AssertionError(f"{day} 的 day_start_bankroll 不一致：{starts}")
        start = starts.pop()
        ws = [w for d in ds for w in d.wagers]
        resolved = [w.profit_units for w in ws if w.resolved]
        unresolved = math.fsum(w.stake_units for w in ws if not w.resolved)
        profit = math.fsum(resolved)
        closed = all(w.resolved for w in ws)
        out.append(DayLedger(day, start, len(ds), len(ws), math.fsum(w.stake_fraction for w in ws),
                             math.fsum(w.stake_units for w in ws), profit, unresolved,
                             start + profit if closed else None, start + profit - unresolved, closed))
    return out


# ------------------------------------------------------------------ #
# 模擬（歷史重建 / fixture validation 共用）                              #
# ------------------------------------------------------------------ #

@dataclass
class SimulationResult:
    strategy_id: str
    scope: StrategyScope
    policy: ExecutionPolicy
    risk: RiskPolicy
    starting_bankroll: float
    decisions: list[GameDecision]
    days: list[DayLedger]

    @property
    def wagers(self) -> list[Wager]:
        return [w for d in self.decisions for w in d.wagers]


def simulate(games: list[dict], odds_rows: list[dict], pred_rows: list[dict], runs: list[dict], *,
             scope: StrategyScope, model_for: Callable, artifacts: asof.ArtifactRegistry,
             policy: ExecutionPolicy = EXECUTION_V1, risk: RiskPolicy = RISK_V1,
             starting_bankroll: float | None = None) -> SimulationResult:
    """依 betting day → decision_time 順序逐批決策；每日結束後依比賽結果結算；下一日用結算後 bankroll。
    games 需含 id、date_utc、status、比分欄位（結算用；決策只用 date_utc）。所有輸入列都會再以 T 過濾。"""
    start_units = policy.starting_bankroll_units if starting_bankroll is None else float(starting_bankroll)
    sid = strategy_id(policy, risk, scope)
    odds_rows = [r for r in odds_rows if scope.matches(r)]
    games_by_id = {int(g["id"]): g for g in games}
    by_day: dict[date, list[dict]] = {}
    for g in games:
        by_day.setdefault(asof.betting_day(g["date_utc"]), []).append(g)
    decisions: list[GameDecision] = []
    for day in sorted(by_day):
        batches: dict[datetime, list[dict]] = {}
        for g in by_day[day]:
            batches.setdefault(asof.decision_time(g["date_utc"], policy), []).append(g)
        first_t = min(batches)
        prior = [w for d in decisions for w in d.wagers]
        state = DayState(day, available_bankroll(start_units, prior, first_t))
        for t in sorted(batches):
            recons = [asof.reconstruct_game(g, t, scope=scope, odds_rows=odds_rows, pred_rows=pred_rows, runs=runs,
                                            risk=risk, policy=policy, model_for=model_for, artifacts=artifacts)
                      for g in sorted(batches[t], key=lambda g: int(g["id"]))]
            decisions.extend(execute_batch(recons, t, state, sid=sid, risk=risk))
        for d in decisions:
            if d.betting_day != day:
                continue
            for w in d.wagers:
                settle_wager(w, games_by_id[w.game_id], policy=policy,
                             resolved_at=w.scheduled_tipoff + policy.result_known_after_tipoff)
    return SimulationResult(sid, scope, policy, risk, start_units, decisions, day_ledgers(decisions))
