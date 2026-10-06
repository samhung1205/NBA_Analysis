"""
Phase D.3 sizing 工作（DB 讀寫；數學全部在 kelly / engine）
------------------------------------------------------------
    sizing_job(now)                    T = now：未開賽、未來 48 小時比賽所屬的 betting day → 每個 series 最新 open snapshot ×
                                       當時最新有效預測的 D.2 定價列 → qualification / Kelly / exposure → 寫入 bet_sizing_snapshots
    select_candidates(...)             純函式（as-of 選擇，與 D.2 alignment 同一規則）
    size_pricings(pricings, ...)       D.4 歷史重建：記憶體中的 MarketPricing（不需要 DB 定價列；不寫入）

寫入 bet_sizing_snapshots：唯一鍵 (market_pricing_snapshot_id, risk_policy_version, sizing_version, portfolio_key)，
ON CONFLICT DO NOTHING——同一定價輸入 × 同一 policy × 同一組合狀態重跑冪等；已存在的列永不改寫；
policy 改版（risk-v2…）或組合改變（新盤口 / 新預測 / 報價變舊）→ 新列，舊列保留。
不寫入 bets（個人下單紀錄），不保存任何 bankroll 金額。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..pricing import engine as pricing_engine
from ..pricing.alignment import latest_snapshots_as_of, odds_input_from_row, select_prediction
from ..pricing.job import HORIZON
from ..timeutil import ensure_utc, now_utc, parse_utc, tpe_day_bounds_utc
from . import engine
from .policy import PRODUCTION_RISK_POLICY, RiskPolicy, assert_registered

log = logging.getLogger(__name__)

HEARTBEAT = dict(source_key="bet_sizing", display_name="理論注碼（D.3 Kelly / 風險控制）", category="model",
                 expected_interval_min=5)

COLUMNS = (
    "sizing_version", "risk_policy_version", "kelly_math_version", "portfolio_key", "market_pricing_snapshot_id",
    "odds_snapshot_id", "prediction_id", "pricing_version", "game_id", "betting_day", "analysis_as_of",
    "pricing_analysis_as_of", "source", "bookmaker", "market", "market_type", "period", "outcome_set", "line", "side",
    "display_line", "settlement_rule", "decimal_odds", "p_win", "p_push", "p_loss", "ev_per_unit", "edge_vs_fair",
    "full_kelly_fraction", "kelly_multiplier", "fractional_kelly_fraction", "max_bet_fraction",
    "single_bet_capped_fraction", "max_game_fraction", "game_exposure_before", "game_scale_factor",
    "game_exposure_after", "game_adjusted_fraction", "max_day_fraction", "daily_exposure_before",
    "daily_scale_factor", "daily_exposure_after", "final_stake_fraction", "qualification_status",
    "mathematically_eligible", "actionable", "reasons", "warnings", "odds_fetched_at", "odds_last_seen_at",
    "quote_age_seconds", "last_seen_age_seconds", "max_quote_age_seconds")
JSON_COLUMNS = ("reasons", "warnings")


@dataclass
class Selection:
    candidates: list[engine.SizingCandidate] = field(default_factory=list)
    incomplete_days: dict[date, str] = field(default_factory=dict)
    pricing_missing: list[dict[str, Any]] = field(default_factory=list)
    skipped_not_open: int = 0
    invalid_snapshots: int = 0


@dataclass
class SizingReport:
    as_of: str
    risk_policy: dict[str, Any] = field(default_factory=dict)
    games: int = 0
    betting_days: list[str] = field(default_factory=list)
    candidates: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    actionable: int = 0
    portfolio: dict[str, dict[str, Any]] = field(default_factory=dict)
    rows_inserted: int = 0
    rows_existing: int = 0
    pricing_missing: list[dict[str, Any]] = field(default_factory=list)
    skipped_not_open: int = 0
    invalid_snapshots: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None


# ------------------------------------------------------------------ #
# as-of 選擇（純函式）                                                    #
# ------------------------------------------------------------------ #

def select_candidates(games: list[dict], odds_rows: list[dict], pred_rows: list[dict], pricing_rows: list[dict],
                      as_of: datetime, *, pricing_version: str = pricing_engine.PRICING_VERSION) -> Selection:
    """T 時點每場比賽「目前可下注」的定價 outcome：
       每個 series 在 T 的最新 snapshot（非 open → 不可下注、略過；與 D.2 / API 一致）×
       T 時點最新有效預測（無 → market_only 列）的定價列（analysis_as_of ≤ T）。
    找不到對應定價列（定價落後 / artifact 失敗）→ 該 betting day 標記 incomplete（exposure 無法完整計算）。"""
    as_of = ensure_utc(as_of)
    sel = Selection()
    pricing_by_snap: dict[int, list[dict]] = {}
    for p in pricing_rows:
        if p.get("pricing_version") != pricing_version:
            continue
        if ensure_utc(parse_utc(p["analysis_as_of"])) > as_of:
            continue                                            # 之後才成立的定價：T 時點不存在
        pricing_by_snap.setdefault(int(p["odds_snapshot_id"]), []).append(p)
    for g in sorted(games, key=lambda g: int(g["id"])):
        gid, start = int(g["id"]), ensure_utc(parse_utc(g["date_utc"]))
        if start <= as_of:
            continue
        day = engine.betting_day(start)
        pred = select_prediction(pred_rows, gid, as_of)
        want = pred.prediction_id if pred is not None else None
        for r in latest_snapshots_as_of([r for r in odds_rows if int(r["game_id"]) == gid], as_of):
            oi = odds_input_from_row(r)
            if oi.snapshot is None:
                sel.invalid_snapshots += 1
                continue
            if oi.snapshot.status != "open":
                sel.skipped_not_open += 1
                continue
            match = [p for p in pricing_by_snap.get(int(r["id"]), []) if p.get("prediction_id") == want]
            if not match:
                sel.pricing_missing.append({"game_id": gid, "odds_snapshot_id": r["id"], "prediction_id": want})
                sel.incomplete_days[day] = "pricing_missing"
                continue
            preset = None
            if pred is not None and any(p.get("artifact_version") != pred.artifact_version for p in match):
                preset = "artifact_mismatch"
            for p in match:
                sel.candidates.append(engine.candidate_from_pricing_row(
                    p, game_start_utc=start, odds_last_seen_at=r.get("last_seen_at"), preset_unavailable=preset))
    return sel


def size_pricings(pricings: list[pricing_engine.MarketPricing], game_starts: dict[int, datetime], as_of: datetime,
                  *, policy: RiskPolicy = PRODUCTION_RISK_POLICY) -> list[engine.SizingResult]:
    """D.4 歷史重建：pricing.alignment.price_game_as_of() 的輸出直接 sizing（只用 T 以前的資料；不寫入）。"""
    cands = [c for mp in pricings for c in engine.candidates_from_market_pricing(mp, game_start_utc=game_starts[mp.game_id])]
    return engine.size_portfolio(cands, as_of=as_of, policy=policy)


# ------------------------------------------------------------------ #
# DB                                                                   #
# ------------------------------------------------------------------ #

def schema_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('bet_sizing_snapshots') IS NOT NULL AS s, "
                "to_regclass('market_pricing_snapshots') IS NOT NULL AS p")
    r = cur.fetchone()
    return bool(r["s"] and r["p"])


def load_inputs(cur, now: datetime, horizon: timedelta) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """未來 horizon 內有未開賽比賽的 betting day → 該日全部未開賽比賽（daily cap 以整個 betting day 計算）。
    SQL 先以 T 過濾（之後的列不讀進來），select_candidates 再做一次（純函式、有測試）。"""
    cur.execute("SELECT id, date_utc FROM games WHERE date_utc > %s AND date_utc <= %s AND status <> 'final'",
                (now, now + horizon))
    days = sorted({engine.betting_day(r["date_utc"]) for r in cur.fetchall()})
    if not days:
        return [], [], [], []
    start, _ = tpe_day_bounds_utc(days[0])
    _, end = tpe_day_bounds_utc(days[-1])
    cur.execute("SELECT id, date_utc, status FROM games WHERE date_utc > %s AND date_utc >= %s AND date_utc < %s "
                "AND status <> 'final' ORDER BY id", (now, start, end))
    games = cur.fetchall()
    # 定價只涵蓋 horizon 內的比賽：betting day 有比賽超出 horizon → 該日 exposure 不完整，整天延到之後的執行再 sizing
    partial = {engine.betting_day(g["date_utc"]) for g in games if ensure_utc(g["date_utc"]) > now + horizon}
    games = [g for g in games if engine.betting_day(g["date_utc"]) not in partial]
    ids = [g["id"] for g in games]
    if not ids:
        return [], [], [], []
    cur.execute("SELECT * FROM odds_snapshots WHERE game_id = ANY(%s) AND fetched_at <= %s ORDER BY id", (ids, now))
    odds = cur.fetchall()
    cur.execute("""SELECT id, game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                          pred_home_h1, pred_away_h1, features_json
                     FROM predictions WHERE game_id = ANY(%s) AND created_at <= %s ORDER BY id""", (ids, now))
    preds = cur.fetchall()
    cur.execute("SELECT * FROM market_pricing_snapshots WHERE game_id = ANY(%s) AND analysis_as_of <= %s ORDER BY id",
                (ids, now))
    return games, odds, preds, cur.fetchall()


def _db_value(k: str, v: Any) -> Any:
    if k in JSON_COLUMNS:
        return json.dumps(v, ensure_ascii=False, allow_nan=False)
    return v


def persist(cur, results: list[engine.SizingResult]) -> tuple[int, int]:
    inserted = existing = 0
    cols = ", ".join(COLUMNS)
    ph = ", ".join("%s::jsonb" if c in JSON_COLUMNS else "%s" for c in COLUMNS)
    for r in results:
        if r.market_pricing_snapshot_id is None:
            raise ValueError("只有對應 market_pricing_snapshots 的 sizing 結果可以寫入")
        row = {c: getattr(r, c) for c in COLUMNS}
        cur.execute(f"INSERT INTO bet_sizing_snapshots ({cols}) VALUES ({ph}) ON CONFLICT DO NOTHING",
                    tuple(_db_value(c, row[c]) for c in COLUMNS))
        if cur.rowcount == 1:
            inserted += 1
        else:
            existing += 1
    return inserted, existing


def _summarize(rep: SizingReport, results: list[engine.SizingResult], bankroll: float | None, keep: bool) -> None:
    rep.candidates = len(results)
    for r in results:
        rep.by_status[r.qualification_status] = rep.by_status.get(r.qualification_status, 0) + 1
        rep.actionable += int(r.actionable)
        d = rep.portfolio.setdefault(r.betting_day.isoformat(), {
            "daily_exposure_before": r.daily_exposure_before, "daily_scale_factor": r.daily_scale_factor,
            "daily_exposure_after": r.daily_exposure_after, "portfolio_key": r.portfolio_key, "games": {}})
        d["games"].setdefault(str(r.game_id), {"game_exposure_before": r.game_exposure_before,
                                               "game_scale_factor": r.game_scale_factor,
                                               "game_exposure_after": r.game_exposure_after})
    rep.betting_days = sorted(rep.portfolio)
    if keep:
        for r in results:
            d = r.to_dict()
            if bankroll is not None:
                d["bankroll_amount"] = float(bankroll)
                d["stake_amount"] = r.stake_amount(bankroll)
            rep.results.append(d)


def sizing_job(now: datetime | None = None, *, horizon: timedelta = HORIZON, dry_run: bool = False,
               policy: RiskPolicy = PRODUCTION_RISK_POLICY, bankroll: float | None = None, db=None) -> SizingReport:
    from .. import db as default_db

    db = db or default_db
    if not dry_run:
        assert_registered(policy)
    now = ensure_utc(now or now_utc())
    rep = SizingReport(now.isoformat(), risk_policy=policy.to_dict())
    with db.cursor() as cur:
        if not schema_ready(cur):
            rep.note = "資料庫尚未套用 migrations/postgres/0006_phase_d3.sql（或 0005），不計算 sizing"
            if not dry_run:
                db.heartbeat(cur, status="warn", error=rep.note, **HEARTBEAT)
            return rep
        games, odds, preds, pricing = load_inputs(cur, now, horizon)
    rep.games = len(games)
    if not games:
        rep.note = "沒有可 sizing 的 betting day（未來 48 小時沒有未開賽比賽，或該日有比賽超出定價 horizon、延後計算）"
        if not dry_run:
            with db.cursor() as cur:
                db.heartbeat(cur, status="ok", records_updated=0, **HEARTBEAT)
        return rep
    sel = select_candidates(games, odds, preds, pricing, now)
    rep.pricing_missing, rep.skipped_not_open, rep.invalid_snapshots = (sel.pricing_missing, sel.skipped_not_open,
                                                                         sel.invalid_snapshots)
    results = engine.size_portfolio(sel.candidates, as_of=now, policy=policy, incomplete_days=sel.incomplete_days)
    _summarize(rep, results, bankroll, keep=dry_run or bankroll is not None)
    if dry_run:
        return rep
    with db.transaction() as cur:
        rep.rows_inserted, rep.rows_existing = persist(cur, results)
    with db.cursor() as cur:
        status = "warn" if sel.pricing_missing else "ok"
        err = f"{len(sel.pricing_missing)} 個可下注盤口沒有對應定價（該 betting day 不給 actionable）" \
            if sel.pricing_missing else None
        db.heartbeat(cur, status=status, error=err, records_updated=rep.rows_inserted, **HEARTBEAT)
    log.info("sizing：比賽 %d、outcome %d %s、actionable %d、新增 %d 列、既有 %d 列", rep.games, rep.candidates,
             rep.by_status, rep.actionable, rep.rows_inserted, rep.rows_existing)
    return rep


@dataclass(frozen=True)
class PricingGate:
    """scheduler-efficiency-v1：定價 / sizing 要不要做（只用 3 個便宜的索引查詢）。"""
    run_pricing: bool
    run_sizing: bool
    reason: str                       # active / no_games_in_window / no_odds_snapshots / no_fresh_inputs
    key: tuple | None = None


_LAST_PRICED_KEY: tuple | None = None


def pricing_gate(cur, now: datetime, last_key: tuple | None, *, horizon: timedelta = HORIZON) -> PricingGate:
    """
    * 48 小時內沒有 eligible 比賽 → 兩者都沒事做（pricing_job / sizing_job 本來就會回報「沒有比賽」）。
    * 有比賽但沒有任何盤口快照 → 沒有東西可定價、也沒有 sizing 候選（兩者的輸出本來就是空的）。
    * 定價輸入 key = (比賽集合, 這些比賽的最大 odds_snapshots.id, 最大 predictions.id)：與上次**成功**定價相同 →
      pricing 的輸出已全部存在（寫入是 ON CONFLICT DO NOTHING、既有列永不改寫、輸入決定輸出），略過重建。
    * sizing 不用 key 略過：報價新鮮度（stale_quote）隨時間改變，同樣輸入在不同 T 可能得到不同結果，所以有盤口就照舊每 5 分鐘算。
    """
    cur.execute("SELECT id FROM games WHERE date_utc > %s AND date_utc <= %s AND status <> 'final' ORDER BY id",
                (now, now + horizon))      # 與 pricing_job 完全相同的比賽集合（不收窄、不擴大）
    ids = [r["id"] for r in cur.fetchall()]
    if not ids:
        return PricingGate(False, False, "no_games_in_window")
    cur.execute("SELECT (SELECT COALESCE(MAX(id), 0) FROM odds_snapshots WHERE game_id = ANY(%s) AND fetched_at <= %s) AS o, "
                "(SELECT COALESCE(MAX(id), 0) FROM predictions WHERE game_id = ANY(%s) AND created_at <= %s) AS p",
                (ids, now, ids, now))
    r = cur.fetchone()
    if not r["o"]:
        return PricingGate(False, False, "no_odds_snapshots")
    key = (tuple(ids), int(r["o"]), int(r["p"]))
    if key == last_key:
        return PricingGate(False, True, "no_fresh_inputs", key)
    return PricingGate(True, True, "active", key)


def pricing_and_sizing_scheduled() -> None:
    """排程進入點：同一個 T 先定價（D.2）再 sizing（D.3）——sizing 讀到的就是同一輪的定價列。
    scheduler-efficiency-v1：先用 pricing_gate 判斷有沒有事；idle 只更新兩個心跳（既有列的 UPSERT），不重建定價。"""
    from .. import db
    from ..pricing.job import HEARTBEAT as PRICING_HEARTBEAT
    from ..pricing.job import pricing_job
    from ..production import activity

    global _LAST_PRICED_KEY
    now = now_utc()
    with db.cursor() as cur:
        gate = pricing_gate(cur, now, _LAST_PRICED_KEY)
    if not gate.run_pricing and not gate.run_sizing:
        activity.log_skip("pricing", gate.reason)
        with db.cursor() as cur:
            db.heartbeat(cur, status="ok", records_updated=0, **PRICING_HEARTBEAT)
            db.heartbeat(cur, status="ok", records_updated=0, **HEARTBEAT)
        return
    if gate.run_pricing:
        rep = pricing_job(now)
        _LAST_PRICED_KEY = gate.key if not rep.errors else None      # 失敗（例如 artifact 載入）→ 下一輪重試
    else:
        activity.log_skip("pricing", gate.reason)
        with db.cursor() as cur:
            activity.touch_heartbeat(cur, **PRICING_HEARTBEAT)      # 保留上次的 ok / warn 狀態，只更新「有在跑」
    sizing_job(now)
