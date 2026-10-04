"""
Personal strategy bankroll（bankroll-v1；純函式，不連 DB）
------------------------------------------------------------
使用者手動維護的策略資金，不是銀行 / bookmaker 帳戶同步。所有金額異動都是 append-only ledger 列：

    initial_funding / deposit     +amount（amount > 0）
    withdrawal                    −amount（amount > 0）
    adjustment                    有號 amount（≠ 0，必須有原因）
    reversal                      −（被沖銷那一筆的有號金額）；amount 欄位為 NULL，由這裡計算
    bet_settlement                實際注單的已結算損益（pipeline；win = stake·(odds − 1)、lose = −stake、push / void = 0）
    bet_settlement_reversal       結算改變 / 注單作廢時沖銷先前的損益（pipeline）

    ledger_balance(t)   = Σ 有號金額（recorded_at ≤ t）
    open_stake(t)       = 有效、t 時尚未結算的實際注單 stake 合計
    current_bankroll    = ledger_balance(now)
    available_bankroll  = current_bankroll − open_stake(now)

day_start_bankroll（execution-v1 語意，凍結於 bankroll_day_snapshots）：
    basis_as_of = 該 betting day 第一筆實際注單的記錄時間；沒有注單 → 第一次出現 qualified 機會的物化時點
    day_start   = ledger_balance(basis_as_of) − 其他 betting day 在 basis_as_of 仍未結算的 stake
    ≤ 0 → 無法建立（bankroll_unavailable；不猜、不以之後才存入的資金回填）
當日所有 stake 比例都以 day_start 為分母；當日中途結算不改變基準；次日才使用更新後的 bankroll。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from ..execution import settlement as settle_v1
from ..timeutil import ensure_utc, parse_utc
from . import policy as P
from .exposure import ActualBet

USER_TYPES = ("initial_funding", "deposit", "withdrawal", "adjustment", "reversal")
SETTLEMENT_TYPES = ("bet_settlement", "bet_settlement_reversal")
ENTRY_TYPES = USER_TYPES + SETTLEMENT_TYPES

# bets.result（使用者 / settle-v1 寫入）→ settle-v1 帳務狀態
RESULT_TO_STATUS = {"win": settle_v1.SETTLED_WIN, "lose": settle_v1.SETTLED_LOSS, "push": settle_v1.SETTLED_PUSH,
                    "void": settle_v1.VOID}


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    entry_type: str
    amount: float | None
    recorded_at: datetime
    bet_id: int | None = None
    reverses_entry_id: int | None = None
    settlement_key: str | None = None
    reason: str | None = None


def entry_from_row(r: dict) -> LedgerEntry:
    amt = r.get("amount")
    return LedgerEntry(id=int(r["id"]), entry_type=r["entry_type"], amount=None if amt is None else float(amt),
                       recorded_at=ensure_utc(parse_utc(r["recorded_at"])), bet_id=r.get("bet_id"),
                       reverses_entry_id=r.get("reverses_entry_id"), settlement_key=r.get("settlement_key"),
                       reason=r.get("reason"))


def signed_amounts(entries: Iterable[LedgerEntry]) -> dict[int, float]:
    """每一筆的有號金額。reversal 只能沖銷更早（id 較小）的一筆，且每筆最多被沖銷一次（DB 也有唯一索引）。"""
    es = sorted(entries, key=lambda e: e.id)
    out: dict[int, float] = {}
    reversed_ids: set[int] = set()
    for e in es:
        if e.entry_type not in ENTRY_TYPES:
            raise LedgerError(f"未知 entry_type {e.entry_type}")
        if e.entry_type == "reversal":
            t = e.reverses_entry_id
            if t is None or t not in out:
                raise LedgerError(f"reversal {e.id} 參照不存在或較晚的 entry {t}")
            if t in reversed_ids:
                raise LedgerError(f"entry {t} 已被沖銷")
            reversed_ids.add(t)
            out[e.id] = -out[t]
            continue
        if e.amount is None or not math.isfinite(e.amount):
            raise LedgerError(f"entry {e.id} 金額不合法")
        if e.entry_type in ("initial_funding", "deposit", "withdrawal"):
            if e.amount <= 0:
                raise LedgerError(f"entry {e.id}（{e.entry_type}）金額必須 > 0")
            out[e.id] = -e.amount if e.entry_type == "withdrawal" else e.amount
        elif e.entry_type == "adjustment":
            if e.amount == 0:
                raise LedgerError(f"adjustment {e.id} 不得為 0")
            out[e.id] = e.amount
        else:
            out[e.id] = e.amount
    return out


def ledger_balance(entries: Iterable[LedgerEntry], as_of: datetime | None = None) -> float:
    es = list(entries)
    signed = signed_amounts(es)
    t = ensure_utc(as_of) if as_of is not None else None
    return math.fsum(signed[e.id] for e in es if t is None or e.recorded_at <= t)


def ledger_watermark(entries: Iterable[LedgerEntry], as_of: datetime | None = None) -> int:
    t = ensure_utc(as_of) if as_of is not None else None
    return max((e.id for e in entries if t is None or e.recorded_at <= t), default=0)


# ------------------------------------------------------------------ #
# 實際注單是否「未結算」（以 ledger 的結算紀錄為準，as-of 決定性）            #
# ------------------------------------------------------------------ #

def _settled_at(bet_id: int, entries: list[LedgerEntry], t: datetime | None) -> bool:
    rel = [e for e in entries if e.bet_id == bet_id and e.entry_type in SETTLEMENT_TYPES
           and (t is None or e.recorded_at <= t)]
    if not rel:
        return False
    return max(rel, key=lambda e: e.id).entry_type == "bet_settlement"


def bet_active_at(b: ActualBet, t: datetime | None) -> bool:
    if t is None:
        return b.active
    if b.recorded_time is None or b.recorded_time > t:
        return False
    if b.voided_at is not None and ensure_utc(b.voided_at) <= t:
        return False
    return b.active or (b.voided_at is not None and ensure_utc(b.voided_at) > t)


def open_bets(bets: Iterable[ActualBet], entries: Iterable[LedgerEntry], t: datetime | None = None
              ) -> list[ActualBet]:
    es = list(entries)
    return [b for b in bets if bet_active_at(b, t) and b.stake_valid() and not _settled_at(b.bet_id, es, t)]


def open_stake(bets: Iterable[ActualBet], entries: Iterable[LedgerEntry], t: datetime | None = None, *,
               exclude_day: date | None = None) -> float:
    return math.fsum(b.stake for b in open_bets(bets, entries, t) if exclude_day is None or b.betting_day != exclude_day)


# ------------------------------------------------------------------ #
# day_start_bankroll                                                   #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class DayStart:
    betting_day: date
    day_start_bankroll: float
    basis_as_of: datetime
    ledger_balance: float
    open_stake_excluded: float
    ledger_watermark: int
    established_reason: str
    bankroll_version: str = P.BANKROLL_VERSION

    def to_row(self) -> dict[str, Any]:
        return {"betting_day": self.betting_day, "day_start_bankroll": self.day_start_bankroll,
                "basis_as_of": self.basis_as_of, "ledger_balance": self.ledger_balance,
                "open_stake_excluded": self.open_stake_excluded, "ledger_watermark": self.ledger_watermark,
                "established_reason": self.established_reason, "bankroll_version": self.bankroll_version}


def basis_for_day(day: date, bets: Iterable[ActualBet], now: datetime) -> tuple[datetime, str]:
    """day_start 的基準時點：該日第一筆實際注單的記錄時間（含之後作廢的；記錄當下它是有效的）；否則 = now。"""
    now = ensure_utc(now)
    times = [b.recorded_time for b in bets if b.betting_day == day and b.recorded_time is not None]
    first = min(times) if times else None
    if first is not None and first <= now:
        return first, "first_recorded_bet"
    return now, "first_qualified_opportunity"


def compute_day_start(day: date, entries: Iterable[LedgerEntry], bets: Iterable[ActualBet], basis_as_of: datetime,
                      reason: str) -> DayStart | None:
    es, bs = list(entries), list(bets)
    t = ensure_utc(basis_as_of)
    bal = ledger_balance(es, t)
    excluded = open_stake(bs, es, t, exclude_day=day)
    start = bal - excluded
    if not (start > 0 and math.isfinite(start)):
        return None
    return DayStart(day, start, t, bal, excluded, ledger_watermark(es, t), reason)


# ------------------------------------------------------------------ #
# 實際注單結算 → ledger（pipeline）                                       #
# ------------------------------------------------------------------ #

def desired_profit(b: ActualBet) -> float | None:
    """該注單目前應入帳的損益；None = 不應有結算紀錄（未結算 / 作廢 / 被更正取代）。"""
    if not b.active or not b.stake_valid() or b.odds is None:
        return None
    st = RESULT_TO_STATUS.get(b.result or "")
    if st is None:
        return None
    return settle_v1.profit(st, b.stake, b.odds)


def settlement_ledger_actions(bets: Iterable[ActualBet], entries: Iterable[LedgerEntry]) -> list[dict[str, Any]]:
    """讓 ledger 的實際注單損益與 bets 目前狀態一致的新 entry（append-only：先沖銷、再入帳；冪等）。"""
    es = list(entries)
    out = []
    for b in sorted(bets, key=lambda b: b.bet_id):
        rel = sorted((e for e in es if e.bet_id == b.bet_id and e.entry_type in SETTLEMENT_TYPES), key=lambda e: e.id)
        net = math.fsum(e.amount for e in rel)
        settled = bool(rel) and rel[-1].entry_type == "bet_settlement"
        want = desired_profit(b)
        if want is None and not settled:
            continue
        if want is not None and settled and math.isclose(net, want, rel_tol=0.0, abs_tol=1e-9):
            continue
        seq = len(rel)
        if settled:
            out.append({"entry_type": "bet_settlement_reversal", "amount": -net, "bet_id": b.bet_id,
                        "settlement_key": f"bet:{b.bet_id}:{seq}:reversal", "reason": "settlement_changed_or_voided"})
            seq += 1
        if want is not None:
            out.append({"entry_type": "bet_settlement", "amount": want, "bet_id": b.bet_id,
                        "settlement_key": f"bet:{b.bet_id}:{seq}:{b.result}", "reason": f"result:{b.result}"})
    return out


# ------------------------------------------------------------------ #
# 摘要（UI：不只一個 balance）                                            #
# ------------------------------------------------------------------ #

def bankroll_summary(entries: Iterable[LedgerEntry], bets: Iterable[ActualBet], now: datetime, *,
                     day_start: DayStart | None, day_used_fraction: float | None, currency: str | None
                     ) -> dict[str, Any]:
    es, bs = list(entries), list(bets)
    current = ledger_balance(es)
    open_ = open_bets(bs, es, None)
    committed = math.fsum(b.stake for b in open_)
    cap = P.RISK_POLICY.max_day_fraction
    remaining = None if day_used_fraction is None else max(0.0, cap - day_used_fraction)
    return {"configured": True, "currency": currency, "current_bankroll": current, "committed_open_stake": committed,
            "available_bankroll": current - committed, "n_open_bets": len(open_),
            "open_bet_ids": [b.bet_id for b in open_],          # 未結算 / 無法結算（ungradable）= pending capital
            "day_start_bankroll": day_start.day_start_bankroll if day_start else None,
            "day_start_basis_as_of": day_start.basis_as_of.isoformat() if day_start else None,
            "day_start_reason": day_start.established_reason if day_start else None,
            "daily_risk_used_fraction": day_used_fraction, "daily_risk_cap_fraction": cap,
            "remaining_risk_budget_fraction": remaining,
            "remaining_risk_budget_amount": (remaining * day_start.day_start_bankroll
                                             if (remaining is not None and day_start) else None),
            "ledger_entries": len(es), "ledger_watermark": ledger_watermark(es)}


def actual_performance(bets: Iterable[ActualBet]) -> dict[str, Any]:
    """User actual betting record（不是策略績效；組成可能含手動 / override / legacy）。turnover 只計已結算 win / lose / push。"""
    bs = [b for b in bets if b.active and b.stake_valid()]
    graded = [b for b in bs if b.result in ("win", "lose", "push")]
    profits = [desired_profit(b) for b in graded]
    staked = math.fsum(b.stake for b in graded)
    pnl = math.fsum(p for p in profits if p is not None)
    comp_origin: dict[str, int] = {}
    comp_compliance: dict[str, int] = {}
    for b in bs:
        o = b.origin or "legacy_unlinked"
        comp_origin[o] = comp_origin.get(o, 0) + 1
        c = b.strategy_compliance or "legacy_unknown"
        comp_compliance[c] = comp_compliance.get(c, 0) + 1
    return {"label": "user_actual_betting_record", "n_bets": len(bs),
            "wins": sum(b.result == "win" for b in bs), "losses": sum(b.result == "lose" for b in bs),
            "pushes": sum(b.result == "push" for b in bs), "voids": sum(b.result == "void" for b in bs),
            "pending": sum(b.result in (None, "pending") for b in bs),
            "total_staked_graded": staked, "realized_pnl": pnl,
            "open_stake": math.fsum(b.stake for b in bs if b.result in (None, "pending")),
            "actual_yield": (pnl / staked) if staked > 0 else None,
            "composition_by_origin": dict(sorted(comp_origin.items())),
            "composition_by_compliance": dict(sorted(comp_compliance.items())),
            "includes_non_strategy_bets": any(k != "compliant" for k in comp_compliance),
            "statement": "User actual betting record — NOT model strategy performance (may include manual, "
                         "override and legacy bets)."}
