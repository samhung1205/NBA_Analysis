"""
Production 重訓（C.5D）
------------------------------------------------------------
    python run_retrain.py                      # cutoff = 現在；訓練 → 檢查 → 原子寫入 → promote
    python run_retrain.py --cutoff 2026-10-03T00:00:00Z --no-promote
    python run_retrain.py --list | --rollback [--to <artifact_version>]

規則
  * 只用「開賽早於 cutoff − 4 小時、已 final」的比賽與 cutoff 之前的傷病報告（spec.TRAINING_GAME_BUFFER）。
  * 模型規格固定為 C.5C 選定（spec.PRODUCTION_SPEC）；**不重新選模型 / 調參**。收縮/先驗參數、補值常數、
    傷病校準狀態屬於「模型擬合」，每次重訓用全部訓練資料重新擬合（與 C.5C walk-forward 每個 fold 相同）。
  * 兩個 profile（early / final）各自以對應決策時點的傷病特徵訓練。
  * 決定性：邏輯迴歸（lbfgs）/ Ridge（closed form）無隨機性；同一 cutoff、同一資料重訓結果相同（有測試）。
  * 失敗不破壞 production：任何一步失敗都不會改 CURRENT；版本目錄先寫到 .tmp-* 再 rename（artifact.save_version）。
  * 上線前檢查（gates）：所有預測有限值、勝負 in-sample log loss < ln 2、各回歸 MAE < 常數模型、
    存檔→重新載入後預測與記憶體內模型完全一致。未通過 → 不 promote（不寫版本目錄）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..models import elo_state
from ..models import temporal_features as tf
from ..models import distributions as dist
from ..models import temporal_model as tm
from ..timeutil import ensure_utc, now_utc, parse_utc
from . import artifact as art_mod
from . import distribution_fit
from . import features as pf
from . import spec
from .inference import predict_profile

log = logging.getLogger(__name__)


class RetrainGateFailed(RuntimeError):
    pass


@dataclass
class RetrainResult:
    artifact_version: str
    path: str
    training_cutoff_utc: str
    n_games: int
    n_train: dict[str, int]
    promoted: bool
    gates: dict[str, Any]
    seconds: float


def data_fingerprint(history: pf.HistoryInputs) -> str:
    h = hashlib.sha256()
    for g in sorted(history.games, key=lambda g: g.game_id):
        h.update(f"{g.game_id}|{g.game_time_utc.isoformat()}|{g.home_pts}|{g.away_pts}|{g.home_h1}|{g.away_h1};".encode())
    h.update(f"derived={len(history.derived)};players={sum(len(v) for v in history.players.values())};"
             f"reports={len(history.injury_index.snapshots)}".encode())
    return h.hexdigest()[:16]


def training_frames(history: pf.HistoryInputs) -> dict[str, tuple[pd.DataFrame, Any]]:
    """{profile: (含 y_{side}_* 的模型輸入表, walk-forward 傷病校準器)}（見 features.build_training_frames）。"""
    return pf.build_training_frames(history)


def _fit(family: str, X: pd.DataFrame, y: pd.Series, hp: float):
    if family == "logistic":
        return tm.fit_logistic(X, y.astype(int), hp)
    if family == "ridge":
        return tm.fit_ridge(X, y, hp)
    raise ValueError(family)


def _in_sample(target: str, pred: np.ndarray, y: np.ndarray) -> dict[str, float]:
    if target == "win":
        p = np.clip(pred, 1e-6, 1 - 1e-6)
        return {"log_loss": float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()),
                "brier": float(((p - y) ** 2).mean())}
    return {"mae": float(np.abs(pred - y).mean()), "mae_const": float(np.abs(y - y.mean()).mean())}


def train_bundle(history: pf.HistoryInputs, cutoff: datetime, *, trained_at: datetime | None = None
                 ) -> tuple[dict[str, Any], dict[str, pd.DataFrame]]:
    """回傳 (bundle, {profile: 已套用收縮/補值的訓練表})。history 必須已截在 cutoff − buffer 之前。"""
    cutoff = ensure_utc(cutoff)
    trained_at = ensure_utc(trained_at or now_utc())
    limit = cutoff - spec.TRAINING_GAME_BUFFER
    late = [g.game_id for g in history.games if ensure_utc(g.game_time_utc) >= limit]
    if late:
        raise ValueError(f"訓練資料含開賽不早於 cutoff − buffer 的比賽：{late[:5]}")
    if not history.games:
        raise ValueError("沒有可訓練的比賽")
    profiles: dict[str, Any] = {}
    finals: dict[str, pd.DataFrame] = {}
    frames = training_frames(history)
    # 預測分佈：只用 walk-forward 樣本外殘差（不用下面 full-fit 模型的 in-sample 殘差）
    distributions, oos_df = distribution_fit.fit_all(history, frames)
    for name, (frame, cal) in frames.items():
        params = tm.fit_all_blends(frame)
        fin, fills = tm.add_model_columns(tm.apply_blends(frame, params))
        models: dict[str, Any] = {}
        for target, cfg in spec.PRODUCTION_SPEC.items():
            feats = list(tm.TARGETS[target][1][cfg["group"]])
            ycol = spec.TARGET_Y[target]
            trn = fin[(fin["season"] >= cfg["train_start"]) & fin[ycol].notna()]
            if target == "win" and trn[ycol].nunique() < 2:
                raise ValueError("勝負訓練資料只有單一類別")
            est = _fit(cfg["family"], trn[feats], trn[ycol], cfg["hp"])
            pred = est.predict_proba(trn[feats].fillna(0.0))[:, 1] if target == "win" else est.predict(trn[feats].fillna(0.0))
            models[target] = {"features": feats, "estimator": est, **cfg, "y": ycol, "n_train": int(len(trn)),
                              "train_seasons": sorted(trn["season"].unique().tolist()),
                              "in_sample": _in_sample(target, pred, trn[ycol].astype(float).values)}
        profiles[name] = {"timing": spec.PROFILES[name],
                          "blend_params": {f"{m}|{mode}": asdict(bp) for (m, mode), bp in params.items()},
                          "fills": {k: float(v) for k, v in fills.items()},
                          "calibrator": cal.to_state(), "models": models,
                          "distributions": distributions[name]}
        finals[name] = fin
    seasons = sorted({g.season for g in history.games})
    n_train = {t: profiles["final"]["models"][t]["n_train"] for t in spec.PRODUCTION_SPEC}
    bundle = {
        "schema_version": spec.ARTIFACT_SCHEMA_VERSION, "model_version": spec.MODEL_VERSION,
        "artifact_version": art_mod.make_artifact_version(cutoff, trained_at),
        "feature_version": tf.FEATURE_VERSION,
        "training_cutoff_utc": cutoff.isoformat(), "training_seasons": seasons,
        "trained_at_utc": trained_at.isoformat(), "spec": spec.PRODUCTION_SPEC, "profiles": profiles,
        "library_versions": art_mod.library_versions(),
        "metadata": {"n_games_history": len(history.games), "n_train": n_train,
                     "last_game_utc": max(ensure_utc(g.game_time_utc) for g in history.games).isoformat(),
                     "n_injury_reports": len(history.injury_index.snapshots),
                     "data_fingerprint": data_fingerprint(history),
                     "training_game_buffer_hours": spec.TRAINING_GAME_BUFFER.total_seconds() / 3600,
                     "calibrator_n_obs": {p: profiles[p]["calibrator"]["n_obs"] for p in profiles},
                     "in_sample": {p: {t: profiles[p]["models"][t]["in_sample"] for t in spec.PRODUCTION_SPEC}
                                   for p in profiles},
                     "elo": "replayed (models/elo_state; elo-v1.0 rules)",
                     "distributions": {"version": spec.DISTRIBUTION_VERSION, "spec": spec.DISTRIBUTION_SPEC,
                                       "oos_rows": int(len(oos_df)),
                                       "oos_seasons": sorted(oos_df["season"].unique().tolist()),
                                       "sigma": {p: {t: distributions[p][t]["state"]["sigma"] for t in distributions[p]}
                                                 for p in distributions},
                                       "mu": {p: {t: distributions[p][t]["state"]["mu"] for t in distributions[p]}
                                              for p in distributions}}},
    }
    return bundle, finals


SAMPLE_ROWS = 200


def run_gates(bundle: dict[str, Any], finals: dict[str, pd.DataFrame]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    ok = True
    for name, prof in bundle["profiles"].items():
        fin = finals[name]
        preds = predict_profile(prof, fin)
        for t, p in preds.items():
            finite = bool(np.isfinite(p).all())
            checks[f"{name}/{t}/finite"] = finite
            ok &= finite
        ins = {t: prof["models"][t]["in_sample"] for t in spec.PRODUCTION_SPEC}
        c = ins["win"]["log_loss"] < math.log(2)
        checks[f"{name}/win/log_loss<ln2"] = c
        ok &= c
        for t in ("margin", "total", "h1_margin", "h1_total"):
            c = ins[t]["mae"] < ins[t]["mae_const"]
            checks[f"{name}/{t}/mae<const"] = c
            ok &= c
            st = prof["distributions"][t]["state"]
            c = bool(np.isfinite(st["sigma"]) and 2.0 < st["sigma"] < 60.0 and np.isfinite(st["mu"])
                     and st["n_fit"] >= spec.MIN_DISTRIBUTION_FIT)
            checks[f"{name}/{t}/distribution_sane"] = c
            ok &= c
    return {"passed": bool(ok), "checks": checks}


def roundtrip_verifier(bundle: dict[str, Any], finals: dict[str, pd.DataFrame]) -> Callable[[dict], None]:
    """存檔後重新載入的 bundle 預測必須與記憶體內模型完全相同（在 rename 成正式版本之前檢查）。"""
    samples = {n: f.tail(SAMPLE_ROWS) for n, f in finals.items()}
    expected = {n: predict_profile(bundle["profiles"][n], samples[n]) for n in samples}

    def verify(loaded: dict) -> None:
        for n, s in samples.items():
            got = predict_profile(loaded["profiles"][n], s)
            for t in got:
                if not np.array_equal(got[t], expected[n][t]):
                    raise RetrainGateFailed(f"存檔→載入後 {n}/{t} 預測不一致")
        for n in samples:
            tf.InjuryCalibrator.from_state(loaded["profiles"][n]["calibrator"])
            for t in spec.DISTRIBUTION_TARGETS:
                a = dist.FittedDistribution.from_state(bundle["profiles"][n]["distributions"][t]["state"])
                b = dist.FittedDistribution.from_state(loaded["profiles"][n]["distributions"][t]["state"])
                if dist.line_probabilities(a, 3.3, [-4.5, 0, 7]) != dist.line_probabilities(b, 3.3, [-4.5, 0, 7]):
                    raise RetrainGateFailed(f"存檔→載入後 {n}/{t} 分佈機率不一致")
    return verify


def retrain(cutoff: datetime | None = None, *, root: str | Path | None = None, promote: bool = True,
            history_loader: Callable[[datetime], pf.HistoryInputs] | None = None,
            trained_at: datetime | None = None, _fail_after_bundle: bool = False) -> RetrainResult:
    t0 = time.time()
    cutoff = ensure_utc(cutoff or now_utc())
    root = art_mod.artifact_root(root)
    with art_mod.exclusive_lock(root):
        n_tmp = art_mod.cleanup_tmp(root)
        if n_tmp:
            log.warning("清除 %d 個中斷留下的暫存版本目錄", n_tmp)
        limit = cutoff - spec.TRAINING_GAME_BUFFER
        history = (history_loader or _db_history)(limit)
        history = history.before(limit)
        bundle, finals = train_bundle(history, cutoff, trained_at=trained_at)
        gates = run_gates(bundle, finals)
        if not gates["passed"]:
            failed = [k for k, v in gates["checks"].items() if not v]
            raise RetrainGateFailed(f"上線前檢查未通過：{failed}")
        path = art_mod.save_version(bundle, root, gates=gates, verify=roundtrip_verifier(bundle, finals),
                                    _fail_after_bundle=_fail_after_bundle)
        promoted = False
        if promote:
            art_mod.promote(bundle["artifact_version"], root, reason="retrain")
            promoted = True
    res = RetrainResult(bundle["artifact_version"], str(path), bundle["training_cutoff_utc"],
                        bundle["metadata"]["n_games_history"], bundle["metadata"]["n_train"], promoted, gates,
                        round(time.time() - t0, 1))
    log.info("重訓完成：%s（cutoff %s，%d 場，訓練樣本 %s，promoted=%s，%.1fs）", res.artifact_version,
             res.training_cutoff_utc, res.n_games, res.n_train, promoted, res.seconds)
    return res


def _db_history(limit: datetime) -> pf.HistoryInputs:
    from ..db import cursor
    from .inputs import load_history
    with cursor() as cur:
        return load_history(cur, limit, full_injuries=True)


def retrain_job() -> RetrainResult | None:
    """排程入口（每週）：失敗只記心跳與 log，不影響目前 production artifact。"""
    from ..db import cursor, heartbeat
    meta = dict(source_key="model_retrain", display_name="模型重訓（ml-v2.0）", category="model",
                expected_interval_min=7 * 24 * 60)
    from . import activity
    try:    # scheduler-efficiency-v1：沒有新的訓練資料就不重訓（不產生與現行 artifact 數學上相同的新版本）
        with cursor() as cur:
            st = activity.retrain_state(cur, now_utc(), activity.current_artifact_n_games(), buffer=spec.TRAINING_GAME_BUFFER)
    except Exception:  # noqa: BLE001 — 判斷失敗 → 保守地照常重訓
        log.exception("重訓前置檢查失敗，照常重訓")
        st = activity.RetrainState(True, "precheck_failed")
    if not st.has_new_data:
        activity.log_skip("retrain", "no_new_training_data",
                          f"retrain_skipped_no_new_data games={st.n_games_now} artifact_games={st.n_games_artifact}", every_s=0)
        with cursor() as cur:
            heartbeat(cur, status="ok", **meta)
        return None
    try:
        res = retrain()
    except art_mod.LockBusy as e:
        log.warning("重訓略過：%s", e)
        return None
    except Exception as e:  # noqa: BLE001
        log.exception("重訓失敗（production artifact 未變動）")
        with cursor() as cur:
            heartbeat(cur, status="error", error=f"重訓失敗：{e}"[:500], **meta)
        return None
    with cursor() as cur:
        heartbeat(cur, status="ok", records_updated=res.n_train.get("win"), **meta)
    return res


def main(argv: list[str] | None = None) -> int:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="Production 重訓 / artifact 管理（C.5D）")
    ap.add_argument("--cutoff", help="訓練資料截止時間（ISO8601，預設現在）")
    ap.add_argument("--no-promote", action="store_true", help="只寫入新版本，不切換 CURRENT")
    ap.add_argument("--root", help="artifact 目錄（預設 pipeline/artifacts/production 或 MODEL_ARTIFACT_DIR）")
    ap.add_argument("--list", action="store_true", help="列出所有版本")
    ap.add_argument("--rollback", action="store_true", help="CURRENT 退回上一個上線過的版本")
    ap.add_argument("--to", help="--rollback 指定版本 / 或直接 promote 指定版本")
    args = ap.parse_args(argv)
    root = art_mod.artifact_root(args.root)
    if args.list:
        for v in art_mod.list_versions(root):
            print(("* " if v["current"] else "  ") + json.dumps(v, ensure_ascii=False))
        return 0
    if args.rollback:
        with art_mod.exclusive_lock(root):
            p = art_mod.rollback(root, to=args.to)
        print(f"CURRENT → {p['artifact_version']}（原 {p['previous']}）")
        return 0
    if args.to:
        with art_mod.exclusive_lock(root):
            p = art_mod.promote(args.to, root, reason="manual")
        print(f"CURRENT → {p['artifact_version']}（原 {p['previous']}）")
        return 0
    res = retrain(parse_utc(args.cutoff) if args.cutoff else None, root=root, promote=not args.no_promote)
    print(f"model_version={spec.MODEL_VERSION}")
    print(f"artifact_version={res.artifact_version}")
    print(f"training_cutoff_utc={res.training_cutoff_utc}")
    print(f"n_games_history={res.n_games}")
    print("n_train=" + json.dumps(res.n_train))
    print(f"promoted={res.promoted}  path={res.path}  ({res.seconds}s)")
    return 0
