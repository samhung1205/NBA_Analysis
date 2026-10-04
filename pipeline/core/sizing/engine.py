"""
Phase D.3 — bet qualification / risk controls / Kelly sizing（唯一實作）
------------------------------------------------------------
    size_portfolio(candidates, as_of=T, policy=RISK_V1) -> list[SizingResult]

輸入：D.2 定價結果（market_pricing_snapshots 列或記憶體中的 MarketPricing.to_rows()），每個 outcome 一個 SizingCandidate。
Kelly 只使用 P(win) / P(push) / P(loss) / 實際賠率（kelly.py）；edge_vs_fair、fair_no_vig_prob 只保存作市場分歧診斷。

步驟（全部決定性；同一組輸入 → 同樣輸出）：
  1. qualify（逐 outcome）
       hard reject：future data、已開賽、市場非 open、不支援的結算、沒有預測、定價列不合法、artifact 不符、機率不合法
       → qualification_status ≠ eligible、不計算 stake
       EV ≤ 0 → no_positive_ev（Kelly = 0）
       EV > 0 → full Kelly（push-aware）→ × kelly_multiplier → min(·, max_bet_fraction)   = single_bet_capped_fraction
       報價過舊 / 輪詢間隔未知 → stale_quote：數學上 eligible，但不 actionable、不進入 exposure
       資料品質旗標（season_opener / low_sample / injury_unknown / …）→ 只加 warning，**不**改 stake
  2. 互斥檢查：同一 snapshot（同一 bookmaker、同一市場、同一時刻）的互斥 outcome 中有 > 1 個正 Kelly
       → 整個市場 mutually_exclusive_positive_kelly（不自行挑一邊）。
       兩向市場在 overround ≥ 0 且機率一致時數學上不可能兩邊都正 EV → 出現代表機率 / 結算有問題；
       三向市場可能兩個 outcome 都正 EV，但多 outcome 聯合 Kelly 不在本階段範圍 → 一樣拒絕。
  3. 同一場 exposure：所有 eligible stake（含不同 bookmaker、不同市場——不假設獨立）合計 > max_game_fraction
       → 全部按比例縮放（不挑最高 EV、不排序砍單）。
  4. 同一 betting day（Asia/Taipei 開賽日期）exposure：game-adjusted stake 合計 > max_day_fraction → 再按比例縮放。
  → final_stake_fraction（bankroll 比例；canonical output）。

mathematically_eligible = 正 Kelly 且通過所有 hard check 與互斥檢查；actionable = 另外報價新鮮、final_stake_fraction > 0。
兩者都**不是**投注推薦（推薦屬 D.5）。

已知限制：exposure 只涵蓋 T 時點仍可下注（未開賽、open、新鮮）的理論機會，不含使用者實際已下的注（bets 表；D.5）。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from ..pricing import engine as pricing_engine
from ..timeutil import ensure_utc, parse_utc, tpe_date
from . import kelly
from .policy import RISK_V1, SIZING_VERSION, RiskPolicy

# ---- qualification_status ---- #
ELIGIBLE = "eligible"
NO_POSITIVE_EV = "no_positive_ev"
UNSUPPORTED_SETTLEMENT = "unsupported_settlement"
MARKET_NOT_OPEN = "market_not_open"
NO_PREDICTION = "no_prediction"
INVALID_PROBABILITY = "invalid_probability"
STALE_QUOTE = "stale_quote"
MUTUALLY_EXCLUSIVE = "mutually_exclusive_positive_kelly"
EXPOSURE_SCALED = "exposure_scaled"
UNAVAILABLE = "unavailable"
QUALIFICATION_STATUSES = (ELIGIBLE, NO_POSITIVE_EV, UNSUPPORTED_SETTLEMENT, MARKET_NOT_OPEN, NO_PREDICTION,
                          INVALID_PROBABILITY, STALE_QUOTE, MUTUALLY_EXCLUSIVE, EXPOSURE_SCALED, UNAVAILABLE)

# D.2 中可以完整計算 EV 的結算規則；其他（兩向上半場獨贏、四分之一線、全場三向）一律不算 stake
SUPPORTED_SETTLEMENT_RULES = ("moneyline_ot_included", "half_line_no_push", "integer_line_push_refund",
                              "three_way_draw_outcome")
NO_PUSH_RULES = ("moneyline_ot_included", "half_line_no_push", "three_way_draw_outcome")
EV_CONSISTENCY_TOL = 1e-9
SIDE_ORDER = {"home": 0, "draw": 1, "away": 2, "over": 3, "under": 4}


def betting_day(game_start_utc: datetime) -> date:
    """betting day = 開賽時間的 Asia/Taipei 日曆日（與 UI「今日 / 明日」、排程同一套日期；決定性）。"""
    return tpe_date(ensure_utc(game_start_utc))


# ------------------------------------------------------------------ #
# 輸入                                                                  #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class SizingCandidate:
    game_id: int
    game_start_utc: datetime
    side: str
    pricing_status: str
    pricing_analysis_as_of: datetime
    odds_fetched_at: datetime
    market_pricing_snapshot_id: int | None = None
    odds_snapshot_id: int | None = None
    prediction_id: int | None = None
    pricing_version: str | None = None
    source: str | None = None
    bookmaker: str | None = None
    market: str | None = None
    market_type: str | None = None
    period: str | None = None
    outcome_set: str | None = None
    line: float | None = None
    display_line: float | None = None
    pricing_status_reason: str | None = None
    settlement_rule: str | None = None
    decimal_odds: float | None = None
    p_win: float | None = None
    p_push: float | None = None
    p_loss: float | None = None
    ev_per_unit: float | None = None
    edge_vs_fair: float | None = None          # 診斷用；Kelly 不讀
    fair_no_vig_prob: float | None = None      # 診斷用；Kelly 不讀
    odds_last_seen_at: datetime | None = None
    model_version: str | None = None
    artifact_version: str | None = None
    pricing_warnings: tuple[str, ...] = ()
    data_quality_flags: tuple[str, ...] = ()
    preset_unavailable: str | None = None      # 選擇層已判定不可用（例如 artifact 不符、定價落後於最新預測）

    def market_key(self) -> tuple:
        """互斥檢查的市場身分：同一 snapshot × 同一預測 × 同一定價版本。"""
        if self.odds_snapshot_id is not None:
            return ("snap", self.odds_snapshot_id, self.prediction_id, self.pricing_version)
        return ("mkt", self.game_id, self.source, self.bookmaker, self.market, self.line, self.prediction_id)

    def member_id(self) -> tuple:
        if self.market_pricing_snapshot_id is not None:
            return ("pricing", self.market_pricing_snapshot_id)
        return self.market_key() + (self.side,)


def _json(v: Any, fallback):
    if v is None:
        return fallback
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return fallback


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def candidate_from_pricing_row(row: dict, *, game_start_utc: datetime, odds_last_seen_at: datetime | None = None,
                               preset_unavailable: str | None = None) -> SizingCandidate:
    """market_pricing_snapshots 一列（或 MarketPricing.to_rows() 的一列）→ SizingCandidate。"""
    diag = _json(row.get("diagnostics"), {})
    return SizingCandidate(
        game_id=int(row["game_id"]), game_start_utc=ensure_utc(parse_utc(game_start_utc)), side=row["side"],
        pricing_status=row["status"], pricing_analysis_as_of=ensure_utc(parse_utc(row["analysis_as_of"])),
        odds_fetched_at=ensure_utc(parse_utc(row["odds_fetched_at"])),
        market_pricing_snapshot_id=row.get("id"), odds_snapshot_id=row.get("odds_snapshot_id"),
        prediction_id=row.get("prediction_id"), pricing_version=row.get("pricing_version"),
        source=row.get("source"), bookmaker=row.get("bookmaker") or row.get("source"), market=row.get("market"),
        market_type=row.get("market_type"), period=row.get("period"), outcome_set=row.get("outcome_set"),
        line=_f(row.get("line")), display_line=_f(row.get("display_line")),
        pricing_status_reason=row.get("status_reason"), settlement_rule=row.get("settlement_rule"),
        decimal_odds=_f(row.get("decimal_odds")), p_win=_f(row.get("model_prob")), p_push=_f(row.get("push_prob")),
        p_loss=_f(row.get("loss_prob")), ev_per_unit=_f(row.get("ev_per_unit")),
        edge_vs_fair=_f(row.get("edge_vs_fair")), fair_no_vig_prob=_f(row.get("fair_no_vig_prob")),
        odds_last_seen_at=ensure_utc(parse_utc(odds_last_seen_at)) if odds_last_seen_at else None,
        model_version=row.get("model_version"), artifact_version=row.get("artifact_version"),
        pricing_warnings=tuple(_json(row.get("warnings"), [])),
        data_quality_flags=tuple((diag or {}).get("data_quality_flags") or ()),
        preset_unavailable=preset_unavailable)


def candidates_from_market_pricing(mp: pricing_engine.MarketPricing, *, game_start_utc: datetime
                                   ) -> list[SizingCandidate]:
    """D.4 歷史重建：記憶體中的定價結果（不需要先寫入 DB）。"""
    return [candidate_from_pricing_row(r, game_start_utc=game_start_utc, odds_last_seen_at=mp.odds_last_seen_at)
            for r in mp.to_rows()]


# ------------------------------------------------------------------ #
# 輸出                                                                  #
# ------------------------------------------------------------------ #

@dataclass
class SizingResult:
    sizing_version: str
    risk_policy_version: str
    kelly_math_version: str
    market_pricing_snapshot_id: int | None
    odds_snapshot_id: int | None
    prediction_id: int | None
    pricing_version: str | None
    game_id: int
    betting_day: date
    analysis_as_of: datetime
    pricing_analysis_as_of: datetime
    source: str | None
    bookmaker: str | None
    market: str | None
    market_type: str | None
    period: str | None
    outcome_set: str | None
    line: float | None
    side: str
    display_line: float | None
    settlement_rule: str | None
    decimal_odds: float | None
    p_win: float | None
    p_push: float | None
    p_loss: float | None
    ev_per_unit: float | None
    edge_vs_fair: float | None                 # 診斷用（市場分歧）；不是 stake 依據
    kelly_multiplier: float
    max_bet_fraction: float
    max_game_fraction: float
    max_day_fraction: float
    qualification_status: str = UNAVAILABLE
    mathematically_eligible: bool = False
    actionable: bool = False
    full_kelly_fraction: float | None = None
    fractional_kelly_fraction: float | None = None
    single_bet_capped_fraction: float | None = None
    game_exposure_before: float = 0.0
    game_scale_factor: float = 1.0
    game_exposure_after: float = 0.0
    game_adjusted_fraction: float = 0.0
    daily_exposure_before: float = 0.0
    daily_scale_factor: float = 1.0
    daily_exposure_after: float = 0.0
    final_stake_fraction: float = 0.0
    odds_fetched_at: datetime | None = None
    odds_last_seen_at: datetime | None = None
    quote_age_seconds: float | None = None
    last_seen_age_seconds: float | None = None
    max_quote_age_seconds: float | None = None
    portfolio_key: str | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _member: tuple = field(default=(), repr=False, compare=False)
    _market: tuple = field(default=(), repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["betting_day"] = self.betting_day.isoformat()
        for k in ("analysis_as_of", "pricing_analysis_as_of", "odds_fetched_at", "odds_last_seen_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    def stake_amount(self, bankroll_amount: float) -> float:
        """呼叫端提供的 bankroll 金額 × final_stake_fraction（只供 CLI 顯示；DB 只存比例）。"""
        b = float(bankroll_amount)
        if not math.isfinite(b) or b < 0:
            raise ValueError("bankroll_amount 必須是 ≥ 0 的有限值")
        return b * self.final_stake_fraction


# ------------------------------------------------------------------ #
# 1. 逐 outcome qualification                                           #
# ------------------------------------------------------------------ #

def _frac(x: float) -> float:
    return abs(float(x)) % 1.0


def _is_quarter(x: float | None) -> bool:
    return x is not None and (math.isclose(_frac(x), 0.25) or math.isclose(_frac(x), 0.75))


def _pricing_gate(c: SizingCandidate) -> tuple[str, str] | None:
    """定價狀態 / 結算規則 → (status, reason)；None = 可以進入機率檢查。"""
    ps, why = c.pricing_status, c.pricing_status_reason
    if ps == pricing_engine.MARKET_ONLY:
        return NO_PREDICTION, f"pricing:{ps}:{why or 'no_valid_prediction'}"
    if ps in (pricing_engine.UNSUPPORTED_SETTLEMENT, pricing_engine.UNSUPPORTED_MARKET):
        return UNSUPPORTED_SETTLEMENT, f"pricing:{ps}:{why or c.settlement_rule}"
    if ps == pricing_engine.REJECTED:
        if why and why.startswith("market_not_open"):
            return MARKET_NOT_OPEN, f"pricing:{why}"
        return UNAVAILABLE, f"invalid_pricing_row:{why or 'rejected'}"
    if ps != pricing_engine.PRICED:
        return UNAVAILABLE, f"invalid_pricing_row:unknown_status:{ps}"
    # ---- priced：再次確認結算規則（defense in depth，不依賴單一欄位） ---- #
    if c.settlement_rule not in SUPPORTED_SETTLEMENT_RULES:
        return UNSUPPORTED_SETTLEMENT, f"settlement:{c.settlement_rule}"
    if c.market_type == "moneyline" and c.period == "h1" and c.outcome_set != "three_way":
        return UNSUPPORTED_SETTLEMENT, "settlement:h1_two_way_tie_settlement_unknown"
    if _is_quarter(c.display_line) or (c.market_type in ("spread", "total") and _is_quarter(c.line)):
        return UNSUPPORTED_SETTLEMENT, "settlement:quarter_line_split_settlement"
    if c.prediction_id is None and c.p_win is None:
        return NO_PREDICTION, "no_model_probability"
    return None


def _freshness(c: SizingCandidate, r: SizingResult, as_of: datetime, policy: RiskPolicy) -> str | None:
    """填入報價年齡；回傳 stale 原因（None = 新鮮）。
    last_seen_at > as_of（歷史重建時，之後的輪詢仍確認同一報價）→ 該報價在 as_of 持續掛牌（D.1：fetched_at ≤ T ≤ last_seen_at），
    有效最後確認時間 = as_of。"""
    fetched = ensure_utc(c.odds_fetched_at)
    seen = ensure_utc(c.odds_last_seen_at) if c.odds_last_seen_at else fetched
    seen = min(max(seen, fetched), as_of)
    r.odds_fetched_at, r.odds_last_seen_at = fetched, c.odds_last_seen_at
    r.quote_age_seconds = (as_of - fetched).total_seconds()
    r.last_seen_age_seconds = (as_of - seen).total_seconds()
    max_age, interval = policy.max_quote_age(c.source), policy.poll_interval(c.source)
    if max_age is None:
        return f"poll_interval_unknown:{c.source}"
    r.max_quote_age_seconds = max_age.total_seconds()
    if interval is not None and (as_of - seen) > interval:
        r.warnings.append("quote_not_confirmed_by_latest_poll")
    if (as_of - seen) > max_age:
        return "last_seen_age_exceeds_max"
    return None


def qualify(c: SizingCandidate, *, as_of: datetime, policy: RiskPolicy = RISK_V1) -> SizingResult:
    as_of = ensure_utc(as_of)
    r = SizingResult(
        sizing_version=SIZING_VERSION, risk_policy_version=policy.version, kelly_math_version=kelly.KELLY_MATH_VERSION,
        market_pricing_snapshot_id=c.market_pricing_snapshot_id, odds_snapshot_id=c.odds_snapshot_id,
        prediction_id=c.prediction_id, pricing_version=c.pricing_version, game_id=c.game_id,
        betting_day=betting_day(c.game_start_utc), analysis_as_of=as_of,
        pricing_analysis_as_of=ensure_utc(c.pricing_analysis_as_of), source=c.source, bookmaker=c.bookmaker,
        market=c.market, market_type=c.market_type, period=c.period, outcome_set=c.outcome_set, line=c.line,
        side=c.side, display_line=c.display_line, settlement_rule=c.settlement_rule, decimal_odds=c.decimal_odds,
        p_win=c.p_win, p_push=c.p_push, p_loss=c.p_loss, ev_per_unit=c.ev_per_unit, edge_vs_fair=c.edge_vs_fair,
        kelly_multiplier=policy.kelly_multiplier, max_bet_fraction=policy.max_bet_fraction,
        max_game_fraction=policy.max_game_fraction, max_day_fraction=policy.max_day_fraction,
        _member=c.member_id(), _market=c.market_key())
    r.warnings.extend(w for w in c.pricing_warnings if w)
    r.warnings.extend(f"data_quality:{f}" for f in c.data_quality_flags)   # 只警示，不改 stake

    def done(status: str, reason: str) -> SizingResult:
        r.qualification_status = status
        r.reasons.append(reason)
        return r

    # ---- hard checks（任何一項 → 不計算 stake） ---- #
    if ensure_utc(c.pricing_analysis_as_of) > as_of or ensure_utc(c.odds_fetched_at) > as_of:
        return done(UNAVAILABLE, "future_data_violation")
    stale = _freshness(c, r, as_of, policy)
    if ensure_utc(c.game_start_utc) <= as_of:
        return done(MARKET_NOT_OPEN, "game_started")
    if c.preset_unavailable:
        return done(UNAVAILABLE, c.preset_unavailable)
    gate = _pricing_gate(c)
    if gate is not None:
        return done(*gate)
    try:
        pw, pp, pl, _ = kelly.validate(c.p_win, c.p_push, c.p_loss, c.decimal_odds)
    except kelly.KellyInputError as e:
        return done(INVALID_PROBABILITY, f"invalid_probability:{e.reason}")
    if c.settlement_rule in NO_PUSH_RULES and pp != 0.0:
        return done(INVALID_PROBABILITY, "invalid_probability:push_on_no_push_market")
    ev = kelly.expected_value(pw, pp, pl, c.decimal_odds)
    if c.ev_per_unit is None or not math.isfinite(c.ev_per_unit) or abs(ev - c.ev_per_unit) > EV_CONSISTENCY_TOL:
        return done(INVALID_PROBABILITY, "invalid_probability:ev_mismatch")

    # ---- Kelly（只用 P(win) / P(push) / P(loss) / 實際賠率） ---- #
    full = kelly.full_kelly_fraction(pw, pp, pl, c.decimal_odds)
    r.full_kelly_fraction = full
    r.fractional_kelly_fraction = max(0.0, full * policy.kelly_multiplier)
    r.single_bet_capped_fraction = min(r.fractional_kelly_fraction, policy.max_bet_fraction)
    if full <= 0.0:
        return done(NO_POSITIVE_EV, "ev_not_positive")
    r.mathematically_eligible = True
    if r.fractional_kelly_fraction > policy.max_bet_fraction:
        r.reasons.append("single_bet_cap_applied")
    if stale is not None:
        return done(STALE_QUOTE, f"stale_quote:{stale}")
    r.qualification_status = ELIGIBLE
    return r


# ------------------------------------------------------------------ #
# 2–4. 互斥 / exposure                                                  #
# ------------------------------------------------------------------ #

def _apply_mutual_exclusivity(results: list[SizingResult]) -> None:
    by_market: dict[tuple, list[SizingResult]] = {}
    for r in results:
        by_market.setdefault(r._market, []).append(r)
    for rs in by_market.values():
        positives = [r for r in rs if (r.full_kelly_fraction or 0.0) > 0.0]
        if len(positives) <= 1:
            continue
        sides = ",".join(sorted(r.side for r in positives))
        for r in rs:
            r.qualification_status = MUTUALLY_EXCLUSIVE
            r.mathematically_eligible = False
            r.reasons.append(f"mutually_exclusive_positive_kelly:{sides}")


def _apply_exposure(results: list[SizingResult], policy: RiskPolicy) -> None:
    def scale(total: float, cap: float) -> float:
        return 1.0 if total <= cap else cap / total

    participants = [r for r in results if r.qualification_status == ELIGIBLE]
    by_game: dict[int, list[SizingResult]] = {}
    for r in participants:
        by_game.setdefault(r.game_id, []).append(r)
    game_stats: dict[int, tuple[float, float, float]] = {}
    for gid, rs in by_game.items():
        before = math.fsum(r.single_bet_capped_fraction for r in rs)
        s = scale(before, policy.max_game_fraction)
        for r in rs:
            r.game_adjusted_fraction = r.single_bet_capped_fraction * s
        game_stats[gid] = (before, s, math.fsum(r.game_adjusted_fraction for r in rs))

    by_day: dict[date, list[SizingResult]] = {}
    for r in participants:
        by_day.setdefault(r.betting_day, []).append(r)
    day_stats: dict[date, tuple[float, float, float]] = {}
    for d, rs in by_day.items():
        before = math.fsum(r.game_adjusted_fraction for r in rs)
        s = scale(before, policy.max_day_fraction)
        for r in rs:
            r.final_stake_fraction = r.game_adjusted_fraction * s
        day_stats[d] = (before, s, math.fsum(r.final_stake_fraction for r in rs))

    for r in results:
        r.game_exposure_before, r.game_scale_factor, r.game_exposure_after = game_stats.get(r.game_id, (0.0, 1.0, 0.0))
        r.daily_exposure_before, r.daily_scale_factor, r.daily_exposure_after = day_stats.get(
            r.betting_day, (0.0, 1.0, 0.0))
        if r.qualification_status != ELIGIBLE:
            r.game_adjusted_fraction = r.final_stake_fraction = 0.0
            r.actionable = False
            continue
        if r.game_scale_factor < 1.0:
            r.reasons.append("game_exposure_scaled")
        if r.daily_scale_factor < 1.0:
            r.reasons.append("daily_exposure_scaled")
        if r.game_scale_factor < 1.0 or r.daily_scale_factor < 1.0:
            r.qualification_status = EXPOSURE_SCALED
        r.actionable = r.final_stake_fraction > 0.0


def _portfolio_keys(results: list[SizingResult], policy: RiskPolicy) -> None:
    """同一 betting day 的組合身分：成員（定價列）+ 各自 exposure 前的狀態 + policy。
    成員或狀態不變 → 同一個 key（重跑冪等）；任一改變 → 新 key → 新的 sizing 列（舊列保留）。"""
    by_day: dict[date, list[SizingResult]] = {}
    for r in results:
        by_day.setdefault(r.betting_day, []).append(r)
    for d, rs in by_day.items():
        members = sorted([list(map(str, r._member)), r.qualification_status] for r in rs)
        payload = json.dumps({"sizing_version": SIZING_VERSION, "policy": policy.to_dict(),
                              "betting_day": d.isoformat(), "members": members}, sort_keys=True)
        key = hashlib.sha256(payload.encode()).hexdigest()[:32]
        for r in rs:
            r.portfolio_key = key


def _sort_key(r: SizingResult) -> tuple:
    return (r.betting_day, r.game_id, r.source or "", r.bookmaker or "", r.market or "",
            r.odds_snapshot_id or 0, r.prediction_id or 0, SIDE_ORDER.get(r.side, 9), r.side)


def size_portfolio(candidates: Iterable[SizingCandidate], *, as_of: datetime, policy: RiskPolicy = RISK_V1,
                   incomplete_days: dict[date, str] | None = None) -> list[SizingResult]:
    """incomplete_days：選擇層發現該 betting day 有可下注盤口沒有對應定價（定價落後 / 失敗）→ 該日 exposure 無法完整計算，
    全部 eligible outcome 改為 unavailable（portfolio_incomplete），不給 actionable。"""
    as_of = ensure_utc(as_of)
    results = sorted((qualify(c, as_of=as_of, policy=policy) for c in candidates), key=_sort_key)
    _apply_mutual_exclusivity(results)
    for r in results:
        why = (incomplete_days or {}).get(r.betting_day)
        if why:
            r.warnings.append(f"portfolio_incomplete:{why}")
            if r.qualification_status == ELIGIBLE:
                r.qualification_status = UNAVAILABLE
                r.reasons.append("portfolio_incomplete")
    _apply_exposure(results, policy)
    _portfolio_keys(results, policy)
    return results
