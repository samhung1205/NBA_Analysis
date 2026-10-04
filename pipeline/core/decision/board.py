"""
Decision board（decision-v1；純函式：輸入 DB 列 → 一個 user × betting day 的物化決策檢視）
------------------------------------------------------------
    build_board(BoardInputs) -> BoardResult

三個概念分開呈現：
    model opportunity   台彩 D.2 定價 × D.3 risk-v1（as_of = 物化時點、台彩-only 組合）→ theoretical sizing
    paper decision      D.4 execution-v1 在 T-60 凍結的 prospective decision（只顯示對照；不建立任何 bet）
    actual bet          使用者手動記錄的 bets（占用 risk-v1 額度；與 paper 結果完全分開）

每個台彩 outcome 的 decision_status（precedence 由上而下）：
    already_recorded              同一 game × market 已有有效實際下注（新增額度 0；不 top-up）
    no_positive_ev / unsupported  D.3：EV ≤ 0 / 不支援的結算
    stale_odds / no_prediction / no_odds / data_incomplete   D.3：報價過舊 / 無預測 / 市場未開 / 組合不完整或不合法
    bankroll_unavailable          沒有 bankroll 或無法凍結 day_start（不產生 actionable 金額）
    data_incomplete               實際下注資料不完整（exposure 無法完整計算 → 保守：不給新額度）
    actual_exposure_over_limit    實際 exposure 已超過 risk-v1（單日或同場）→ 新額度 0（不改歷史、不用負數額度）
    risk_cap_reached              剩餘額度 = 0
    qualified                     有正的新增額度（資料品質旗標 → status_group = review）
國際盤（Odds API）只輸出 diagnostic（EV 等），永遠沒有額度、不是台彩證據。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..execution.asof import quote_is_stale
from ..execution.policy import StrategyScope
from ..pricing.alignment import latest_snapshots_as_of, select_prediction
from ..pricing.job import HORIZON
from ..sizing import engine as sz
from ..sizing.job import select_candidates
from ..timeutil import ensure_utc, parse_utc
from . import bankroll as bk
from . import exposure as ex
from . import policy as P

GROUP_ORDER = {P.ACTIONABLE: 0, P.REVIEW: 1, P.RECORDED: 2, P.BLOCKED: 3, P.INACTIVE: 4, P.DIAGNOSTIC: 5}
STATUS_GROUP = {P.QUALIFIED: P.ACTIONABLE, P.ALREADY_RECORDED: P.RECORDED, P.NO_POSITIVE_EV: P.INACTIVE,
                P.UNSUPPORTED: P.INACTIVE, P.STALE_ODDS: P.BLOCKED, P.NO_PREDICTION: P.BLOCKED, P.NO_ODDS: P.BLOCKED,
                P.RISK_CAP_REACHED: P.BLOCKED, P.ACTUAL_EXPOSURE_OVER_LIMIT: P.BLOCKED,
                P.BANKROLL_UNAVAILABLE: P.BLOCKED, P.DATA_INCOMPLETE: P.BLOCKED}
D3_TO_STATUS = {sz.NO_POSITIVE_EV: P.NO_POSITIVE_EV, sz.UNSUPPORTED_SETTLEMENT: P.UNSUPPORTED,
                sz.STALE_QUOTE: P.STALE_ODDS, sz.NO_PREDICTION: P.NO_PREDICTION, sz.MARKET_NOT_OPEN: P.NO_ODDS,
                sz.MUTUALLY_EXCLUSIVE: P.DATA_INCOMPLETE, sz.INVALID_PROBABILITY: P.DATA_INCOMPLETE,
                sz.UNAVAILABLE: P.DATA_INCOMPLETE}
BAD_INGESTION_OUTCOMES = ("blocked", "auth_failed", "network_failure", "parser_changed", "error", "not_configured",
                          "rate_limited")
PRICING_FIELDS = ("market_type", "period", "outcome_set", "line", "display_line", "model_target", "model_threshold",
                  "comparator", "settlement_rule", "decimal_odds", "raw_implied_prob", "fair_no_vig_prob",
                  "market_overround", "model_prob", "push_prob", "loss_prob", "edge_vs_fair", "ev_per_unit",
                  "artifact_version", "pricing_version", "prediction_id")


@dataclass
class BoardInputs:
    now: datetime
    betting_day: date
    user_id: int
    games: list[dict]                     # 該 betting day 的全部比賽（id, date_utc, status）
    odds_rows: list[dict]                 # 這些比賽的 odds_snapshots（fetched_at ≤ now；全部來源）
    pred_rows: list[dict]
    pricing_rows: list[dict]              # market_pricing_snapshots（analysis_as_of ≤ now）
    sizing_rows: list[dict] = field(default_factory=list)      # bet_sizing_snapshots（只取 id 作 provenance）
    bets: list[ex.ActualBet] = field(default_factory=list)       # 使用者全部注單（bankroll 需要跨日）
    account: dict | None = None
    ledger: list[bk.LedgerEntry] = field(default_factory=list)
    day_snapshot: dict | None = None      # 已凍結的 bankroll_day_snapshots 列
    bet_event_watermark: int = 0          # 物化時最大 user 端 bet_events id（Node 據此判斷是否需要重算）
    twsport_source: dict | None = None    # data_sources（source_key = twsport）
    paper: dict[int, dict] | None = None  # game_id → paper decision 摘要；None = paper ledger 不可用
    evidence: dict = field(default_factory=dict)
    horizon: timedelta = HORIZON
    # 只供 seed / fixture（seed 預測不是 production ml-v2.0，無法走 production 選擇）；production 一律 None
    candidate_selector: Callable[..., Any] | None = None
    prediction_selector: Callable[[list[dict], int, datetime], dict | None] | None = None


@dataclass
class BoardResult:
    snapshot: dict[str, Any]
    opportunities: list[dict[str, Any]]
    new_day_start: bk.DayStart | None = None
    bet_fractions: dict[int, float] = field(default_factory=dict)   # bet_id → stake / day_start（填 bets 一次）
    fingerprint: str = ""


# ------------------------------------------------------------------ #
# 台彩盤口狀態（為什麼沒有 actionable）                                    #
# ------------------------------------------------------------------ #

def taiwan_odds_state(game: dict, now: datetime, tw_rows: list[dict], source: dict | None, *,
                      horizon: timedelta = HORIZON) -> tuple[str, str | None]:
    start = ensure_utc(parse_utc(game["date_utc"]))
    if (game.get("status") or "scheduled") != "scheduled" or start <= now:
        return P.TW_GAME_STARTED, game.get("status")
    latest = latest_snapshots_as_of(tw_rows, now)
    if latest:
        open_ = [r for r in latest if (r.get("market_status") or "open") == "open"]
        if not open_:
            return P.TW_MARKET_CLOSED, "all_markets_suspended_or_closed"
        fresh = [r for r in open_ if not quote_is_stale(
            "twsport", parse_utc(r["fetched_at"]), parse_utc(r.get("last_seen_at")) or parse_utc(r["fetched_at"]),
            now, P.RISK_POLICY)]
        return (P.TW_AVAILABLE, None) if fresh else (P.TW_STALE, "last_seen_age_exceeds_2x_poll_interval")
    if start > now + horizon:
        return P.TW_OUTSIDE_WINDOW, f"tipoff_beyond_{int(horizon.total_seconds() // 3600)}h_pricing_window"
    if source is None:
        return P.TW_NOT_CONFIGURED, "twsport_ingestion_never_ran"
    outcome = source.get("last_outcome")
    if outcome in BAD_INGESTION_OUTCOMES or source.get("last_status") == "error":
        return P.TW_INGESTION_UNAVAILABLE, f"{outcome or source.get('last_status')}:har_not_imported"
    last_ok = parse_utc(source.get("last_success_at"))
    interval = P.RISK_POLICY.poll_interval("twsport")
    if last_ok is None or (interval is not None and now - ensure_utc(last_ok) > 2 * interval):
        return P.TW_INGESTION_UNAVAILABLE, "ingestion_stale:har_not_imported"
    return P.TW_NOT_PUBLISHED, None


# ------------------------------------------------------------------ #
# helpers                                                              #
# ------------------------------------------------------------------ #

def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _iso(v) -> str | None:
    t = parse_utc(v)
    return ensure_utc(t).isoformat() if t else None


def _json(v, fb):
    if v is None:
        return fb
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return fb


def _prediction_summary(pred_rows: list[dict], gid: int, now: datetime) -> dict | None:
    p = select_prediction(pred_rows, gid, now)
    if p is None:
        return None
    r = p.row
    h, a = _f(r.get("pred_home_h1")), _f(r.get("pred_away_h1"))
    fj = _json(r.get("features_json"), {}) or {}
    return {"prediction_id": p.prediction_id, "model_version": p.model_version, "artifact_version": p.artifact_version,
            "prediction_kind": p.prediction_kind, "created_at": _iso(p.created_at),
            "available_at": _iso(p.available_at), "pred_margin": _f(r.get("pred_margin")),
            "pred_total": _f(r.get("pred_total")),
            "h1_margin": (h - a) if (h is not None and a is not None) else None,
            "h1_total": (h + a) if (h is not None and a is not None) else None,
            "data_quality_flags": list(((fj.get("data_quality") or {}).get("flags")) or [])}


def _floor_amount(x: float) -> float:
    return math.floor(x / P.CURRENCY_STEP) * P.CURRENCY_STEP


def _opp_base(gid: int, pricing: dict, odds_row: dict | None, scope_kind: str, label: str) -> dict[str, Any]:
    o = {"game_id": gid, "scope_kind": scope_kind, "evidence_label": label, "source": pricing["source"],
         "bookmaker": pricing.get("bookmaker") or pricing["source"], "market": pricing["market"],
         "side": pricing["side"], "odds_snapshot_id": pricing.get("odds_snapshot_id"),
         "odds_fetched_at": _iso(pricing.get("odds_fetched_at")),
         "odds_last_seen_at": _iso((odds_row or {}).get("last_seen_at")),
         "pricing_snapshot_id": pricing.get("id")}
    for k in PRICING_FIELDS:
        v = pricing.get(k)
        o[k] = _f(v) if k not in ("market_type", "period", "outcome_set", "model_target", "comparator",
                                  "settlement_rule", "artifact_version", "pricing_version", "prediction_id") else v
    return o


# ------------------------------------------------------------------ #
# build                                                                #
# ------------------------------------------------------------------ #

def build_board(inp: BoardInputs) -> BoardResult:
    now, day = ensure_utc(inp.now), inp.betting_day
    pol = P.RISK_POLICY
    games = sorted(inp.games, key=lambda g: (ensure_utc(parse_utc(g["date_utc"])), int(g["id"])))
    gstart = {int(g["id"]): ensure_utc(parse_utc(g["date_utc"])) for g in games}
    pricing_by_id = {int(r["id"]): r for r in inp.pricing_rows if r.get("id") is not None}
    odds_by_id = {int(r["id"]): r for r in inp.odds_rows if r.get("id") is not None}
    sizing_ref: dict[int, tuple] = {}
    for s in inp.sizing_rows:
        k = int(s["market_pricing_snapshot_id"])
        key = (ensure_utc(parse_utc(s["analysis_as_of"])), int(s["id"]))
        if k not in sizing_ref or key > sizing_ref[k]:
            sizing_ref[k] = key

    # ---- 1. 台彩：as_of = now 的 D.3（台彩-only 組合；與 D.4 scope 先過濾一致） ---- #
    scope = P.PRIMARY
    tw_odds = [r for r in inp.odds_rows if scope.matches(r)]
    tw_pricing = [r for r in inp.pricing_rows if scope.matches(r)]
    upcoming = [g for g in games if (g.get("status") or "scheduled") == "scheduled"]
    selector = inp.candidate_selector or select_candidates
    sel = selector(upcoming, tw_odds, inp.pred_rows, tw_pricing, now)
    results = sz.size_portfolio(sel.candidates, as_of=now, policy=pol, incomplete_days=sel.incomplete_days)
    participants_all = [r for r in results if r.qualification_status in ex.PARTICIPANT_STATUSES]

    # ---- 2. bankroll / day start ---- #
    day_bets = [b for b in inp.bets if b.betting_day == day]
    account = inp.account
    has_ledger = bool(inp.ledger)
    day_start: bk.DayStart | None = None
    new_day_start = None
    bankroll_reason = None
    if inp.day_snapshot is not None:
        d = inp.day_snapshot
        day_start = bk.DayStart(day, float(d["day_start_bankroll"]), ensure_utc(parse_utc(d["basis_as_of"])),
                                float(d["ledger_balance"]), float(d["open_stake_excluded"]), int(d["ledger_watermark"]),
                                d["established_reason"], d.get("bankroll_version") or P.BANKROLL_VERSION)
    elif account is None or not has_ledger:
        bankroll_reason = "bankroll_not_configured"
    elif participants_all or day_bets:
        basis, why = bk.basis_for_day(day, inp.bets, now)
        day_start = bk.compute_day_start(day, inp.ledger, inp.bets, basis, why)
        if day_start is None:
            bankroll_reason = "day_start_not_positive_at_basis"
        else:
            new_day_start = day_start
    base = day_start.day_start_bankroll if day_start else None

    # ---- 3. actual exposure ---- #
    expo = ex.actual_exposure(inp.bets, day, base)
    recorded = ex.recorded_markets(inp.bets, day)
    bet_fractions = {}
    if base is not None:
        for b in day_bets:
            if b.stake_valid():
                bet_fractions[b.bet_id] = b.stake / base
    exposure_known = expo.day_fraction is not None and not expo.incomplete

    # ---- 4. 台彩 outcome → decision status ---- #
    participants = [r for r in participants_all if (r.game_id, r.market) not in recorded]
    rescaled: dict[int, ex.Rescaled] = {}
    if exposure_known and participants:
        rescaled = ex.rescale_with_actual(participants, expo, policy=pol)
    resc_by_member = {id(participants[i]): v for i, v in rescaled.items()}
    paper = inp.paper or {}
    opps: list[dict[str, Any]] = []
    for r in results:
        pr = pricing_by_id.get(int(r.market_pricing_snapshot_id)) if r.market_pricing_snapshot_id else None
        if pr is None:
            continue
        o = _opp_base(r.game_id, pr, odds_by_id.get(int(pr["odds_snapshot_id"])), "taiwan_primary", P.TAIWAN_LABEL)
        o.update(d3_qualification_status=r.qualification_status, full_kelly_fraction=r.full_kelly_fraction,
                 fractional_kelly_fraction=r.fractional_kelly_fraction,
                 single_bet_capped_fraction=r.single_bet_capped_fraction,
                 theoretical_final_fraction=r.final_stake_fraction,
                 sizing_snapshot_id=sizing_ref.get(int(r.market_pricing_snapshot_id), (None, None))[1],
                 day_start_bankroll=base, remaining_day_fraction=None, remaining_game_fraction=None,
                 actual_game_exposure=expo.fraction_for_game(r.game_id), game_scale_factor=None,
                 day_scale_factor=None, user_adjusted_fraction=None, max_additional_stake_amount=None,
                 suggested_stake_amount=None, linked_bet_ids=[], linked_actual_fraction=None,
                 reasons=list(r.reasons), warnings=list(r.warnings), paper_decision_id=None)
        if expo.day_fraction is not None:
            o["remaining_day_fraction"] = max(0.0, pol.max_day_fraction - expo.day_fraction)
            o["remaining_game_fraction"] = max(0.0, pol.max_game_fraction - expo.fraction_for_game(r.game_id))
        pd_ = paper.get(r.game_id)
        if pd_ and any(w.get("market") == r.market and w.get("side") == r.side for w in pd_.get("wagers", [])):
            o["paper_decision_id"] = pd_.get("decision_id")
        status = None
        linked = recorded.get((r.game_id, r.market))
        if linked:
            same = [b for b in linked if b.side == r.side]
            status = P.ALREADY_RECORDED
            o["linked_bet_ids"] = sorted(b.bet_id for b in linked)
            if same:
                o["reasons"].append("linked_actual_bet")
                if base is not None:
                    frac = math.fsum(b.stake for b in same if b.stake_valid()) / base
                    o["linked_actual_fraction"] = frac
                    if frac > (r.final_stake_fraction or 0.0) + P.OVER_LIMIT_TOL:
                        o["warnings"].append("over_theoretical_target")
                    elif frac + P.OVER_LIMIT_TOL < (r.final_stake_fraction or 0.0):
                        o["reasons"].append("below_theoretical_target_no_top_up")
            else:
                o["reasons"].append("market_has_recorded_bet:" + ",".join(sorted({b.side or '?' for b in linked})))
            o["user_adjusted_fraction"] = 0.0
            o["reasons"].append("no_top_up:decision-v1")
        elif r.qualification_status not in ex.PARTICIPANT_STATUSES:
            status = D3_TO_STATUS.get(r.qualification_status, P.DATA_INCOMPLETE)
            o["user_adjusted_fraction"] = 0.0
        elif expo.incomplete:
            status = P.DATA_INCOMPLETE
            o["reasons"].append("actual_exposure_incomplete")
            o["user_adjusted_fraction"] = 0.0
        elif id(r) not in resc_by_member:
            status = P.BANKROLL_UNAVAILABLE                      # 有實際下注但沒有 day_start：比例無法計算
            o["reasons"].append(bankroll_reason or "day_start_not_established")
        else:
            rs = resc_by_member[id(r)]
            o.update(user_adjusted_fraction=rs.user_adjusted_fraction, game_scale_factor=rs.game_scale_factor,
                     day_scale_factor=rs.day_scale_factor, remaining_game_fraction=rs.remaining_game_fraction,
                     remaining_day_fraction=rs.remaining_day_fraction, actual_game_exposure=rs.actual_game_exposure)
            if rs.game_scale_factor < 1.0:
                o["reasons"].append("game_capacity_scaled_with_actual_exposure")
            if rs.day_scale_factor < 1.0:
                o["reasons"].append("daily_capacity_scaled_with_actual_exposure")
            if rs.day_over_limit or rs.game_over_limit:
                status = P.ACTUAL_EXPOSURE_OVER_LIMIT
                o["user_adjusted_fraction"] = 0.0
                o["reasons"].append("actual_day_exposure_over_limit" if rs.day_over_limit
                                    else "actual_game_exposure_over_limit")
            elif rs.user_adjusted_fraction <= P.EXPOSURE_EPS:
                status = P.RISK_CAP_REACHED
                o["user_adjusted_fraction"] = 0.0
                o["reasons"].append("no_remaining_daily_capacity" if rs.remaining_day_fraction <= 0
                                    else "no_remaining_game_capacity")
            elif base is None:
                status = P.BANKROLL_UNAVAILABLE                  # 比例照算（理論額度），但不產生可操作金額
                o["reasons"].append(bankroll_reason or "day_start_not_established")
            else:
                status = P.QUALIFIED
                amt = rs.user_adjusted_fraction * base
                o["max_additional_stake_amount"] = amt
                o["suggested_stake_amount"] = _floor_amount(amt)
        o["decision_status"] = status
        grp = STATUS_GROUP[status]
        if status == P.QUALIFIED and any(w.startswith("data_quality:") for w in o["warnings"]):
            grp = P.REVIEW
        o["status_group"] = grp
        opps.append(o)

    # ---- 5. 國際盤 diagnostic（只有定價數字；沒有額度、不是台彩證據） ---- #
    intl_odds = [r for r in inp.odds_rows if r.get("source") == "oddsapi"]
    intl_pricing = [r for r in inp.pricing_rows if r.get("source") == "oddsapi"]
    isel = selector(upcoming, intl_odds, inp.pred_rows, intl_pricing, now)
    for c in isel.candidates:
        pr = pricing_by_id.get(int(c.market_pricing_snapshot_id)) if c.market_pricing_snapshot_id else None
        if pr is None:
            continue
        o = _opp_base(c.game_id, pr, odds_by_id.get(int(pr["odds_snapshot_id"])), "international_diagnostic",
                      P.INTERNATIONAL)
        o.update(d3_qualification_status=None, full_kelly_fraction=None, fractional_kelly_fraction=None,
                 single_bet_capped_fraction=None, theoretical_final_fraction=None, sizing_snapshot_id=None,
                 day_start_bankroll=None, remaining_day_fraction=None, remaining_game_fraction=None,
                 actual_game_exposure=None, game_scale_factor=None, day_scale_factor=None,
                 user_adjusted_fraction=None, max_additional_stake_amount=None, suggested_stake_amount=None,
                 linked_bet_ids=[], linked_actual_fraction=None, paper_decision_id=None,
                 reasons=["international_market_diagnostic:not_taiwan_sports_lottery_evidence"],
                 warnings=list(_json(pr.get("warnings"), [])), decision_status=(
                     P.NO_PREDICTION if pr.get("status") == "market_only" else
                     P.UNSUPPORTED if pr.get("status") != "priced" else
                     P.QUALIFIED if (_f(pr.get("ev_per_unit")) or 0) > 0 else P.NO_POSITIVE_EV),
                 status_group=P.DIAGNOSTIC)
        opps.append(o)

    # ---- 6. display_rank（只為 UI 排序：actionable → EV 高 → 開賽早；不影響 qualification / stake） ---- #
    def rank_key(o):
        ev = o.get("ev_per_unit")
        return (GROUP_ORDER[o["status_group"]], -(ev if ev is not None else -9.0), gstart[o["game_id"]],
                o["game_id"], o["source"], o["bookmaker"], o["market"], sz.SIDE_ORDER.get(o["side"], 9))
    for i, o in enumerate(sorted(opps, key=rank_key), 1):
        o["display_rank"] = i
    opps.sort(key=lambda o: o["display_rank"])

    # ---- 7. 每場摘要 ---- #
    game_out = []
    for g in games:
        gid = int(g["id"])
        tw_state, tw_reason = taiwan_odds_state(g, now, [r for r in tw_odds if int(r["game_id"]) == gid],
                                                inp.twsport_source, horizon=inp.horizon)
        mine = [o for o in opps if o["game_id"] == gid and o["scope_kind"] == "taiwan_primary"]
        groups = {o["status_group"] for o in mine}
        if P.ACTIONABLE in groups:
            gs = P.ACTIONABLE
        elif P.REVIEW in groups:
            gs = P.REVIEW
        elif P.RECORDED in groups:
            gs = P.RECORDED
        elif tw_state != P.TW_AVAILABLE or P.BLOCKED in groups:
            gs = P.BLOCKED
        else:
            gs = P.INACTIVE
        pred = (inp.prediction_selector or _prediction_summary)(inp.pred_rows, gid, now)
        blockers = []
        if tw_state != P.TW_AVAILABLE:
            blockers.append(f"taiwan_odds:{tw_state}")
        if pred is None and (g.get("status") or "scheduled") == "scheduled":
            blockers.append("no_prediction")
        blockers += sorted({o["decision_status"] for o in mine if o["status_group"] == P.BLOCKED})
        used = expo.fraction_for_game(gid)
        game_out.append({
            "game_id": gid, "tipoff_utc": gstart[gid].isoformat(), "game_status": g.get("status"),
            "status_group": gs, "taiwan_odds_state": tw_state, "taiwan_odds_reason": tw_reason,
            "prediction": pred, "blockers": blockers,
            "n_taiwan_outcomes": len(mine), "n_qualified": sum(o["decision_status"] == P.QUALIFIED for o in mine),
            "n_international": sum(1 for o in opps if o["game_id"] == gid and o["scope_kind"] != "taiwan_primary"),
            "actual_game_stake": expo.stake_for_game(gid), "actual_game_fraction": used,
            "remaining_game_fraction": None if used is None else max(0.0, pol.max_game_fraction - used),
            "remaining_game_amount": (None if (used is None or base is None)
                                      else max(0.0, pol.max_game_fraction - used) * base),
            "actual_bet_ids": sorted(b.bet_id for b in day_bets if b.active and b.game_id == gid),
            "paper_decision": paper.get(gid) if inp.paper is not None else None})

    # ---- 8. 摘要 / bankroll / evidence ---- #
    rem_day = None if expo.day_fraction is None else max(0.0, pol.max_day_fraction - expo.day_fraction)
    exposure_out = expo.to_dict()
    exposure_out.update({
        "max_day_fraction": pol.max_day_fraction, "max_game_fraction": pol.max_game_fraction,
        "max_bet_fraction": pol.max_bet_fraction, "remaining_day_fraction": rem_day,
        "day_cap_used_share": (None if expo.day_fraction is None
                               else min(1.0, expo.day_fraction / pol.max_day_fraction)),   # UI 進度條（已用 / 上限）
        "remaining_day_amount": None if (rem_day is None or base is None) else rem_day * base,
        "max_bet_amount": None if base is None else pol.max_bet_fraction * base,
        "day_over_limit": expo.day_fraction is not None and expo.day_fraction > pol.max_day_fraction + P.OVER_LIMIT_TOL,
        "games_over_limit": sorted(gid for gid in gstart
                                   if (expo.fraction_for_game(gid) or 0.0) > pol.max_game_fraction + P.OVER_LIMIT_TOL)})
    if account is not None and has_ledger:
        bank = bk.bankroll_summary(inp.ledger, inp.bets, now, day_start=day_start, day_used_fraction=expo.day_fraction,
                                   currency=account.get("currency"))
    else:
        bank = {"configured": False, "currency": (account or {}).get("currency"),
                "reason": bankroll_reason or "bankroll_not_configured"}
    if bankroll_reason and account is not None and has_ledger:
        bank["day_start_unavailable_reason"] = bankroll_reason
    tw_states = [x["taiwan_odds_state"] for x in game_out]
    tw_opps = [o for o in opps if o["scope_kind"] == "taiwan_primary"]
    if not games:
        status = "no_games"
    elif bankroll_reason and (participants_all or day_bets):
        status = P.BANKROLL_UNAVAILABLE
    elif expo.incomplete:
        status = "actual_exposure_incomplete"
    elif exposure_out["day_over_limit"]:
        status = P.ACTUAL_EXPOSURE_OVER_LIMIT
    elif not tw_opps and all(s != P.TW_AVAILABLE for s in tw_states):
        status = "no_taiwan_odds"
    else:
        status = "ok"
    summary = {
        "betting_day": day.isoformat(), "n_games": len(games),
        "n_scheduled": sum((g.get("status") or "scheduled") == "scheduled" for g in games),
        "n_with_prediction": sum(1 for x in game_out if x["prediction"]),
        "n_qualified": sum(o["decision_status"] == P.QUALIFIED for o in tw_opps),
        "n_actionable": sum(o["status_group"] == P.ACTIONABLE for o in tw_opps),
        "n_review": sum(o["status_group"] == P.REVIEW for o in tw_opps),
        "n_already_recorded": sum(o["decision_status"] == P.ALREADY_RECORDED for o in tw_opps),
        "n_blocked_outcomes": sum(o["status_group"] == P.BLOCKED for o in tw_opps),
        "n_positive_ev_taiwan": sum((o.get("ev_per_unit") or 0) > 0 for o in tw_opps),
        "n_international_diagnostic": sum(o["scope_kind"] != "taiwan_primary" for o in opps),
        "taiwan_odds_states": {s: tw_states.count(s) for s in sorted(set(tw_states))},
        "n_games_no_taiwan_odds": sum(s != P.TW_AVAILABLE for s in tw_states),
        "n_games_stale_taiwan_odds": tw_states.count(P.TW_STALE),
        "n_actual_bets": sum(1 for b in day_bets if b.active),
        "evidence_badge": P.EVIDENCE_BADGE, "status": status,
        "language": "Qualified opportunity / Positive EV / Theoretical sizing / Risk-adjusted capacity — "
                    "not a guarantee; prospective validation in progress"}
    risk_limits = {**pol.to_dict(), "decision_version": P.DECISION_VERSION, "bankroll_version": P.BANKROLL_VERSION,
                   "execution_policy_version": P.EXECUTION_POLICY.version, "top_up_allowed": P.TOP_UP_ALLOWED,
                   "min_ev_threshold": P.MIN_EV_THRESHOLD}
    warnings = sorted(set(expo.warnings) | ({"bankroll_not_configured"} if bank.get("configured") is False else set()))
    snapshot = {
        "user_id": inp.user_id, "account_id": (account or {}).get("id"), "betting_day": day,
        "decision_version": P.DECISION_VERSION, "risk_policy_version": pol.version,
        "execution_policy_version": P.EXECUTION_POLICY.version, "scope": f"{scope.source}:{scope.bookmaker}",
        "risk_state_version": (account or {}).get("risk_state_version"),
        "ledger_watermark": bk.ledger_watermark(inp.ledger) if inp.ledger else 0,
        "bet_event_watermark": inp.bet_event_watermark, "status": status,
        "summary": summary, "bankroll": bank, "actual_exposure": exposure_out, "risk_limits": risk_limits,
        "games": game_out, "evidence": inp.evidence, "actual_performance": bk.actual_performance(inp.bets),
        "warnings": warnings}
    fp = fingerprint(snapshot, opps, day_start)
    return BoardResult(snapshot=snapshot, opportunities=opps, new_day_start=new_day_start,
                       bet_fractions=bet_fractions, fingerprint=fp)


def fingerprint(snapshot: dict, opps: list[dict], day_start: bk.DayStart | None) -> str:
    """物化內容（不含時間戳）的雜湊：任何輸入改變（bet / bankroll / 盤口 / 預測 / 新鮮度 / evidence）→ 新 snapshot。"""
    payload = json.dumps({"snapshot": snapshot, "opportunities": opps,
                          "day_start": day_start.to_row() if day_start else None},
                         sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()
