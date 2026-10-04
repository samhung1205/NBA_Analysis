"""
Strategy metrics / bankroll path / day-block bootstrap / descriptive subgroups（純函式）
------------------------------------------------------------
定義（兩個都不叫 ROI）：
    total_staked        Σ stake（只計 graded：win / loss / push / draw_win；void、pending、ungradable 另列）
    net_profit          Σ profit（已結算；void = 0）
    yield               net_profit / total_staked                       （= 每單位投注額的報酬，turnover-based）
    bankroll_return     ending_bankroll / starting_bankroll − 1         （資金曲線的報酬）
    ending_bankroll     starting + net_profit − unresolved_stake        （未結算 stake 不入帳、不猜結果）
    log_growth          ln(ending_bankroll / starting_bankroll)
    max_drawdown        bankroll 路徑（起始 + 每個 betting day 的 day_close）的最大 peak-to-trough（絕對值與 %）
    expected_profit     Σ stake × ev_per_unit（ex-ante；與 realized 比較只作 calibration / variance 診斷，不調任何參數）

Bootstrap：抽樣單位 = betting day（同場 / 同日 bets 相關，不做逐 bet IID）。
    至少 MIN_BOOTSTRAP_DAYS 個有 graded bet 的 betting day 才輸出 CI；否則 insufficient_sample。
    30 的依據：cluster bootstrap 在 cluster 數 < ~30 時 percentile 區間覆蓋率明顯不足（few-clusters problem），
    且日報酬分佈厚尾（單日上限 8%）——這是統計穩定性的門檻，不是看結果決定的。

Subgroup：只作描述（exploratory / descriptive），不得據以加入 strategy filter、最低 EV 門檻或任何參數。
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, timedelta
from typing import Any, Iterable

import numpy as np

from . import settlement
from .engine import NO_BET, DayLedger, GameDecision, Wager

MIN_BOOTSTRAP_DAYS = 30
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261004
EV_BINS = ((0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, math.inf))
ODDS_BINS = ((1.0, 1.8), (1.8, 2.2), (2.2, 3.0), (3.0, math.inf))
EARLY_SEASON_DAYS = 30
DESCRIPTIVE_LABEL = "exploratory_descriptive_only"


def _div(a: float, b: float) -> float | None:
    return a / b if b else None


def _mean(xs: list[float]) -> float | None:
    return math.fsum(xs) / len(xs) if xs else None


def max_drawdown(path: list[float]) -> dict[str, Any]:
    """path：bankroll 序列（含起點）。回傳最大絕對回落與最大 % 回落（peak-to-trough）。"""
    peak = -math.inf
    best_abs = best_pct = 0.0
    at_abs = at_pct = None
    for i, v in enumerate(path):
        peak = max(peak, v)
        dd = peak - v
        pct = dd / peak if peak > 0 else 0.0
        if dd > best_abs:
            best_abs, at_abs = dd, i
        if pct > best_pct:
            best_pct, at_pct = pct, i
    return {"max_drawdown_units": best_abs, "max_drawdown_pct": best_pct, "trough_index_abs": at_abs,
            "trough_index_pct": at_pct}


def bankroll_path(starting: float, days: list[DayLedger]) -> list[float]:
    return [float(starting)] + [d.day_close_bankroll for d in days]


def strategy_metrics(decisions: list[GameDecision], days: list[DayLedger], starting_bankroll: float
                     ) -> dict[str, Any]:
    ws: list[Wager] = [w for d in decisions for w in d.wagers]
    st = Counter(w.settlement_status for w in ws)
    graded = [w for w in ws if w.settlement_status in settlement.GRADED]
    resolved = [w for w in ws if w.resolved]
    unresolved = [w for w in ws if not w.resolved]
    staked = math.fsum(w.stake_units for w in graded)
    net = math.fsum(w.profit_units for w in resolved)
    unresolved_stake = math.fsum(w.stake_units for w in unresolved)
    ending = float(starting_bankroll) + net - unresolved_stake
    exp = math.fsum(w.expected_profit_units for w in graded)
    realized_graded = math.fsum(w.profit_units for w in graded)
    var = math.fsum(w.stake_units ** 2 * (w.p_win * (w.decimal_odds - 1) ** 2 + w.p_loss - w.ev_per_unit ** 2)
                    for w in graded)
    n_dec = len(decisions)
    n_bet_dec = sum(d.decision_status != NO_BET for d in decisions)
    dd = max_drawdown(bankroll_path(starting_bankroll, days))
    bet_days = [d for d in days if d.n_bets]
    worst = min(bet_days, key=lambda d: (d.day_profit, d.betting_day), default=None)
    best = max(bet_days, key=lambda d: (d.day_profit, d.betting_day), default=None)

    def day_info(d: DayLedger | None):
        return None if d is None else {"betting_day": d.betting_day.isoformat(), "day_profit_units": d.day_profit,
                                       "day_return": _div(d.day_profit, d.day_start_bankroll), "n_bets": d.n_bets}

    return {
        "n_decisions": n_dec, "n_bet_decisions": n_bet_dec, "n_no_bet": n_dec - n_bet_dec,
        "action_rate": _div(n_bet_dec, n_dec),
        "no_bet_reasons": dict(sorted(Counter(d.no_bet_reason for d in decisions if d.decision_status == NO_BET)
                                      .items())),
        "n_bets": len(ws), "wins": st[settlement.SETTLED_WIN] + st[settlement.SETTLED_DRAW_WIN],
        "draw_wins": st[settlement.SETTLED_DRAW_WIN], "losses": st[settlement.SETTLED_LOSS],
        "pushes": st[settlement.SETTLED_PUSH], "void": st[settlement.VOID], "ungradable": st[settlement.UNGRADABLE],
        "pending": st[settlement.PENDING], "unresolved_stake_units": unresolved_stake,
        "total_staked_units": staked, "turnover_units": staked, "net_profit_units": net,
        "yield": _div(realized_graded, staked),
        "starting_bankroll_units": float(starting_bankroll), "ending_bankroll_units": ending,
        "bankroll_return": ending / float(starting_bankroll) - 1.0,
        "bankroll_status": "complete" if not unresolved else "provisional_unresolved",
        "log_bankroll_growth": math.log(ending / float(starting_bankroll)) if ending > 0 else None,
        "mean_log_growth_per_bet_day": (math.log(ending / float(starting_bankroll)) / len(bet_days))
        if bet_days and ending > 0 else None,
        "avg_stake_fraction": _mean([w.stake_fraction for w in ws]),
        "avg_decimal_odds": _mean([w.decimal_odds for w in ws]),
        "avg_ev_per_unit": _mean([w.ev_per_unit for w in ws]),
        "stake_weighted_ev_per_unit": _div(math.fsum(w.stake_units * w.ev_per_unit for w in ws),
                                           math.fsum(w.stake_units for w in ws)),
        "expected_profit_units": exp, "realized_profit_units": realized_graded,
        "realized_minus_expected_units": realized_graded - exp,
        "model_profit_sd_units": math.sqrt(var) if var > 0 else None,
        "realized_vs_expected_z": (realized_graded - exp) / math.sqrt(var) if var > 0 else None,
        **dd, "n_betting_days": len(days), "n_bet_days": len(bet_days),
        "worst_betting_day": day_info(worst), "best_betting_day": day_info(best),
    }


# ------------------------------------------------------------------ #
# Day-block bootstrap                                                  #
# ------------------------------------------------------------------ #

def day_block_bootstrap(days: list[DayLedger], decisions: list[GameDecision], *, n_resamples: int = BOOTSTRAP_RESAMPLES,
                        seed: int = BOOTSTRAP_SEED, min_days: int = MIN_BOOTSTRAP_DAYS) -> dict[str, Any]:
    """以 betting day 為單位重抽（同日 bets 一起抽）。輸出 yield 95% CI 與 bankroll return 分佈。"""
    by_day: dict[date, list[Wager]] = {}
    for d in decisions:
        for w in d.wagers:
            if w.settlement_status in settlement.GRADED:
                by_day.setdefault(w.betting_day, []).append(w)
    start_of = {d.betting_day: d.day_start_bankroll for d in days}
    keys = sorted(by_day)
    out: dict[str, Any] = {"unit": "betting_day", "n_days": len(keys), "min_days": min_days,
                           "n_resamples": n_resamples, "seed": seed}
    if len(keys) < min_days:
        out["status"] = "insufficient_sample"
        return out
    profit = np.array([math.fsum(w.profit_units for w in by_day[k]) for k in keys])
    stake = np.array([math.fsum(w.stake_units for w in by_day[k]) for k in keys])
    ret = profit / np.array([start_of[k] for k in keys])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(n_resamples, len(keys)))
    yields = profit[idx].sum(axis=1) / stake[idx].sum(axis=1)
    br = np.exp(np.log1p(ret[idx]).sum(axis=1)) - 1.0
    q = lambda a, p: float(np.quantile(a, p))  # noqa: E731
    out.update(status="ok",
               yield_point=float(profit.sum() / stake.sum()),
               yield_ci95=[q(yields, 0.025), q(yields, 0.975)],
               bankroll_return_quantiles={str(p): q(br, p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)},
               prob_bankroll_return_negative=float((br < 0).mean()),
               note="day returns resampled as exchangeable blocks; ignores serial dependence across days")
    return out


# ------------------------------------------------------------------ #
# Descriptive subgroups（不是策略最佳化）                                   #
# ------------------------------------------------------------------ #

def _bin(x: float, bins) -> str:
    for lo, hi in bins:
        if lo < x <= hi or (lo == x == bins[0][0]):
            return f"({lo},{hi}]"
    return "out_of_range"


def _odds_bin(x: float) -> str:
    for lo, hi in ODDS_BINS:
        if lo <= x < hi:
            return f"[{lo},{hi})"
    return "out_of_range"


def subgroup_report(wagers: Iterable[Wager], *, season_opener: dict[int, date] | None = None) -> dict[str, Any]:
    """season_opener：game_id → 該季第一個 betting day（early_season = 開季 30 天內）；沒有就以本次資料第一天代替。"""
    graded = [w for w in wagers if w.settlement_status in settlement.GRADED]
    first = min((w.betting_day for w in graded), default=None)

    def phase(w: Wager) -> str:
        opener = (season_opener or {}).get(w.game_id, first)
        return "early_season" if w.betting_day < opener + timedelta(days=EARLY_SEASON_DAYS) else "later_season"

    keys = {
        "market": lambda w: f"{w.period}:{w.market_type}" + (":three_way" if w.outcome_set == "three_way" else ""),
        "source": lambda w: w.source, "bookmaker": lambda w: w.bookmaker,
        "month": lambda w: w.betting_day.strftime("%Y-%m"), "side": lambda w: w.side,
        "ev_bin": lambda w: _bin(w.ev_per_unit, EV_BINS), "odds_range": lambda w: _odds_bin(w.decimal_odds),
        "season_phase": phase,
    }
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for name, f in keys.items():
        g: dict[str, list[Wager]] = {}
        for w in graded:
            g.setdefault(str(f(w)), []).append(w)
        groups[name] = {}
        for k in sorted(g):
            xs = g[k]
            s = math.fsum(w.stake_units for w in xs)
            p = math.fsum(w.profit_units for w in xs)
            groups[name][k] = {"n_bets": len(xs), "staked_units": s, "profit_units": p, "yield": _div(p, s),
                               "expected_profit_units": math.fsum(w.expected_profit_units for w in xs)}
    return {"label": DESCRIPTIVE_LABEL,
            "note": "descriptive breakdown only; must not be used to add filters / EV thresholds to execution-v1",
            "groups": groups}
