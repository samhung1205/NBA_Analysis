"""
Production 預測工作（C.5D）
------------------------------------------------------------
    python run_predict.py --kind early|final|refresh [--now ISO] [--game-id N ...] [--dry-run]

kind
  early    每日（台灣 12:20，賽程同步後）：未來 36 小時內所有未開賽比賽
  final    每 5 分鐘檢查：開賽前 5~75 分鐘、且目前 artifact 尚未產生 final 版本的比賽（≈ 開賽前 60 分鐘重算）
  refresh  傷病觸發（每 15 分鐘檢查，傷病抓取之後）：開賽 75 分鐘 ~ 36 小時內的比賽，只有在「最後一次預測之後
           出現了新的傷病報告」時才重算；重算結果與上一筆相比有實質變化才寫入（MATERIAL）
  （C.5C：T-60 → T-15 沒有可測得的改善，所以不做 15 分鐘級的開賽前輪詢）

寫入規則（predictions 表沒有唯一鍵；API 讀「每場 created_at 最新的一筆」）：
  * 同一場比賽的寫入以 pg_advisory_xact_lock 序列化；
  * 與該場（同 model_version）最新一筆比較 input_hash（模型輸入向量 + artifact 版本 + profile + 資料品質旗標）：
      相同 → 不寫（重跑不會無限新增）；唯一例外是 final 第一次確認（最新一筆不是 final）會寫一筆 final。
      不同 → 寫入新的一筆（保留歷史版本：features_json 帶 prediction_kind / prediction_as_of_utc / input_hash /
             artifact_version / supersedes）。refresh 另外要求變化達到 MATERIAL 門檻。
  * 開賽前 5 分鐘內、已開賽的比賽一律不寫。
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..timeutil import ensure_utc, now_utc, parse_utc
from . import artifact as art_mod
from . import spec
from .features import HistoryInputs, ScheduledGame
from .inference import MIN_LEAD, GamePrediction, Skipped, predict_games

log = logging.getLogger(__name__)

KINDS = ("early", "final", "refresh")
HORIZON = timedelta(hours=36)
FINAL_WINDOW = timedelta(minutes=75)
LOCK_NS = 58405                      # pg_advisory_xact_lock(namespace, game_id)
MATERIAL = {"home_win_prob": 0.01, "pred_margin": 0.5, "pred_total": 1.0, "pred_home_h1": 0.5, "pred_away_h1": 0.5}
HEARTBEAT = dict(source_key="model_predict", display_name=f"預測引擎（{spec.MODEL_VERSION}）", category="model",
                 expected_interval_min=24 * 60)


@dataclass
class PredictReport:
    kind: str
    as_of: str
    artifact_version: str | None = None
    candidates: int = 0
    inserted: int = 0
    unchanged: int = 0
    immaterial: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None


# ------------------------------------------------------------------ #
# 候選比賽                                                              #
# ------------------------------------------------------------------ #

def _latest_rows(cur, game_ids: list[int]) -> dict[int, dict]:
    if not game_ids:
        return {}
    cur.execute(
        """SELECT DISTINCT ON (game_id) id, game_id, created_at, home_win_prob, pred_margin, pred_total,
                  pred_home_h1, pred_away_h1, features_json
             FROM predictions WHERE model_version = %s AND game_id = ANY(%s)
            ORDER BY game_id, created_at DESC, id DESC""", (spec.MODEL_VERSION, game_ids))
    return {r["game_id"]: r for r in cur.fetchall()}


def select_candidates(cur, kind: str, now: datetime, art: art_mod.LoadedArtifact, *, horizon: timedelta = HORIZON,
                      game_ids: list[int] | None = None) -> list[ScheduledGame]:
    from .inputs import load_upcoming
    if kind == "early":
        return load_upcoming(cur, now + MIN_LEAD, now + horizon, game_ids=game_ids)
    if kind == "final":
        games = load_upcoming(cur, now + MIN_LEAD, now + FINAL_WINDOW, game_ids=game_ids)
        latest = _latest_rows(cur, [g.game_id for g in games])
        out = []
        for g in games:
            fj = (latest.get(g.game_id) or {}).get("features_json") or {}
            if fj.get("prediction_kind") == "final" and fj.get("artifact_version") == art.artifact_version:
                continue
            out.append(g)
        return out
    if kind == "refresh":
        games = load_upcoming(cur, now + FINAL_WINDOW, now + horizon, game_ids=game_ids)
        latest = _latest_rows(cur, [g.game_id for g in games])
        cur.execute("SELECT max(report_time_utc) AS t FROM injury_reports WHERE report_time_utc < %s", (now,))
        newest = (cur.fetchone() or {}).get("t")
        out = []
        for g in games:
            fj = (latest.get(g.game_id) or {}).get("features_json") or {}
            last_as_of = parse_utc(fj.get("prediction_as_of_utc")) if fj else None
            if not fj or fj.get("artifact_version") != art.artifact_version:
                out.append(g)                                   # 沒有預測 / artifact 已更新
            elif newest is not None and last_as_of is not None and ensure_utc(newest) > last_as_of:
                out.append(g)                                   # 最後一次預測之後有新傷病報告
        return out
    raise ValueError(f"unknown kind {kind!r}")


# ------------------------------------------------------------------ #
# 寫入                                                                  #
# ------------------------------------------------------------------ #

def _material(latest: dict, p: GamePrediction) -> bool:
    new = p.row_values()
    for k, thr in MATERIAL.items():
        old = latest.get(k)
        if old is None or abs(float(old) - float(new[k])) >= thr:
            return True
    old_flags = ((latest.get("features_json") or {}).get("data_quality") or {}).get("flags") or []
    return sorted(old_flags) != p.quality.flags


def upsert_prediction(tcur, p: GamePrediction, kind: str) -> str:
    """回傳 inserted / unchanged / immaterial。呼叫端提供交易游標（advisory lock 在交易結束時釋放）。"""
    tcur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NS, p.game.game_id))
    tcur.execute(
        """SELECT id, created_at, home_win_prob, pred_margin, pred_total, pred_home_h1, pred_away_h1, features_json
             FROM predictions WHERE game_id = %s AND model_version = %s
            ORDER BY created_at DESC, id DESC LIMIT 1""", (p.game.game_id, spec.MODEL_VERSION))
    latest = tcur.fetchone()
    if latest is not None:
        fj = latest["features_json"] or {}
        if fj.get("input_hash") == p.input_hash:
            if not (kind == "final" and fj.get("prediction_kind") != "final"):
                return "unchanged"
        elif (kind == "refresh" and fj.get("artifact_version") == p.features_json["artifact_version"]
              and not _material(latest, p)):
            return "immaterial"            # 同一個 artifact、變化未達門檻（新 artifact 一律記錄）
    body = dict(p.features_json)
    body["prediction_kind"] = kind
    body["supersedes"] = latest["id"] if latest is not None else None
    tcur.execute(
        """INSERT INTO predictions
             (game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
              pred_home_h1, pred_away_h1, pred_home_h2, pred_away_h2, confidence, features_json)
           VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
        (p.game.game_id, spec.MODEL_VERSION, p.home_win_prob, p.pred_margin, p.pred_total, p.pred_home_h1,
         p.pred_away_h1, p.pred_home_h2, p.pred_away_h2, p.confidence, json.dumps(body, ensure_ascii=False)))
    return "inserted"


# ------------------------------------------------------------------ #
# Job                                                                  #
# ------------------------------------------------------------------ #

def predict_upcoming_games_job(kind: str = "early", now: datetime | None = None, *, horizon: timedelta = HORIZON,
                               game_ids: list[int] | None = None, dry_run: bool = False,
                               root: str | Path | None = None) -> PredictReport:
    from ..db import cursor, heartbeat, transaction
    from .inputs import load_history, load_pending

    if kind not in KINDS:
        raise ValueError(f"kind 必須是 {KINDS}")
    now = ensure_utc(now or now_utc())
    rep = PredictReport(kind, now.isoformat())
    try:
        art = art_mod.load_current(root)
    except art_mod.ArtifactError as e:
        log.error("預測中止：%s", e)
        if not dry_run:
            with cursor() as cur:
                heartbeat(cur, status="error", error=f"artifact 無法載入：{e}"[:500], **HEARTBEAT)
        raise
    rep.artifact_version = art.artifact_version
    with cursor() as cur:
        cands = select_candidates(cur, kind, now, art, horizon=horizon, game_ids=game_ids)
        rep.candidates = len(cands)
        if not cands:
            rep.note = "沒有需要預測的比賽"
            log.info("預測（%s）：沒有候選比賽", kind)
            if kind == "early" and not dry_run:          # 每日一次的存活心跳（休賽期也看得到排程有在跑）
                heartbeat(cur, status="ok", records_updated=0, **HEARTBEAT)
            return rep
        history: HistoryInputs = load_history(cur, now)
        pending = load_pending(cur, now, max(g.game_time_utc for g in cands))
    try:
        preds, skipped = predict_games(art, history, cands, pending, now, kind=kind)
    except Exception as e:
        log.exception("預測（%s）失敗", kind)
        if not dry_run:
            with cursor() as cur:
                heartbeat(cur, status="error", error=f"預測失敗：{type(e).__name__}: {e}"[:500], **HEARTBEAT)
        raise
    rep.skipped = [{"game_id": s.game_id, "reason": s.reason} for s in skipped]
    for p in preds:
        rep.predictions.append({"game_id": p.game.game_id, "tip_utc": p.game.game_time_utc.isoformat(),
                                "matchup": f"{p.game.away_abbr}@{p.game.home_abbr}", "profile": p.profile,
                                **{k: round(v, 3) for k, v in p.row_values().items()},
                                "confidence": p.confidence, "flags": p.quality.flags})
    if dry_run:
        rep.note = "dry-run：未寫入資料庫"
        return rep
    for p in preds:
        with transaction() as tcur:
            res = upsert_prediction(tcur, p, kind)
        setattr(rep, res, getattr(rep, res) + 1)
    with cursor() as cur:
        heartbeat(cur, status="warn" if skipped else "ok", records_updated=rep.inserted,
                  error=(f"{len(skipped)} 場略過：{skipped[0].reason}" if skipped else None), **HEARTBEAT)
    log.info("預測（%s，%s）：候選 %d、寫入 %d、未變 %d、變化不足 %d、略過 %d", kind, art.artifact_version,
             rep.candidates, rep.inserted, rep.unchanged, rep.immaterial, len(skipped))
    return rep


def early_prediction_job() -> PredictReport:
    return predict_upcoming_games_job("early")


def final_prediction_job() -> PredictReport:
    return predict_upcoming_games_job("final")


def injury_refresh_job() -> PredictReport:
    return predict_upcoming_games_job("refresh")


def main(argv: list[str] | None = None) -> int:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="Production 預測（C.5D）")
    ap.add_argument("--kind", choices=KINDS, default="early")
    ap.add_argument("--now", help="預測時間戳（ISO8601，預設現在；只能用於重現，不可晚於開賽）")
    ap.add_argument("--horizon-hours", type=float, default=HORIZON.total_seconds() / 3600)
    ap.add_argument("--game-id", type=int, action="append")
    ap.add_argument("--dry-run", action="store_true", help="只計算並印出，不寫資料庫")
    ap.add_argument("--root", help="artifact 目錄")
    args = ap.parse_args(argv)
    rep = predict_upcoming_games_job(args.kind, parse_utc(args.now) if args.now else None,
                                     horizon=timedelta(hours=args.horizon_hours), game_ids=args.game_id,
                                     dry_run=args.dry_run, root=args.root)
    print(json.dumps(rep.__dict__, ensure_ascii=False, indent=1, default=str))
    return 0
