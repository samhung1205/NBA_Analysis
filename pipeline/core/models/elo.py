"""
Elo 評分模型（規格書 §4 Phase B-8）
------------------------------------------------------------
標準 NBA Elo：主場優勢以固定 Elo 點數加成、依分差調整 K 值的
margin-of-victory 乘數（業界常見做法，如 FiveThirtyEight NBA 模型）。
"""
from __future__ import annotations

INITIAL_RATING = 1500.0
HOME_ADVANTAGE = 100.0  # 主場優勢換算的 Elo 點數加成
K_BASE = 20.0
SEASON_REGRESS_FACTOR = 0.25  # 新賽季開打前，評分往 1500 迴歸的比例


def win_prob(rating_diff: float) -> float:
    """rating_diff = (主隊評分 + 主場加成) - 客隊評分"""
    return 1.0 / (1.0 + 10 ** (-rating_diff / 400))


def mov_multiplier(margin: float, rating_diff: float) -> float:
    """分差越大、Elo 差距越小的爆冷，K 值調整倍數越大"""
    return ((abs(margin) + 3) ** 0.8) / (7.5 + 0.006 * abs(rating_diff))


def update_ratings(
    home_rating: float, away_rating: float, *, home_won: bool, margin: float,
    rating_diff: float, k: float = K_BASE,
) -> tuple[float, float]:
    """依實際結果更新評分。rating_diff 須用「未含臨場調整」的基礎評分差，
    確保評分只反映球隊實力，臨場因素（休息/傷病）只影響單場預測機率。"""
    expected = win_prob(rating_diff)
    actual = 1.0 if home_won else 0.0
    mult = mov_multiplier(margin, rating_diff)
    delta = k * mult * (actual - expected)
    return home_rating + delta, away_rating - delta


def regress_to_mean(rating: float, factor: float = SEASON_REGRESS_FACTOR,
                     mean: float = INITIAL_RATING) -> float:
    return rating * (1 - factor) + mean * factor
