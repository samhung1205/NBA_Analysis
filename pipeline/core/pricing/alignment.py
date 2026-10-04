"""
時間對齊（Phase D.2）——防止 future leakage 的唯一選擇規則
------------------------------------------------------------
給定 analysis_as_of = T：

  盤口   每個 series (game, source, bookmaker, market) 取 fetched_at ≤ T 的最新一列。
         最新一列不是 open（suspended / closed）→ 該市場在 T 沒有可定價報價（不退回較舊的 open 列；與 API 相同）。
         之後的線 / 價變動（fetched_at > T）永遠看不到。
  預測   取 available_at = max(created_at, prediction_as_of_utc) ≤ T 且「有效」（ml-v2.0、有 artifact_version /
         profile / 點預測）的最新一筆；early / final / refresh 一視同仁，只看時間——不會因為 final 比較準
         就拿它去定價更早的盤口。
  模型   用該 prediction 列記錄的 artifact_version 重算（C.5E line_probability_from_prediction_row；版本目錄不可變），
         不用最新 artifact 重算舊預測。

歷史重建（D.4）與 live 定價用同一組函式，只差 T。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable

from ..odds.canonical import (FULL_GAME, H1, MONEYLINE, SPREAD, THREE_WAY, TOTAL, TWO_WAY, InvalidMarket,
                              make_snapshot)
from ..timeutil import ensure_utc, parse_utc
from .engine import (MarketPricing, ModelProbabilities, OddsInput, PredictionInput, price_market,
                     prediction_from_row, prediction_invalid_reason)

_FROM_LEGACY = {"ml": (MONEYLINE, FULL_GAME), "spread": (SPREAD, FULL_GAME), "total": (TOTAL, FULL_GAME),
                "h1_ml": (MONEYLINE, H1), "h1_spread": (SPREAD, H1), "h1_total": (TOTAL, H1)}
THRESHOLD_TOL = 1e-9


def _num(x: Any) -> float | None:
    return None if x is None else float(x)


def odds_input_from_row(row: dict) -> OddsInput:
    """odds_snapshots 一列 → OddsInput。一律重新經過 D.1 canonical 驗證（make_snapshot），
    並核對已存的 model_threshold 與 canonical 計算一致；不一致 / 不合法 → snapshot=None + invalid_reason。"""
    fetched = ensure_utc(parse_utc(row["fetched_at"]))
    last_seen = parse_utc(row.get("last_seen_at"))
    base = dict(snapshot_id=row.get("id"), game_id=int(row["game_id"]), fetched_at=fetched,
                last_seen_at=ensure_utc(last_seen) if last_seen else None, source=row.get("source"),
                bookmaker=row.get("bookmaker") or row.get("source"), market=row.get("market"))
    mt, period = row.get("market_type"), row.get("period")
    if not mt or not period:
        if row.get("market") not in _FROM_LEGACY:
            return OddsInput(snapshot=None, invalid_reason="unknown_market", **base)
        mt, period = _FROM_LEGACY[row["market"]]
    outcome_set = row.get("outcome_set") or TWO_WAY
    sides = {MONEYLINE: ["home", "away"] + (["draw"] if outcome_set == THREE_WAY else []),
             SPREAD: ["home", "away"], TOTAL: ["over", "under"]}[mt]
    prices = {s: _num(row.get(f"{s}_odds")) for s in sides}
    try:
        snap = make_snapshot(
            source=row["source"], bookmaker=row.get("bookmaker") or row["source"],
            source_event_id=row.get("source_event_id") or "", market_type=mt, period=period,
            status=row.get("market_status") or "open", prices=prices, outcome_set=outcome_set,
            home_line=_num(row.get("line")) if mt == SPREAD else None,
            away_line=_num(row.get("away_line")) if mt == SPREAD else None,
            total_line=_num(row.get("line")) if mt == TOTAL else None)
    except InvalidMarket as e:
        return OddsInput(snapshot=None, invalid_reason=f"invalid_snapshot:{e.reason}", **base)
    stored = row.get("model_threshold")
    if stored is not None and abs(float(stored) - snap.model_threshold) > THRESHOLD_TOL:
        return OddsInput(snapshot=None, invalid_reason="threshold_mismatch", **base)
    if snap.market != row.get("market"):
        return OddsInput(snapshot=None, invalid_reason="market_code_mismatch", **base)
    return OddsInput(snapshot=snap, **base)


def series_key(row: dict) -> tuple:
    return (int(row["game_id"]), row["source"], row.get("bookmaker") or row["source"], row["market"])


def latest_snapshots_as_of(rows: Iterable[dict], as_of: datetime) -> list[dict]:
    """每個 series 在 as_of 時點的最新一列（fetched_at ≤ as_of；同時間以 id 決定）。"""
    as_of = ensure_utc(as_of)
    best: dict[tuple, dict] = {}
    for r in rows:
        t = ensure_utc(parse_utc(r["fetched_at"]))
        if t > as_of:
            continue
        k = series_key(r)
        cur = best.get(k)
        if cur is None or (t, r.get("id") or 0) > (ensure_utc(parse_utc(cur["fetched_at"])), cur.get("id") or 0):
            best[k] = r
    return [best[k] for k in sorted(best, key=lambda k: (k[0], k[1], k[2] or "", k[3]))]


def select_prediction(rows: Iterable[dict], game_id: int, as_of: datetime) -> PredictionInput | None:
    """analysis_as_of 時點以前最新且有效的一筆預測（不看 prediction_kind，只看時間）。"""
    as_of = ensure_utc(as_of)
    best: PredictionInput | None = None
    for r in rows:
        if int(r["game_id"]) != int(game_id) or prediction_invalid_reason(r) is not None:
            continue
        p = prediction_from_row(r)
        if p.available_at > as_of:
            continue
        if best is None or (p.available_at, p.created_at, p.prediction_id or 0) > \
                (best.available_at, best.created_at, best.prediction_id or 0):
            best = p
    return best


def price_game_as_of(odds_rows: Iterable[dict], prediction_rows: Iterable[dict], game_id: int, as_of: datetime,
                     model_for: Callable[[PredictionInput], ModelProbabilities]) -> list[MarketPricing]:
    """一場比賽在 as_of 時點的完整定價（每個 bookmaker / 市場各自獨立）。"""
    pred = select_prediction(prediction_rows, game_id, as_of)
    model = model_for(pred) if pred is not None else None
    out = []
    for r in latest_snapshots_as_of([r for r in odds_rows if int(r["game_id"]) == int(game_id)], as_of):
        out.append(price_market(odds_input_from_row(r), pred, model, as_of=as_of))
    return out
