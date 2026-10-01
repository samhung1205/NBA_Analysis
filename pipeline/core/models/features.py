"""
特徵工程（規格書 §4 Phase B-7）
------------------------------------------------------------
賽程密集度/休息日、傷病影響、近況、H2H。全部只能用「比賽開打前已知」
的資訊 —— walk-forward 回測呼叫這裡時，餵進來的都已是嚴格早於當前
比賽時間的歷史資料（由呼叫端保證，見 jobs/backtest.py 的時間序處理）。

臨場因素（休息/傷病）以 Elo 點數調整，只影響「單場預測機率」，
不改變球隊的基礎 Elo 評分（基礎評分只依實際比賽結果更新）。
"""
from __future__ import annotations

REST_B2B_PENALTY = 30.0
REST_ADVANTAGE_BONUS = 10.0
INJURY_PENALTY_PER_STARTER = 40.0
INJURY_PENALTY_CAP = 150.0


def rest_adjustment(home_rest_days: int | None, away_rest_days: int | None) -> float:
    """回傳加到「主隊 - 客隊」Elo 差距的調整值"""
    adj = 0.0
    if home_rest_days == 0:
        adj -= REST_B2B_PENALTY
    elif home_rest_days is not None and home_rest_days >= 2:
        adj += REST_ADVANTAGE_BONUS
    if away_rest_days == 0:
        adj += REST_B2B_PENALTY
    elif away_rest_days is not None and away_rest_days >= 2:
        adj -= REST_ADVANTAGE_BONUS
    return adj


def injury_adjustment(home_starters_out: int, away_starters_out: int) -> tuple[float, float, float]:
    """回傳 (home_penalty, away_penalty, net_adjustment)；net 為正代表對主隊有利"""
    home_pen = min(home_starters_out * INJURY_PENALTY_PER_STARTER, INJURY_PENALTY_CAP)
    away_pen = min(away_starters_out * INJURY_PENALTY_PER_STARTER, INJURY_PENALTY_CAP)
    return home_pen, away_pen, away_pen - home_pen


def recent_form(results: list[bool], n: int = 10) -> float | None:
    """results：由新到舊排序的最近戰績（True=贏），回傳加權勝率（近期權重較高）"""
    recent = results[:n]
    if not recent:
        return None
    weights = [n - i for i in range(len(recent))]
    return sum(w * (1.0 if r else 0.0) for w, r in zip(weights, recent)) / sum(weights)
