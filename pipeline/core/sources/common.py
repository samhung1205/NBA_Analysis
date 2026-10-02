"""
各來源共用的賽事正規化工具
------------------------------------------------------------
所有來源（NBA CDN / stats.nba.com / ESPN）都轉成同一種 game dict，
下游 jobs/games_sync.py 不需要知道資料從哪來：

  nba_game_id, source, season, season_stage, date_utc(aware UTC), status,
  home_nba_team_id, away_nba_team_id, home_abbr, away_abbr, home_pts, away_pts,
  [home_q1.. away_h2]（有逐節資料時才帶；沒帶的欄位 UPSERT 不會覆寫既有值）
"""
from __future__ import annotations

GAME_STATUS_MAP = {1: "scheduled", 2: "live", 3: "final"}

# NBA gameId 前綴：001 季前賽、002 例行賽、003 明星賽、004 季後賽、005 附加賽、006 NBA Cup 決賽
_STAGE_BY_PREFIX = {"002": "regular", "004": "playoffs", "005": "playin", "006": "regular"}


def stage_from_game_id(game_id: str) -> str | None:
    """None = 不收錄（季前賽 / 明星賽 / 未知）。"""
    return _STAGE_BY_PREFIX.get(str(game_id)[:3])


def season_from_game_id(game_id: str) -> str:
    """'0022501000' → '2025-26'（gameId 第 4~5 碼為賽季起始年後兩碼）。"""
    y = 2000 + int(str(game_id)[3:5])
    return f"{y}-{str(y + 1)[-2:]}"


def quarters_from_periods(side: str, periods: list[dict]) -> dict:
    """periods: [{'period': 1, 'score': 28}, ...]（NBA CDN / ScoreboardV3 同格式）。
    OT 合併為單一 *_ot 欄位；半場 = Q1+Q2 / Q3+Q4。"""
    q: dict = {f"{side}_q{i}": None for i in range(1, 5)}
    ot = 0
    has_ot = False
    for p in periods or []:
        n, score = p.get("period"), p.get("score")
        if n is None:
            continue
        if 1 <= n <= 4:
            q[f"{side}_q{n}"] = score
        elif n >= 5:
            has_ot = True
            ot += score or 0
    q[f"{side}_ot"] = ot if has_ot else None
    q[f"{side}_h1"] = _sum_or_none(q[f"{side}_q1"], q[f"{side}_q2"])
    q[f"{side}_h2"] = _sum_or_none(q[f"{side}_q3"], q[f"{side}_q4"])
    return q


def _sum_or_none(a, b):
    return None if a is None or b is None else a + b
