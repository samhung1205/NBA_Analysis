"""
Settlement engine（settle-v1；唯一實作）
------------------------------------------------------------
依「下注當時」的 snapshot / pricing outcome（model_target / model_threshold / comparator / settlement_rule）結算，
不重新查目前的盤口線。只有 games.status = 'final' 才結算；任何不完整 / 不一致 → ungradable（不猜）。

    target     margin = home − away；total = home + away；h1_margin / h1_total 用上半場比分（home_h1 / away_h1）
    gt         win ⇔ value > threshold；push ⇔ value = threshold；loss ⇔ value < threshold
    lt         win ⇔ value < threshold；push ⇔ value = threshold；loss ⇔ value > threshold
    eq         （三向的和局）win ⇔ value = threshold → settled_draw_win；否則 loss

    moneyline_ot_included      全場分差含延長賽；NBA 不可能平手 → 分差 0 = 比分不一致（ungradable）
    half_line_no_push          半分線；value = threshold 不可能（比分是整數）→ ungradable
    integer_line_push_refund   value = threshold → push（退還本金）
    three_way_draw_outcome     上半場主 / 和 / 客；h1 平手時主 / 客都是 loss（不是 push），和局是中獎 outcome
    其他（兩向上半場獨贏、四分之一線、全場三向）→ 永遠不結算成有效注單（ungradable: unsupported_settlement）

payout：win / draw_win → stake·(odds − 1)；loss → −stake；push / void → 0；pending / ungradable → None（不入帳）。
void：只有登記在 VOID_RULES 的明確通用規則才使用；execution-v1 沒有登記任何規則
（取消 / 延期 / 改期的退款規則依 bookmaker 而定，未驗證 → ungradable + exclusion reason）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..sizing.engine import SUPPORTED_SETTLEMENT_RULES
from ..timeutil import ensure_utc, parse_utc
from .policy import EXECUTION_V1, SETTLEMENT_VERSION, ExecutionPolicy

PENDING = "pending"
SETTLED_WIN = "settled_win"
SETTLED_LOSS = "settled_loss"
SETTLED_PUSH = "settled_push"
SETTLED_DRAW_WIN = "settled_draw_win"
VOID = "void"
UNGRADABLE = "ungradable"
SETTLEMENT_STATUSES = (PENDING, SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH, SETTLED_DRAW_WIN, VOID, UNGRADABLE)
TERMINAL = (SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH, SETTLED_DRAW_WIN, VOID)     # 結算後不可再改
GRADED = (SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH, SETTLED_DRAW_WIN)              # 計入 turnover
WINS = (SETTLED_WIN, SETTLED_DRAW_WIN)

VOID_RULES: dict[str, str] = {}          # execution-v1：沒有已驗證的 bookmaker void 規則（見模組說明）

_TARGETS = {"margin": ("pts", "diff"), "total": ("pts", "sum"), "h1_margin": ("h1", "diff"), "h1_total": ("h1", "sum")}


@dataclass(frozen=True)
class Settlement:
    status: str
    reason: str | None
    actual_value: float | None
    home_score: int | None
    away_score: int | None
    profit_units: float | None
    settlement_version: str = SETTLEMENT_VERSION

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


def profit(status: str, stake: float, decimal_odds: float) -> float | None:
    """單筆損益（bankroll 單位）。stake 本金已在 bankroll 內，不另外加回（避免 double count）。"""
    if status in WINS:
        return float(stake) * (float(decimal_odds) - 1.0)
    if status == SETTLED_LOSS:
        return -float(stake)
    if status in (SETTLED_PUSH, VOID):
        return 0.0
    if status in (PENDING, UNGRADABLE):
        return None
    raise ValueError(f"未知 settlement status {status}")


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if math.isfinite(f) and f == int(f) else None


def _consistency(game: dict) -> str | None:
    """比分一致性：全場 = 四節 + 延長；上半場 = Q1 + Q2（欄位齊全時才檢查）。"""
    for side in ("home", "away"):
        pts, qs = _int(game.get(f"{side}_pts")), [_int(game.get(f"{side}_q{i}")) for i in range(1, 5)]
        if pts is not None and None not in qs:
            ot = _int(game.get(f"{side}_ot")) or 0
            if pts != sum(qs) + ot:
                return f"score_inconsistent:{side}_pts_vs_quarters"
        h1 = _int(game.get(f"{side}_h1"))
        if h1 is not None and qs[0] is not None and qs[1] is not None and h1 != qs[0] + qs[1]:
            return f"score_inconsistent:{side}_h1_vs_quarters"
        if h1 is not None and pts is not None and h1 > pts:
            return f"score_inconsistent:{side}_h1_gt_full"
    return None


def settle(bet: dict, game: dict, *, decision_scheduled_tipoff: datetime | None = None,
           policy: ExecutionPolicy = EXECUTION_V1) -> Settlement:
    """bet：下注當時的 outcome 規格（market_type / period / outcome_set / side / model_target / model_threshold /
    comparator / settlement_rule / decimal_odds / stake_units）；game：games 列（目前比分）。"""
    def out(status, reason=None, value=None, h=None, a=None):
        p = profit(status, bet.get("stake_units") or 0.0, bet.get("decimal_odds") or 1.0) \
            if status not in (PENDING, UNGRADABLE) else None
        return Settlement(status, reason, value, h, a, p)

    rule = bet.get("settlement_rule")
    if rule not in SUPPORTED_SETTLEMENT_RULES:
        return out(UNGRADABLE, f"unsupported_settlement:{rule}")
    if bet.get("market_type") == "moneyline" and bet.get("period") == "h1" and bet.get("outcome_set") != "three_way":
        return out(UNGRADABLE, "unsupported_settlement:h1_two_way_tie_settlement_unknown")
    thr = bet.get("model_threshold")
    if thr is None or not math.isfinite(float(thr)):
        return out(UNGRADABLE, "missing_threshold")
    thr = float(thr)
    if math.isclose(abs(thr) % 1.0, 0.25) or math.isclose(abs(thr) % 1.0, 0.75):
        return out(UNGRADABLE, "unsupported_settlement:quarter_line")
    target = bet.get("model_target")
    if target not in _TARGETS:
        return out(UNGRADABLE, f"unknown_target:{target}")

    status = (game.get("status") or "").lower()
    if status in VOID_RULES:
        return out(VOID, VOID_RULES[status])
    if status in ("cancelled", "canceled", "postponed", "suspended"):
        return out(UNGRADABLE, f"game_{status}:void_rule_unverified")
    if status != "final":
        return out(PENDING, f"game_status:{status or 'unknown'}")
    if decision_scheduled_tipoff is not None and game.get("date_utc") is not None:
        moved = abs(ensure_utc(parse_utc(game["date_utc"])) - ensure_utc(parse_utc(decision_scheduled_tipoff)))
        if moved > policy.reschedule_tolerance:
            return out(UNGRADABLE, "rescheduled_after_decision:void_rule_unverified")
    bad = _consistency(game)
    if bad:
        return out(UNGRADABLE, bad)

    kind, op = _TARGETS[target]
    h, a = _int(game.get(f"home_{kind}")), _int(game.get(f"away_{kind}"))
    if h is None or a is None:
        return out(UNGRADABLE, "missing_h1_score" if kind == "h1" else "missing_final_score")
    value = float(h - a if op == "diff" else h + a)
    if target == "margin" and value == 0:
        return out(UNGRADABLE, "score_inconsistent:full_game_tie", value, h, a)

    cmp = bet.get("comparator")
    if cmp == "eq":
        if rule != "three_way_draw_outcome":
            return out(UNGRADABLE, "comparator_rule_mismatch", value, h, a)
        return out(SETTLED_DRAW_WIN if value == thr else SETTLED_LOSS, None, value, h, a)
    if cmp not in ("gt", "lt"):
        return out(UNGRADABLE, f"unknown_comparator:{cmp}", value, h, a)
    if value == thr:
        if rule == "integer_line_push_refund":
            return out(SETTLED_PUSH, None, value, h, a)
        if rule == "three_way_draw_outcome":
            return out(SETTLED_LOSS, "h1_draw_loses_home_away", value, h, a)
        return out(UNGRADABLE, f"score_on_no_push_line:{rule}", value, h, a)
    won = value > thr if cmp == "gt" else value < thr
    return out(SETTLED_WIN if won else SETTLED_LOSS, None, value, h, a)
