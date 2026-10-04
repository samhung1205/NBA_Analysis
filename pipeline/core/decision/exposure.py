"""
Actual-bet exposure controller（decision-v1；純函式，不連 DB）
------------------------------------------------------------
    actual_exposure(bets, day, day_start_bankroll)        → 使用者「真的已下注」在某個 betting day 占用的 risk-v1 額度
    rescale_with_actual(results, exposure, recorded, ...) → 台彩 D.3 理論機會在扣除實際下注後的新增額度

actual exposure（只計 active、未 void / superseded 的 bets）：
    actual_day_exposure      = Σ stake（該 betting day）÷ day_start_bankroll
    actual_game_exposure[g]  = Σ stake（同一場）÷ day_start_bankroll
  已結算的當日注單仍占用當日額度（execution-v1：當日中途結算不釋放額度）。
  分母永遠是當日凍結的 day_start_bankroll（中途輸贏不改變百分比基準）。

不完整資料（不猜、不回填）：
    stake 不合法                → 無法計入 → 整個 betting day exposure_incomplete（不給新的 actionable 額度）
    betting day 無法確定        → 可能屬於任何一天 → 所有 day exposure_incomplete
    game 無法確定（有 day）      → 計入 daily；同場額度保守處理：這筆 stake 視為占用「每一場」的同場額度
    legacy（D.5 以前的紀錄）      → 照常計入（有 game / stake），附 legacy_unlinked_bet 警示

rescale（risk-v1 同一套上限；不排序、不挑最佳）：
    Step 1  每筆 ≤ 2%                         = D.3 single_bet_capped_fraction
    Step 2  remaining_game = max(0, 3% − actual_game)；同場 Σ > remaining_game → 等比例縮放
    Step 3  remaining_day  = max(0, 8% − actual_day)；全部 Σ > remaining_day → 等比例縮放
    → user_adjusted_fraction。沒有任何實際下注時 = D.3 final（逐位元相同；測試）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from ..sizing import engine as sizing_engine
from ..sizing.policy import RiskPolicy
from ..timeutil import ensure_utc, parse_utc
from . import policy as P

PARTICIPANT_STATUSES = (sizing_engine.ELIGIBLE, sizing_engine.EXPOSURE_SCALED)


@dataclass(frozen=True)
class ActualBet:
    bet_id: int
    game_id: int | None
    betting_day: date | None
    market: str | None
    side: str | None
    stake: float | None
    odds: float | None
    record_status: str = P.ACTIVE
    result: str | None = "pending"
    recorded_time: datetime | None = None          # recorded_at（D.5）或 placed_at（legacy）
    voided_at: datetime | None = None
    origin: str | None = None                        # None = legacy（D.5 以前）
    source: str | None = None
    bookmaker: str | None = None
    strategy_compliance: str | None = None
    decision_opportunity_id: int | None = None
    settled_at: datetime | None = None

    @property
    def legacy(self) -> bool:
        return self.origin is None

    @property
    def active(self) -> bool:
        return self.record_status == P.ACTIVE

    def stake_valid(self) -> bool:
        return self.stake is not None and math.isfinite(self.stake) and self.stake > 0


def _date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def bet_from_row(row: dict, game_start_utc: datetime | None) -> ActualBet:
    """bets 列 → ActualBet。betting_day：記錄時寫入的值（凍結）；舊列 NULL → 由比賽開賽時間導出（Asia/Taipei）。"""
    day = _date(row.get("betting_day"))
    if day is None and game_start_utc is not None:
        day = sizing_engine.betting_day(ensure_utc(parse_utc(game_start_utc)))
    rec = parse_utc(row.get("recorded_at")) or parse_utc(row.get("placed_at"))
    return ActualBet(
        bet_id=int(row["id"]), game_id=int(row["game_id"]) if row.get("game_id") is not None else None,
        betting_day=day, market=row.get("market"), side=row.get("selection"), stake=_float(row.get("stake")),
        odds=_float(row.get("odds")), record_status=row.get("record_status") or P.ACTIVE,
        result=row.get("result") or "pending", recorded_time=ensure_utc(rec) if rec else None,
        voided_at=parse_utc(row.get("voided_at")), origin=row.get("origin"), source=row.get("source"),
        bookmaker=row.get("bookmaker"), strategy_compliance=row.get("strategy_compliance"),
        decision_opportunity_id=row.get("decision_opportunity_id"), settled_at=parse_utc(row.get("settled_at")))


# ------------------------------------------------------------------ #
# actual exposure                                                     #
# ------------------------------------------------------------------ #

@dataclass
class ActualExposure:
    betting_day: date
    day_start_bankroll: float | None
    day_stake: float = 0.0
    game_stake: dict[int, float] = field(default_factory=dict)
    unattributed_game_stake: float = 0.0           # 有 day、沒有 game 的 stake（保守地占用每一場）
    day_fraction: float | None = None
    game_fraction: dict[int, float] = field(default_factory=dict)
    unattributed_game_fraction: float | None = None
    bets: list[dict[str, Any]] = field(default_factory=list)
    incomplete: bool = False
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fraction_for_game(self, game_id: int) -> float | None:
        if self.day_fraction is None:
            return None
        return self.game_fraction.get(game_id, 0.0) + (self.unattributed_game_fraction or 0.0)

    def stake_for_game(self, game_id: int) -> float:
        return self.game_stake.get(game_id, 0.0) + self.unattributed_game_stake

    def to_dict(self) -> dict[str, Any]:
        return {"betting_day": self.betting_day.isoformat(), "day_start_bankroll": self.day_start_bankroll,
                "day_stake": self.day_stake, "day_fraction": self.day_fraction,
                "game_stake": {str(k): v for k, v in sorted(self.game_stake.items())},
                "game_fraction": {str(k): v for k, v in sorted(self.game_fraction.items())},
                "unattributed_game_stake": self.unattributed_game_stake,
                "unattributed_game_fraction": self.unattributed_game_fraction,
                "n_bets": len(self.bets), "bets": self.bets, "incomplete": self.incomplete,
                "issues": list(self.issues), "warnings": list(self.warnings)}


def actual_exposure(bets: Iterable[ActualBet], day: date, day_start_bankroll: float | None) -> ActualExposure:
    """某個 betting day 的實際下注 exposure。day_start_bankroll 為 None（bankroll 未設定 / 尚未凍結）→ 只有金額、沒有比例。"""
    base = day_start_bankroll if (day_start_bankroll is not None and day_start_bankroll > 0) else None
    ex = ActualExposure(betting_day=day, day_start_bankroll=base)
    day_stakes, unattributed, game_stakes = [], [], {}
    for b in sorted(bets, key=lambda b: b.bet_id):
        if not b.active:
            continue
        if b.betting_day is None:
            ex.incomplete = True                                   # 可能屬於任何一天：所有 day 都保守處理
            ex.issues.append(f"bet:{b.bet_id}:betting_day_unknown")
            continue
        if b.betting_day != day:
            continue
        item = {"bet_id": b.bet_id, "game_id": b.game_id, "market": b.market, "side": b.side, "stake": b.stake,
                "odds": b.odds, "result": b.result, "origin": b.origin, "strategy_compliance": b.strategy_compliance,
                "legacy": b.legacy, "fraction": None, "counted": False}
        if not b.stake_valid():
            ex.incomplete = True
            ex.issues.append(f"bet:{b.bet_id}:invalid_stake")
            ex.bets.append(item)
            continue
        item["counted"] = True
        day_stakes.append(b.stake)
        if b.game_id is None:
            unattributed.append(b.stake)
            ex.warnings.append(f"bet:{b.bet_id}:game_unknown_counted_against_every_game")
        else:
            game_stakes.setdefault(b.game_id, []).append(b.stake)
        if b.legacy:
            ex.warnings.append(f"bet:{b.bet_id}:legacy_unlinked_bet")
        if base is not None:
            item["fraction"] = b.stake / base
        ex.bets.append(item)
    ex.day_stake = math.fsum(day_stakes)
    ex.unattributed_game_stake = math.fsum(unattributed)
    ex.game_stake = {g: math.fsum(v) for g, v in game_stakes.items()}
    if base is not None:
        ex.day_fraction = ex.day_stake / base
        ex.unattributed_game_fraction = ex.unattributed_game_stake / base
        ex.game_fraction = {g: s / base for g, s in ex.game_stake.items()}
    elif not day_stakes:
        ex.day_fraction, ex.unattributed_game_fraction = 0.0, 0.0     # 沒有任何實際下注：比例為 0（與分母無關）
    if ex.incomplete:
        ex.warnings.append("actual_exposure_may_be_incomplete")
    return ex


# ------------------------------------------------------------------ #
# 已記錄的機會（同一 game × market）                                     #
# ------------------------------------------------------------------ #

def recorded_markets(bets: Iterable[ActualBet], day: date) -> dict[tuple[int, str], list[ActualBet]]:
    """(game_id, market) → 有效實際下注。任何來源（含手動輸入）都算：同一場同一玩法已有實際注單 → 不再提出新增額度。"""
    out: dict[tuple[int, str], list[ActualBet]] = {}
    for b in bets:
        if b.active and b.betting_day == day and b.game_id is not None and b.market:
            out.setdefault((b.game_id, b.market), []).append(b)
    return out


# ------------------------------------------------------------------ #
# rescale                                                              #
# ------------------------------------------------------------------ #

@dataclass
class Rescaled:
    user_adjusted_fraction: float
    actual_game_exposure: float
    remaining_game_fraction: float
    remaining_day_fraction: float
    game_scale_factor: float
    day_scale_factor: float
    game_over_limit: bool
    day_over_limit: bool


def _remaining(cap: float, used: float) -> float:
    r = cap - used
    return r if r > P.EXPOSURE_EPS else 0.0


def _scale(total: float, cap: float) -> float:
    if total <= cap:
        return 1.0
    return cap / total if total > 0 else 0.0


def rescale_with_actual(participants: list[sizing_engine.SizingResult], exposure: ActualExposure, *,
                        policy: RiskPolicy = P.RISK_POLICY) -> dict[int, Rescaled]:
    """participants：台彩 D.3 eligible / exposure_scaled（不含已記錄的市場），以 index 回傳結果。
    exposure 必須有比例（day_start 已知）。"""
    if exposure.day_fraction is None:
        raise ValueError("actual exposure 沒有 day_start_bankroll，無法計算剩餘額度")
    day_used = exposure.day_fraction
    remaining_day = _remaining(policy.max_day_fraction, day_used)
    day_over = day_used > policy.max_day_fraction + P.OVER_LIMIT_TOL
    by_game: dict[int, list[int]] = {}
    for i, r in enumerate(participants):
        if r.qualification_status not in PARTICIPANT_STATUSES or not (r.single_bet_capped_fraction or 0) > 0:
            raise ValueError(f"非 D.3 participant 不可進入 rescale：{r.qualification_status}")
        by_game.setdefault(r.game_id, []).append(i)
    adjusted: dict[int, float] = {}
    game_info: dict[int, tuple[float, float, float, bool]] = {}
    for gid, idx in by_game.items():
        used = exposure.fraction_for_game(gid)
        rem = _remaining(policy.max_game_fraction, used)
        total = math.fsum(participants[i].single_bet_capped_fraction for i in idx)
        s = _scale(total, rem)
        for i in idx:
            adjusted[i] = participants[i].single_bet_capped_fraction * s
        game_info[gid] = (used, rem, s, used > policy.max_game_fraction + P.OVER_LIMIT_TOL)
    day_total = math.fsum(adjusted.values())
    s_day = _scale(day_total, remaining_day)
    out = {}
    for i, r in enumerate(participants):
        used, rem, s_g, g_over = game_info[r.game_id]
        out[i] = Rescaled(user_adjusted_fraction=adjusted[i] * s_day, actual_game_exposure=used,
                          remaining_game_fraction=rem, remaining_day_fraction=remaining_day, game_scale_factor=s_g,
                          day_scale_factor=s_day, game_over_limit=g_over, day_over_limit=day_over)
    return out
