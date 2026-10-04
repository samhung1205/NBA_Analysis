"""
Production 預測分佈的擬合（C.5E）
------------------------------------------------------------
重訓時：用與 production 相同流程產生的 walk-forward OOS 點預測（oos.walk_forward_oos）的殘差擬合分佈；
**不使用** production full-fit 模型的 in-sample 殘差。分佈規格固定為 C.5E 在驗證賽季選定者（spec.DISTRIBUTION_SPEC）。

存入 artifact：profiles.<profile>.distributions.<target> = {
    "state": FittedDistribution.to_state()（類型、μ、σ、尺度模型、empirical CDF 格點、擬合樣本數 / 期間）,
    "distribution_version", "spec", "shared_profiles", "oos_seasons", "fit_set_summary"（擬合集上的覆蓋率，稽核用）}
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..models import distributions as dist
from . import oos, spec


def first_oos_season(seasons: list[str]) -> str | None:
    """第一季只能當訓練資料；OOS 從第二季開始。"""
    s = sorted(set(seasons))
    return s[1] if len(s) >= 2 else None


def oos_frame(history, frames) -> pd.DataFrame:
    first = first_oos_season([g.season for g in history.games])
    if first is None:
        raise ValueError("至少需要兩個賽季才能產生樣本外殘差")
    df = oos.walk_forward_oos(history, first_season=first, frames=frames)
    if df.empty:
        raise ValueError("沒有樣本外預測可擬合分佈")
    if not (df["train_max_season"] < df["season"]).all():
        raise AssertionError("分佈擬合資料含 in-sample 預測")
    return df


def fit_profile_distributions(df: pd.DataFrame, profile: str) -> dict[str, Any]:
    out = {}
    for target, cfg in spec.DISTRIBUTION_SPEC.items():
        pool = df[(df["target"] == target) & (df["profile"].isin(spec.PROFILES if cfg["shared_profiles"] else [profile]))]
        ctx = oos.context_arrays(pool) if cfg["scaled"] else None
        if len(pool) < spec.MIN_DISTRIBUTION_FIT:
            raise ValueError(f"{profile}/{target}：樣本外殘差只有 {len(pool)} 筆（< {spec.MIN_DISTRIBUTION_FIT}）")
        loc = cfg.get("location", "oos_mean")
        if loc not in ("oos_mean", "zero"):
            raise ValueError(f"unknown location rule {loc!r}")
        d = dist.fit_distribution(target, pool["resid"].values, kind=cfg["kind"], scaled=cfg["scaled"], ctx=ctx,
                                  lam=cfg.get("lambda") or 200.0, times=pool["game_time_utc"].astype(str),
                                  fix_mu=0.0 if loc == "zero" else None)
        own = pool[pool["profile"] == profile]
        sc = dist.score_outcomes(*dist.outcome_pmf(d, own["pred"].values, oos.context_arrays(own)), own["y"].values)
        out[target] = {"state": d.to_state(), "distribution_version": spec.DISTRIBUTION_VERSION, "spec": dict(cfg),
                       "shared_profiles": bool(cfg["shared_profiles"]),
                       "oos_seasons": sorted(pool["season"].unique().tolist()),
                       "fit_set_summary": {"n": int(len(own)), "coverage": dist.coverage(sc["pit"]),
                                           "rps": float(sc["rps"].mean()),
                                           "note": "擬合集上的覆蓋率（點預測為 OOS、分佈參數為擬合值）；樣本外評估見 C.5E 報告"}}
    return out


def fit_all(history, frames) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    df = oos_frame(history, frames)
    return {p: fit_profile_distributions(df, p) for p in spec.PROFILES}, df
