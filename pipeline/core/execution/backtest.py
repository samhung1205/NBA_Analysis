"""
Historical strategy backtest（只用 DB 中真實觀測的 snapshot；不造盤口、不用現在的盤口回填過去）
------------------------------------------------------------
    run_strategy_backtest(scope, date_from, date_to)

流程：讀取期間內的比賽（Asia/Taipei betting day）→ 只保留 observed odds 列（seed / synthetic / unverified 排除並計數）
→ 若沒有任何 observed 列：historical_evidence_available = false、不計算任何 ROI / yield（不是 0）
→ 否則 engine.simulate（與 fixture validation、prospective ledger 同一套 as-of / D.2 / D.3 / controller / settlement）
→ metrics、day-block bootstrap、descriptive subgroups、evidence 標記。

台彩 = source twsport；--source oddsapi --bookmaker <key> 只產生 international_market_diagnostic（永不標為台彩 ROI）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..pricing import engine as pricing_engine
from ..sizing.policy import PRODUCTION_RISK_POLICY, RiskPolicy
from ..timeutil import ensure_utc, tpe_day_bounds_utc
from . import asof, evidence, metrics
from .engine import simulate
from .ledger import display_amounts
from .policy import EXECUTION_V1, ExecutionPolicy, StrategyScope, strategy_id


def load_period(cur, scope: StrategyScope, date_from: date, date_to: date) -> dict[str, list[dict]]:
    start, _ = tpe_day_bounds_utc(date_from)
    _, end = tpe_day_bounds_utc(date_to)
    cur.execute("SELECT * FROM games WHERE date_utc >= %s AND date_utc < %s ORDER BY date_utc, id", (start, end))
    games = cur.fetchall()
    if not games:
        return {"games": [], "odds": [], "preds": [], "runs": [], "openers": []}
    ids = [g["id"] for g in games]
    last_t = max(ensure_utc(g["date_utc"]) for g in games)
    cur.execute("""SELECT * FROM odds_snapshots WHERE game_id = ANY(%s) AND source = %s
                     AND COALESCE(bookmaker, source) = %s AND fetched_at <= %s ORDER BY id""",
                (ids, scope.source, scope.bookmaker, last_t))
    odds = cur.fetchall()
    cur.execute("""SELECT id, game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                          pred_home_h1, pred_away_h1, features_json
                     FROM predictions WHERE game_id = ANY(%s) AND created_at <= %s ORDER BY id""", (ids, last_t))
    preds = cur.fetchall()
    run_ids = sorted({r["fetch_run_id"] for r in odds if r.get("fetch_run_id") is not None})
    cur.execute("""SELECT id, source, fetched_at, outcome, diagnostics FROM odds_fetch_runs
                    WHERE source = %s AND (id = ANY(%s) OR (fetched_at >= %s AND fetched_at <= %s)) ORDER BY id""",
                (scope.source, run_ids, start, last_t))
    runs = cur.fetchall()
    cur.execute("SELECT season, MIN(date_utc) AS opener FROM games WHERE season = ANY(%s) "
                "AND season_stage = 'regular' GROUP BY season", (sorted({g["season"] for g in games}),))
    return {"games": games, "odds": odds, "preds": preds, "runs": runs, "openers": cur.fetchall()}


def run_strategy_backtest(scope: StrategyScope, date_from: date, date_to: date, *, starting_bankroll: float = 1.0,
                          policy: ExecutionPolicy = EXECUTION_V1, risk: RiskPolicy = PRODUCTION_RISK_POLICY,
                          root: str | Path | None = None, db=None, bootstrap_seed: int = metrics.BOOTSTRAP_SEED
                          ) -> dict[str, Any]:
    from .. import db as default_db

    db = db or default_db
    if date_to < date_from:
        raise ValueError("date_to < date_from")
    with db.cursor() as cur:
        data = load_period(cur, scope, date_from, date_to)
    observed, rejected = evidence.partition_odds_rows(data["odds"], data["runs"])
    preds = [p for p in data["preds"] if evidence.prediction_is_production(p)]
    out: dict[str, Any] = {
        "strategy_id": strategy_id(policy, risk, scope), "execution_policy": policy.to_dict(),
        "risk_policy": risk.to_dict(), "period": {"from": date_from.isoformat(), "to": date_to.isoformat(),
                                                  "betting_day_timezone": policy.betting_day_timezone},
        "n_games_in_period": len(data["games"]), "n_odds_rows_loaded": len(data["odds"]),
        "n_predictions_loaded": len(data["preds"]), "n_production_predictions": len(preds)}
    if not observed:
        out["evidence"] = evidence.evidence_summary(scope=scope, validation_only=False, n_observed_rows=0,
                                                    rejected_rows=rejected)
        out["metrics"] = None
        return out
    sim = simulate(data["games"], observed, preds, data["runs"], scope=scope,
                   model_for=pricing_engine.ArtifactCache(root).provider,
                   artifacts=asof.LocalArtifactRegistry(root), policy=policy, risk=risk)
    used = {i for d in sim.decisions for i in d.odds_snapshot_ids}
    used |= {w.odds_snapshot_id for w in sim.wagers}
    out["evidence"] = evidence.evidence_summary(scope=scope, validation_only=False, n_observed_rows=len(observed),
                                                rejected_rows=rejected, used_snapshot_ids=used,
                                                observed_ids=[int(r["id"]) for r in observed])
    assert all(w.source == scope.source and w.bookmaker == scope.bookmaker for w in sim.wagers)
    opener = {r["season"]: asof.betting_day(r["opener"]) for r in data["openers"]}
    season_opener = {int(g["id"]): opener[g["season"]] for g in data["games"] if g["season"] in opener}
    m = metrics.strategy_metrics(sim.decisions, sim.days, sim.starting_bankroll)
    out.update(metrics=m, display=display_amounts(m, starting_bankroll),
               bootstrap=metrics.day_block_bootstrap(sim.days, sim.decisions, seed=bootstrap_seed),
               subgroups=metrics.subgroup_report(sim.wagers, season_opener=season_opener),
               days=[d.to_dict() for d in sim.days], decisions=[d.to_dict() for d in sim.decisions])
    return out
