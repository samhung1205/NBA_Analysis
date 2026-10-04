"""
Walk-forward 樣本外（OOS）點預測（C.5E）
------------------------------------------------------------
預測分佈的參數只能由「樣本外殘差」估計：對每個測試賽季 S，點預測模型（C.5C 固定規格，與 production 相同的
特徵 / 收縮 / 補值流程）只用 S 之前的賽季訓練，再預測 S 的每一場。production 的 full-fit 模型對訓練資料的
in-sample 殘差一律不用（它們偏小，會讓預測分佈過窄）。

輸出一列 = (profile, target, game)：pred、y、resid = y − pred，以及尺度模型 / 稽核用的情境欄位。
train_max_season 記錄 fold 模型看過的最後一季（必定 < season；測試會檢查）。
"""
from __future__ import annotations

import warnings
from typing import Iterable

import numpy as np
import pandas as pd

from ..models import temporal_model as tm
from . import features as pf
from . import spec

REG_TARGETS = ("margin", "total", "h1_margin", "h1_total")
FIRST_OOS_SEASON = "2022-23"         # 2021-22 是資料庫第一季：只能當訓練資料


def fold_predictions(frame: pd.DataFrame, test_season: str, targets: Iterable[str] = REG_TARGETS) -> pd.DataFrame:
    """以 season < test_season 訓練（收縮參數 / 補值 / 模型皆只用訓練季），預測 test_season。"""
    tr = frame[frame["season"] < test_season]
    te = frame[frame["season"] == test_season]
    if tr.empty or te.empty:
        return pd.DataFrame()
    with warnings.catch_warnings():
        # 第一個 fold（只有一季訓練資料）沒有「上季」可擬合先驗係數 → 係數為 0（等同只用本季收縮），numpy 會警告空切片
        warnings.simplefilter("ignore", RuntimeWarning)
        params = tm.fit_all_blends(tr)
    trf, fills = tm.add_model_columns(tm.apply_blends(tr, params))
    tef, _ = tm.add_model_columns(tm.apply_blends(te, params), fills)
    rows = []
    ctx = pd.DataFrame({"game_id": tef["game_id"].values, "season": test_season,
                        "game_time_utc": tef["game_time_utc"].values, "min_gp": tef["min_gp"].values,
                        "home_gp": tef["home_gp"].values, "away_gp": tef["away_gp"].values,
                        "inj_both_known": tef["inj_both_known"].values, "cont_known": tef["cont_known"].values,
                        "is_playoffs": tef["is_playoffs"].values, "train_max_season": tr["season"].max(),
                        "n_train_games": len(tr)})
    for target in targets:
        cfg = spec.PRODUCTION_SPEC[target]
        ycol = spec.TARGET_Y[target]
        feats = list(tm.TARGETS[target][1][cfg["group"]])
        trn = trf[(trf["season"] >= cfg["train_start"]) & trf[ycol].notna()]
        est = tm.fit_logistic(trn[feats], trn[ycol].astype(int), cfg["hp"]) if cfg["family"] == "logistic" \
            else tm.fit_ridge(trn[feats], trn[ycol], cfg["hp"])
        X = tef[feats].fillna(0.0)
        pred = est.predict_proba(X)[:, 1] if target == "win" else est.predict(X)
        part = ctx.copy()
        part["target"] = target
        part["pred"] = pred
        part["y"] = tef[ycol].values.astype(float)
        rows.append(part)
    out = pd.concat(rows, ignore_index=True)
    out["resid"] = out["y"] - out["pred"]
    return out


def walk_forward_oos(history: pf.HistoryInputs, *, first_season: str = FIRST_OOS_SEASON,
                     frames: dict | None = None, targets: Iterable[str] = REG_TARGETS) -> pd.DataFrame:
    """每個 profile、每個測試賽季（≥ first_season）的 OOS 預測。frames 可傳入已建好的訓練表以免重算。"""
    frames = frames or pf.build_training_frames(history)
    parts = []
    for profile, (frame, _) in frames.items():
        for season in sorted(s for s in frame["season"].unique() if s >= first_season):
            p = fold_predictions(frame, season, targets)
            if not p.empty:
                p.insert(0, "profile", profile)
                parts.append(p)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out = out[out["y"].notna()].reset_index(drop=True)
    assert (out["train_max_season"] < out["season"]).all(), "OOS 預測的訓練資料必須早於測試賽季"
    out["game_time_utc"] = pd.to_datetime(out["game_time_utc"], utc=True)
    return out.sort_values(["profile", "target", "game_time_utc", "game_id"]).reset_index(drop=True)


def context_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    from ..models.distributions import context_features
    return context_features(df["pred"].values, df["min_gp"].values, df["inj_both_known"].values,
                            df["is_playoffs"].values)
