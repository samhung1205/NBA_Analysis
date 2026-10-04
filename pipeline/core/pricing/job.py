"""
Phase D.2 定價工作（DB 讀寫；數學全部在 engine / novig）
------------------------------------------------------------
    pricing_job(now)               未開賽、未來 48 小時的比賽：每個 series 最新 snapshot × 當時最新有效預測 → 定價 → 寫入
    reconstruct_game(game_id, T)   只用 T 以前存在的 snapshot / prediction / artifact 重建（D.4 用；不寫入）

寫入 market_pricing_snapshots：唯一鍵 (odds_snapshot_id, COALESCE(prediction_id, 0), side, pricing_version)，
ON CONFLICT DO NOTHING → 重跑冪等；同一組輸入的輸出是決定性的（artifact 版本目錄不可變），
已存在的列永不改寫。最新 snapshot 不是 open → 不寫（API 也不顯示）；模型機率取不到（artifact 載入失敗）→
該場不寫（不把暫時性錯誤永久存成 market_only），回報 error。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..timeutil import ensure_utc, now_utc
from . import engine
from .alignment import latest_snapshots_as_of, odds_input_from_row, select_prediction

log = logging.getLogger(__name__)

HORIZON = timedelta(hours=48)          # 與 Odds API 抓取視窗一致
HEARTBEAT = dict(source_key="market_pricing", display_name="盤口定價（D.2 去水 / edge / EV）", category="model",
                 expected_interval_min=5)

COLUMNS = (
    "pricing_version", "no_vig_method", "odds_snapshot_id", "prediction_id", "game_id", "analysis_as_of",
    "odds_fetched_at", "prediction_created_at", "prediction_kind", "prediction_profile", "model_version",
    "artifact_version", "distribution_version", "source", "bookmaker", "market", "market_type", "period",
    "outcome_set", "line", "away_line", "status", "status_reason", "settlement_rule", "total_raw_implied",
    "market_overround", "fair_prob_sum", "side", "display_line", "model_target", "model_threshold", "comparator",
    "decimal_odds", "raw_implied_prob", "fair_no_vig_prob", "model_prob", "push_prob", "loss_prob", "edge_vs_fair",
    "ev_per_unit", "expected_return", "ev_percent", "warnings", "diagnostics")


@dataclass
class PricingReport:
    as_of: str
    games: int = 0
    markets: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    rows_inserted: int = 0
    rows_existing: int = 0
    skipped_not_open: int = 0
    invalid_snapshots: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    pricings: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None


def schema_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('market_pricing_snapshots') IS NOT NULL AS ok")
    return bool(cur.fetchone()["ok"])


def load_rows(cur, game_ids: list[int], as_of: datetime) -> tuple[list[dict], list[dict]]:
    """SQL 先以 as_of 過濾（之後的列根本不讀進來），alignment 再做一次（純函式、有測試）。"""
    cur.execute("SELECT * FROM odds_snapshots WHERE game_id = ANY(%s) AND fetched_at <= %s ORDER BY id",
                (game_ids, as_of))
    odds = cur.fetchall()
    cur.execute("""SELECT id, game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                          pred_home_h1, pred_away_h1, features_json
                     FROM predictions WHERE game_id = ANY(%s) AND created_at <= %s ORDER BY id""",
                (game_ids, as_of))
    return odds, cur.fetchall()


def _db_value(k: str, v: Any) -> Any:
    if k in ("warnings", "diagnostics"):
        return json.dumps(v, ensure_ascii=False, allow_nan=False)
    return v


def persist(cur, pricings: list[engine.MarketPricing]) -> tuple[int, int]:
    inserted = existing = 0
    cols = ", ".join(COLUMNS)
    ph = ", ".join("%s::jsonb" if c in ("warnings", "diagnostics") else "%s" for c in COLUMNS)
    for mp in pricings:
        for row in mp.to_rows():
            cur.execute(f"INSERT INTO market_pricing_snapshots ({cols}) VALUES ({ph}) ON CONFLICT DO NOTHING",
                        tuple(_db_value(c, row[c]) for c in COLUMNS))
            if cur.rowcount == 1:
                inserted += 1
            else:
                existing += 1
    return inserted, existing


def price_games(odds_rows: list[dict], pred_rows: list[dict], game_ids: list[int], as_of: datetime,
                arts: engine.ArtifactCache, rep: PricingReport, *, include_not_open: bool = False
                ) -> list[engine.MarketPricing]:
    out: list[engine.MarketPricing] = []
    for gid in game_ids:
        pred = select_prediction(pred_rows, gid, as_of)
        try:
            model = arts.provider(pred) if pred is not None else None
            priced = []
            for r in latest_snapshots_as_of([r for r in odds_rows if int(r["game_id"]) == gid], as_of):
                oi = odds_input_from_row(r)
                if oi.snapshot is None:
                    rep.invalid_snapshots.append({"odds_snapshot_id": oi.snapshot_id, "reason": oi.invalid_reason})
                    continue
                if oi.snapshot.status != "open" and not include_not_open:
                    rep.skipped_not_open += 1
                    continue
                priced.append(engine.price_market(oi, pred, model, as_of=as_of))
        except engine.ModelProbabilityError as e:
            rep.errors.append({"game_id": gid, "prediction_id": pred.prediction_id if pred else None,
                               "error": str(e)[:300]})
            continue
        out.extend(priced)
    for mp in out:
        rep.by_status[mp.status] = rep.by_status.get(mp.status, 0) + 1
    rep.markets += len(out)
    return out


def pricing_job(now: datetime | None = None, *, horizon: timedelta = HORIZON, dry_run: bool = False,
                root: str | Path | None = None, db=None) -> PricingReport:
    from .. import db as default_db

    db = db or default_db
    now = ensure_utc(now or now_utc())
    rep = PricingReport(now.isoformat())
    with db.cursor() as cur:
        if not schema_ready(cur):
            rep.note = "資料庫尚未套用 migrations/postgres/0005_phase_d2.sql（npm run db:migrate:pg），不定價"
            if not dry_run:
                db.heartbeat(cur, status="warn", error=rep.note, **HEARTBEAT)
            return rep
        cur.execute("SELECT id FROM games WHERE date_utc > %s AND date_utc <= %s AND status <> 'final' ORDER BY id",
                    (now, now + horizon))
        game_ids = [r["id"] for r in cur.fetchall()]
        rep.games = len(game_ids)
        if not game_ids:
            rep.note = "未來 48 小時沒有未開賽的比賽"
            if not dry_run:
                db.heartbeat(cur, status="ok", records_updated=0, **HEARTBEAT)
            return rep
        odds_rows, pred_rows = load_rows(cur, game_ids, now)
    pricings = price_games(odds_rows, pred_rows, game_ids, now, engine.ArtifactCache(root), rep)
    rep.pricings = [mp.to_dict() for mp in pricings] if dry_run else []
    if dry_run:
        return rep
    with db.transaction() as cur:
        rep.rows_inserted, rep.rows_existing = persist(cur, pricings)
    with db.cursor() as cur:
        status = "error" if rep.errors else ("warn" if rep.invalid_snapshots else "ok")
        err = "; ".join(e["error"] for e in rep.errors)[:500] if rep.errors else (
            f"{len(rep.invalid_snapshots)} 個 snapshot 無法通過 canonical 驗證" if rep.invalid_snapshots else None)
        db.heartbeat(cur, status=status, error=err, records_updated=rep.rows_inserted, **HEARTBEAT)
    log.info("定價：比賽 %d、市場 %d %s、新增 %d 列、既有 %d 列、錯誤 %d", rep.games, rep.markets, rep.by_status,
             rep.rows_inserted, rep.rows_existing, len(rep.errors))
    return rep


def reconstruct_game(game_id: int, as_of: datetime, *, root: str | Path | None = None, db=None
                     ) -> PricingReport:
    """歷史重建：只用 as_of 以前已存在的 snapshot / prediction（及其當時的 artifact 版本）。不寫入。"""
    from .. import db as default_db

    db = db or default_db
    as_of = ensure_utc(as_of)
    rep = PricingReport(as_of.isoformat(), games=1)
    with db.cursor() as cur:
        odds_rows, pred_rows = load_rows(cur, [game_id], as_of)
    pricings = price_games(odds_rows, pred_rows, [game_id], as_of, engine.ArtifactCache(root), rep,
                           include_not_open=True)
    rep.pricings = [mp.to_dict() for mp in pricings]
    return rep


def pricing_job_scheduled() -> None:
    """排程進入點（例外由 scheduling.guarded 接住）。"""
    pricing_job()
