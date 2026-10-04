"""
Elo 狀態重播（C.5D production inference 用）
------------------------------------------------------------
elo_ratings 表只在跑 Elo 回測（jobs/backtest.py）時寫入，每日排程不會更新它，而且未開賽比賽沒有列。
production 需要「預測時間戳之前最後的評分」，所以直接用與 backtest 完全相同的規則重播所有已結束比賽：

  * 依開賽時間排序（同一隊不會同時有兩場，同時刻的比賽互不影響）
  * 某隊進入新賽季的第一場：評分先往 1500 迴歸 25%（elo.regress_to_mean）
  * 以「未含臨場調整」的評分差（含主場加成）更新，margin-of-victory 乘數

elo_diff = 主隊賽前評分 − 客隊賽前評分（不含主場加成；與 c5c_inputs 從 elo_ratings.rating_before 取值的定義相同）。
未開賽比賽的賽前評分：若該隊上一場屬於前一季，回傳迴歸後的值（不改動狀態），與該場真正開打時 backtest 會用的值一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from . import elo

MODEL_VERSION = "elo-v1.0"      # 規則與 jobs/backtest.py（settings.model_version 預設）相同


@dataclass
class EloState:
    ratings: dict[int, float] = field(default_factory=dict)
    last_season: dict[int, str] = field(default_factory=dict)
    n_games: int = 0

    def _current(self, tid: int, season: str) -> float:
        r = self.ratings.get(tid, elo.INITIAL_RATING)
        if self.last_season.get(tid) and self.last_season[tid] != season:
            r = elo.regress_to_mean(r)
        return r

    def pregame(self, home_id: int, away_id: int, season: str) -> tuple[float, float]:
        """賽前評分（不改動狀態）。"""
        return self._current(home_id, season), self._current(away_id, season)

    def absorb(self, home_id: int, away_id: int, season: str, home_pts: int, away_pts: int) -> tuple[float, float]:
        """併入一場已結束比賽，回傳 (主隊賽前, 客隊賽前)。"""
        rh, ra = self.pregame(home_id, away_id, season)
        base_diff = (rh + elo.HOME_ADVANTAGE) - ra
        nh, na = elo.update_ratings(rh, ra, home_won=home_pts > away_pts, margin=home_pts - away_pts,
                                    rating_diff=base_diff)
        self.ratings[home_id], self.ratings[away_id] = nh, na
        self.last_season[home_id] = self.last_season[away_id] = season
        self.n_games += 1
        return rh, ra


def replay(games: Iterable) -> tuple[EloState, dict[int, tuple[float, float]]]:
    """games：GameRecord-like（game_id, season, game_time_utc, home/away_team_id, home/away_pts）。
    回傳 (最終狀態, {game_id: (主隊賽前, 客隊賽前)})。"""
    st = EloState()
    before: dict[int, tuple[float, float]] = {}
    for g in sorted(games, key=lambda g: (g.game_time_utc, g.game_id)):
        if g.home_pts is None or g.away_pts is None:
            raise ValueError(f"Elo 重播只能用已結束比賽（game_id={g.game_id}）")
        before[g.game_id] = st.absorb(g.home_team_id, g.away_team_id, g.season, g.home_pts, g.away_pts)
    return st, before
