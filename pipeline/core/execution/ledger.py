"""
Prospective paper-decision ledger（DB；migration 0007）+ 排程 job
------------------------------------------------------------
    paper_decision_job(now)      已到 decision_time T（= 開賽 − 60 分）、尚未有 decision 的比賽 → 以 **T** 為 analysis_as_of
                                 重建（不是 job 實際執行時間）→ D.3 sizing → execution-v1 controller → 寫入 decision（含 no_bet）+ paper bets
    paper_settlement_job(now)    pending / ungradable 的 paper bets → settle-v1（只有 final 才結算；結算完成後不可改）
    load_ledger(sid)             讀回 decisions / bets → 與歷史引擎相同的資料結構（metrics 共用）

規則：
  * 一個 strategy × 一場比賽只有一筆 decision（UNIQUE + advisory lock + 不可變 trigger）；T 之後出現更好的賠率 / 預測不會重做。
  * job 晚於 T 執行：仍只用 T 以前的資料（fetched_at ≤ T、available_at ≤ T、T 以前的輪詢確認）；
    晚超過 max_evaluation_lag（15 分）→ 記錄 no_bet decision_window_missed（不事後補做、不挑結果）。
  * day_start_bankroll：該 betting day 第一筆 decision 的 T 時點可用 bankroll（起始 + T 以前已結算損益 − 仍未結算 stake），寫入後凍結。
  * 不寫入 bets（使用者真正下注紀錄）、不寫 market_pricing_snapshots / bet_sizing_snapshots（那些是 D.2 / D.3 job 的 cache）。
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..pricing import engine as pricing_engine
from ..sizing.policy import PRODUCTION_RISK_POLICY, SIZING_VERSION, RiskPolicy
from ..sizing import kelly
from ..timeutil import ensure_utc, now_utc, parse_utc
from . import asof, evidence, metrics, settlement
from .engine import DayState, GameDecision, Wager, day_ledgers, execute_batch, missed_decision
from .policy import (EXECUTION_V1, PRIMARY_SCOPE, SETTLEMENT_VERSION, ExecutionPolicy, StrategyScope,
                     assert_registered, strategy_id)

log = logging.getLogger(__name__)

HEARTBEAT = dict(source_key="paper_strategy", display_name="Paper strategy ledger（D.4 execution-v1）", category="model",
                 expected_interval_min=5)
TRACKING_START_UTC = datetime(2026, 10, 1, tzinfo=timezone.utc)   # 2026-27 prospective tracking 起點（之前的比賽不補記）
LOOKBACK = timedelta(hours=48)                                       # 排程中斷後最多回溯記錄 48 小時（missed decision）
PROSPECTIVE_SCOPES: tuple[StrategyScope, ...] = (PRIMARY_SCOPE,)

DECISION_COLUMNS = (
    "strategy_id", "execution_policy_version", "risk_policy_version", "sizing_version", "pricing_version",
    "kelly_math_version", "strategy_scope", "evidence_label", "source", "bookmaker", "game_id", "betting_day",
    "scheduled_tipoff", "decision_time", "evaluated_at", "evaluation_lag_seconds", "decision_status", "no_bet_reason",
    "blockers", "odds_snapshot_ids", "prediction_id", "prediction_available_at", "artifact_version", "model_version",
    "distribution_version", "pricing_fingerprint", "sizing_fingerprint", "sizing", "day_start_bankroll",
    "committed_fraction_before", "remaining_day_fraction_before", "execution_scale_factor", "total_stake_fraction",
    "total_stake_units")
DECISION_JSON = ("blockers", "odds_snapshot_ids", "sizing")
BET_COLUMNS = (
    "decision_id", "strategy_id", "game_id", "betting_day", "decision_time", "scheduled_tipoff", "odds_snapshot_id",
    "prediction_id", "source", "bookmaker", "market", "market_type", "period", "outcome_set", "side", "line",
    "display_line", "model_target", "model_threshold", "comparator", "settlement_rule", "decimal_odds", "p_win",
    "p_push", "p_loss", "ev_per_unit", "sizing_final_stake_fraction", "execution_scale_factor", "stake_fraction",
    "stake_units", "expected_profit_units")


@dataclass
class PaperReport:
    as_of: str
    dry_run: bool
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    settled: int = 0
    still_open: int = 0
    note: str | None = None


def schema_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('paper_strategy_decisions') IS NOT NULL AS d, "
                "to_regclass('paper_strategy_bets') IS NOT NULL AS b, to_regclass('paper_strategy_days') IS NOT NULL AS y, "
                "to_regclass('odds_fetch_runs') IS NOT NULL AS r")
    r = cur.fetchone()
    return bool(r["d"] and r["b"] and r["y"] and r["r"])


# ------------------------------------------------------------------ #
# 讀取                                                                  #
# ------------------------------------------------------------------ #

def due_games(cur, sid: str, now: datetime, policy: ExecutionPolicy) -> list[dict]:
    """decision_time ≤ now、在追蹤視窗內、此 strategy 尚未有 decision 的比賽。"""
    lo = max(TRACKING_START_UTC, now - LOOKBACK)
    off = policy.decision_offset
    cur.execute("""SELECT g.id, g.date_utc, g.status FROM games g
                    WHERE g.date_utc - %s <= %s AND g.date_utc - %s >= %s
                      AND NOT EXISTS (SELECT 1 FROM paper_strategy_decisions d
                                       WHERE d.strategy_id = %s AND d.game_id = g.id)
                    ORDER BY g.date_utc, g.id""", (off, now, off, lo, sid))
    return cur.fetchall()


def load_asof_inputs(cur, game_ids: list[int], scope: StrategyScope, until: datetime
                     ) -> tuple[list[dict], list[dict], list[dict]]:
    """SQL 先以 until（批次的 T）過濾；reconstruct_game 再逐場以 T 過濾一次（純函式、有測試）。"""
    cur.execute("""SELECT * FROM odds_snapshots WHERE game_id = ANY(%s) AND source = %s
                     AND COALESCE(bookmaker, source) = %s AND fetched_at <= %s ORDER BY id""",
                (game_ids, scope.source, scope.bookmaker, until))
    odds = cur.fetchall()
    cur.execute("""SELECT id, game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                          pred_home_h1, pred_away_h1, features_json
                     FROM predictions WHERE game_id = ANY(%s) AND created_at <= %s ORDER BY id""", (game_ids, until))
    preds = cur.fetchall()
    cur.execute("""SELECT id, source, fetched_at, outcome, diagnostics FROM odds_fetch_runs
                    WHERE source = %s AND fetched_at <= %s AND fetched_at >= %s ORDER BY id""",
                (scope.source, until, until - timedelta(days=3)))
    return odds, preds, cur.fetchall()


def _prior_bets(cur, sid: str, day: date) -> list[dict]:
    cur.execute("SELECT * FROM paper_strategy_bets WHERE strategy_id = %s AND betting_day < %s ORDER BY id", (sid, day))
    return cur.fetchall()


def wager_from_row(r: dict) -> Wager:
    w = Wager(**{k: r[k] for k in (
        "strategy_id", "game_id", "betting_day", "decision_time", "scheduled_tipoff", "odds_snapshot_id",
        "prediction_id", "source", "bookmaker", "market", "market_type", "period", "outcome_set", "side", "line",
        "display_line", "model_target", "model_threshold", "comparator", "settlement_rule", "decimal_odds", "p_win",
        "p_push", "p_loss", "ev_per_unit", "sizing_final_stake_fraction", "execution_scale_factor", "stake_fraction",
        "stake_units", "expected_profit_units", "settlement_status", "settlement_reason", "actual_value",
        "home_score", "away_score", "profit_units")})
    w.resolved_at = r.get("settled_at") if r["settlement_status"] in settlement.TERMINAL else None
    if isinstance(w.betting_day, str):
        w.betting_day = date.fromisoformat(w.betting_day)
    return w


def day_state(cur, sid: str, day: date, first_t: datetime, game_id: int, *, policy: ExecutionPolicy,
              risk: RiskPolicy, dry_run: bool) -> DayState:
    """既有 day row → 沿用（凍結）；否則以 first_t 時點可用 bankroll 建立。committed = 當日已寫入的 stake 比例。"""
    cur.execute("SELECT * FROM paper_strategy_days WHERE strategy_id = %s AND betting_day = %s", (sid, day))
    row = cur.fetchone()
    if row is None:
        prior = [wager_from_row(r) for r in _prior_bets(cur, sid, day)]
        first_t = ensure_utc(first_t)
        known = [w.resolved and w.resolved_at is not None and ensure_utc(w.resolved_at) <= first_t for w in prior]
        done = [w for w, k in zip(prior, known) if k]
        out = [w for w, k in zip(prior, known) if not k]
        pnl, outstanding = math.fsum(w.profit_units for w in done), math.fsum(w.stake_units for w in out)
        start = policy.starting_bankroll_units + pnl - outstanding
        if not dry_run:
            cur.execute("""INSERT INTO paper_strategy_days (strategy_id, betting_day, day_start_bankroll, established_at,
                             established_by_game_id, starting_bankroll_units, prior_resolved_profit, prior_unresolved_stake)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (sid, day, start, first_t, game_id, policy.starting_bankroll_units, pnl, outstanding))
        return DayState(day, start)
    cur.execute("SELECT COALESCE(SUM(stake_fraction), 0) AS c FROM paper_strategy_bets "
                "WHERE strategy_id = %s AND betting_day = %s", (sid, day))
    return DayState(day, float(row["day_start_bankroll"]), float(cur.fetchone()["c"]))


# ------------------------------------------------------------------ #
# 寫入                                                                  #
# ------------------------------------------------------------------ #

def _decision_values(d: GameDecision, scope: StrategyScope, policy: ExecutionPolicy, risk: RiskPolicy) -> dict:
    lag = (ensure_utc(d.evaluated_at) - ensure_utc(d.decision_time)).total_seconds()
    v = d.to_dict(with_wagers=False)
    v.update(execution_policy_version=policy.version, risk_policy_version=risk.version, sizing_version=SIZING_VERSION,
             pricing_version=pricing_engine.PRICING_VERSION, kelly_math_version=kelly.KELLY_MATH_VERSION,
             strategy_scope=scope.kind, evidence_label=scope.evidence_label, source=scope.source,
             bookmaker=scope.bookmaker, evaluation_lag_seconds=lag)
    return v


def persist_decision(cur, d: GameDecision, *, scope: StrategyScope, policy: ExecutionPolicy, risk: RiskPolicy
                     ) -> int | None:
    v = _decision_values(d, scope, policy, risk)
    cols = ", ".join(DECISION_COLUMNS)
    ph = ", ".join("%s::jsonb" if c in DECISION_JSON else "%s" for c in DECISION_COLUMNS)
    vals = tuple(json.dumps(v[c], ensure_ascii=False, allow_nan=False, default=str) if c in DECISION_JSON else v[c]
                 for c in DECISION_COLUMNS)
    cur.execute(f"INSERT INTO paper_strategy_decisions ({cols}) VALUES ({ph}) "
                "ON CONFLICT (strategy_id, game_id) DO NOTHING RETURNING id", vals)
    r = cur.fetchone()
    if r is None:
        return None
    did = int(r["id"])
    for w in d.wagers:
        wv = w.to_dict()
        wv["decision_id"] = did
        cur.execute(f"INSERT INTO paper_strategy_bets ({', '.join(BET_COLUMNS)}) VALUES "
                    f"({', '.join(['%s'] * len(BET_COLUMNS))})", tuple(wv[c] for c in BET_COLUMNS))
    return did


# ------------------------------------------------------------------ #
# Jobs                                                                 #
# ------------------------------------------------------------------ #

def _decide_scope(cur, scope: StrategyScope, now: datetime, *, policy: ExecutionPolicy, risk: RiskPolicy, root,
                  dry_run: bool, rep: PaperReport) -> None:
    sid = strategy_id(policy, risk, scope)
    stats = rep.strategies.setdefault(sid, {"scope": scope.to_dict(), "decided": 0, "bets": 0, "no_bet": {},
                                            "already_decided": 0, "missed": 0})
    if not dry_run:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"paper_strategy:{sid}",))
    games = due_games(cur, sid, now, policy)
    by_day: dict[date, list[dict]] = {}
    for g in games:
        by_day.setdefault(asof.betting_day(g["date_utc"]), []).append(g)
    arts = pricing_engine.ArtifactCache(root)
    registry = asof.LocalArtifactRegistry(root)
    for day in sorted(by_day):
        batches: dict[datetime, list[dict]] = {}
        for g in by_day[day]:
            batches.setdefault(asof.decision_time(g["date_utc"], policy), []).append(g)
        first_t = min(batches)
        first_g = min(batches[first_t], key=lambda g: int(g["id"]))
        state = day_state(cur, sid, day, first_t, int(first_g["id"]), policy=policy, risk=risk, dry_run=dry_run)
        for t in sorted(batches):
            batch = sorted(batches[t], key=lambda g: int(g["id"]))
            if now - t > policy.max_evaluation_lag:
                decisions = [missed_decision(g, sid=sid, scope=scope, day=state, policy=policy, risk=risk,
                                             evaluated_at=now) for g in batch]
                stats["missed"] += len(decisions)
            else:
                ids = [int(g["id"]) for g in batch]
                odds, preds, runs = load_asof_inputs(cur, ids, scope, t)
                recons = [asof.reconstruct_game(g, t, scope=scope, odds_rows=odds, pred_rows=preds, runs=runs,
                                                risk=risk, policy=policy, model_for=arts.provider, artifacts=registry)
                          for g in batch]
                decisions = execute_batch(recons, t, state, sid=sid, risk=risk, evaluated_at=now)
            for d in decisions:
                if not dry_run and persist_decision(cur, d, scope=scope, policy=policy, risk=risk) is None:
                    stats["already_decided"] += 1
                    continue
                stats["decided"] += 1
                stats["bets"] += len(d.wagers)
                if d.no_bet_reason:
                    stats["no_bet"][d.no_bet_reason] = stats["no_bet"].get(d.no_bet_reason, 0) + 1
                rep.decisions.append(d.to_dict())


def paper_decision_job(now: datetime | None = None, *, scopes: Iterable[StrategyScope] = PROSPECTIVE_SCOPES,
                       policy: ExecutionPolicy = EXECUTION_V1, risk: RiskPolicy = PRODUCTION_RISK_POLICY,
                       dry_run: bool = False, root: str | Path | None = None, db=None) -> PaperReport:
    from .. import db as default_db

    db = db or default_db
    if not dry_run:
        assert_registered(policy, risk)
    now = ensure_utc(now or now_utc())
    rep = PaperReport(now.isoformat(), dry_run)
    with db.cursor() as cur:
        if not schema_ready(cur):
            rep.note = "資料庫尚未套用 migrations/postgres/0007_phase_d4.sql，不記錄 paper decision"
            if not dry_run:
                db.heartbeat(cur, status="warn", error=rep.note, **HEARTBEAT)
            return rep
    for scope in scopes:
        if dry_run:
            with db.cursor() as cur:
                _decide_scope(cur, scope, now, policy=policy, risk=risk, root=root, dry_run=True, rep=rep)
        else:
            with db.transaction() as cur:
                _decide_scope(cur, scope, now, policy=policy, risk=risk, root=root, dry_run=False, rep=rep)
    return rep


def paper_settlement_job(now: datetime | None = None, *, policy: ExecutionPolicy = EXECUTION_V1, dry_run: bool = False,
                         db=None, rep: PaperReport | None = None) -> PaperReport:
    from .. import db as default_db

    db = db or default_db
    now = ensure_utc(now or now_utc())
    rep = rep or PaperReport(now.isoformat(), dry_run)
    with db.cursor() as cur:
        if not schema_ready(cur):
            rep.note = rep.note or "資料庫尚未套用 0007"
            return rep
        cur.execute("""SELECT b.*, d.scheduled_tipoff AS decision_tipoff FROM paper_strategy_bets b
                         JOIN paper_strategy_decisions d ON d.id = b.decision_id
                        WHERE b.settlement_status IN ('pending', 'ungradable') ORDER BY b.id""")
        open_bets = cur.fetchall()
        if not open_bets:
            return rep
        cur.execute("SELECT * FROM games WHERE id = ANY(%s)", (sorted({b["game_id"] for b in open_bets}),))
        games = {g["id"]: g for g in cur.fetchall()}
    updates = []
    for b in open_bets:
        s = settlement.settle(b, games[b["game_id"]], decision_scheduled_tipoff=b["decision_tipoff"], policy=policy)
        if s.terminal:
            rep.settled += 1
        else:
            rep.still_open += 1
        if (s.status, s.reason) != (b["settlement_status"], b["settlement_reason"]):
            updates.append((b["id"], s))
    if dry_run or not updates:
        return rep
    with db.transaction() as cur:
        for bid, s in updates:
            cur.execute("""UPDATE paper_strategy_bets SET settlement_status = %s, settlement_reason = %s,
                             settlement_version = %s, actual_value = %s, home_score = %s, away_score = %s,
                             profit_units = %s, settled_at = %s
                            WHERE id = %s AND settlement_status IN ('pending', 'ungradable')""",
                        (s.status, s.reason, SETTLEMENT_VERSION, s.actual_value, s.home_score, s.away_score,
                         s.profit_units, now if s.terminal else None, bid))
    return rep


def paper_has_work(cur, now: datetime, *, scopes: Iterable[StrategyScope] = PROSPECTIVE_SCOPES,
                   policy: ExecutionPolicy = EXECUTION_V1, risk: RiskPolicy = PRODUCTION_RISK_POLICY) -> bool:
    """scheduler-efficiency-v1：有 T-60 decision 候選（與 _decide_scope 同一個 due_games 條件）或未結算的 paper bet 才需要跑。
    兩個條件任一成立 → 完整 job（execution-v1 語意完全不變；>15 分鐘的 decision_window_missed 仍由 job 內判斷）。"""
    for scope in scopes:
        if due_games(cur, strategy_id(policy, risk, scope), now, policy):
            return True
    cur.execute("SELECT 1 FROM paper_strategy_bets WHERE settlement_status IN ('pending', 'ungradable') LIMIT 1")
    return cur.fetchone() is not None


def paper_strategy_scheduled() -> None:
    """排程進入點：先記錄到期的 T-60 decision，再結算；兩者各自隔離（決策失敗不影響結算）。心跳 data_sources.paper_strategy。"""
    from .. import db
    from ..production import activity

    now = now_utc()
    try:
        with db.cursor() as cur:
            work = paper_has_work(cur, now)
    except Exception:  # noqa: BLE001 — 查詢失敗（例如 migration 未套用）→ 照舊跑完整 job，由它們回報原因
        work = True
    if not work:
        activity.log_skip("paper", "no_candidate_or_open_bet")
        with db.cursor() as cur:
            db.heartbeat(cur, status="ok", records_updated=0, **HEARTBEAT)
        return
    status, err, n = "ok", None, 0
    try:
        rep = paper_decision_job(now)
        n = sum(s["decided"] for s in rep.strategies.values())
        if rep.note:
            status, err = "warn", rep.note
    except Exception as e:  # noqa: BLE001
        log.exception("paper decision job 失敗")
        status, err = "error", f"decision: {type(e).__name__}: {e}"[:500]
    try:
        paper_settlement_job(now)
    except Exception as e:  # noqa: BLE001
        log.exception("paper settlement job 失敗")
        status, err = "error", ((err + "; ") if err else "") + f"settlement: {type(e).__name__}: {e}"[:300]
    with db.cursor() as cur:
        db.heartbeat(cur, status=status, error=err, records_updated=n, **HEARTBEAT)


# ------------------------------------------------------------------ #
# 讀回 ledger → metrics（prospective performance tracking）               #
# ------------------------------------------------------------------ #

def load_ledger(cur, sid: str) -> list[GameDecision]:
    cur.execute("SELECT * FROM paper_strategy_decisions WHERE strategy_id = %s ORDER BY decision_time, game_id", (sid,))
    drows = cur.fetchall()
    cur.execute("SELECT * FROM paper_strategy_bets WHERE strategy_id = %s ORDER BY id", (sid,))
    bets: dict[int, list[Wager]] = {}
    for r in cur.fetchall():
        bets.setdefault(int(r["decision_id"]), []).append(wager_from_row(r))
    out = []
    for r in drows:
        j = lambda k, fb: r[k] if not isinstance(r[k], str) else json.loads(r[k])  # noqa: E731
        out.append(GameDecision(
            strategy_id=r["strategy_id"], game_id=r["game_id"], betting_day=r["betting_day"],
            scheduled_tipoff=r["scheduled_tipoff"], decision_time=r["decision_time"], evaluated_at=r["evaluated_at"],
            decision_status=r["decision_status"], no_bet_reason=r["no_bet_reason"], blockers=j("blockers", {}),
            odds_snapshot_ids=j("odds_snapshot_ids", []), prediction_id=r["prediction_id"],
            prediction_available_at=r["prediction_available_at"], artifact_version=r["artifact_version"],
            model_version=r["model_version"], distribution_version=r["distribution_version"],
            pricing_fingerprint=r["pricing_fingerprint"], sizing_fingerprint=r["sizing_fingerprint"],
            sizing=j("sizing", []), day_start_bankroll=r["day_start_bankroll"],
            committed_fraction_before=r["committed_fraction_before"],
            remaining_day_fraction_before=r["remaining_day_fraction_before"],
            execution_scale_factor=r["execution_scale_factor"], total_stake_fraction=r["total_stake_fraction"],
            total_stake_units=r["total_stake_units"], wagers=bets.get(int(r["id"]), [])))
    return out


def paper_performance(scope: StrategyScope = PRIMARY_SCOPE, *, policy: ExecutionPolicy = EXECUTION_V1,
                      risk: RiskPolicy = PRODUCTION_RISK_POLICY, starting_bankroll: float = 1.0, db=None
                      ) -> dict[str, Any]:
    from .. import db as default_db

    db = db or default_db
    sid = strategy_id(policy, risk, scope)
    with db.cursor() as cur:
        if not schema_ready(cur):
            return {"strategy_id": sid, "note": "0007 未套用"}
        decisions = load_ledger(cur, sid)
    days = day_ledgers(decisions)
    m = metrics.strategy_metrics(decisions, days, policy.starting_bankroll_units)
    return {"strategy_id": sid, "evidence": evidence.evidence_summary(scope=scope, validation_only=False,
                                                                      prospective=True),
            "metrics": m, "display": display_amounts(m, starting_bankroll),
            "bootstrap": metrics.day_block_bootstrap(days, decisions),
            "subgroups": metrics.subgroup_report([w for d in decisions for w in d.wagers]),
            "days": [d.to_dict() for d in days]}


def display_amounts(m: dict[str, Any], starting_bankroll: float) -> dict[str, Any]:
    """normalized 單位 × 顯示用起始 bankroll（不影響任何選擇邏輯；只是比例換算）。"""
    k = float(starting_bankroll) / float(m["starting_bankroll_units"])
    keys = ("starting_bankroll_units", "ending_bankroll_units", "net_profit_units", "total_staked_units",
            "unresolved_stake_units", "expected_profit_units", "realized_profit_units", "max_drawdown_units")
    return {"starting_bankroll": float(starting_bankroll),
            **{k_.replace("_units", ""): (m[k_] * k if m.get(k_) is not None else None) for k_ in keys}}
