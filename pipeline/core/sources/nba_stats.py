"""
nba_api (stats.nba.com) fetcher
------------------------------------------------------------
規格書 §1.1：無官方文件、會封鎖高頻請求；一律加隨機延遲、失敗重試，
排程抓取寫入 DB，絕不即時現抓。

端點選擇（2026-08 實測結果，詳見開發紀錄）：
  - 逐節比分：一律用 ScoreboardV3(date) —— BoxScoreSummaryV2/V3 對
    2025-26（含）之後的賽季會回傳 None，且 V3 摘要沒有逐節分數欄位；
    ScoreboardV3 按日期查詢在新舊賽季都驗證可用，且一次拿到當天全部
    賽事，呼叫次數遠低於逐場查詢。
  - 球隊/球員 box score：BoxScoreTraditionalV3 + BoxScoreAdvancedV3，
    新舊賽季皆驗證可用；V2 系列已停止供應 2025-26 賽季資料。
  - 整季賽程與基本團隊數據：LeagueGameFinder（team-game 粒度，一次
    拿全季，用來決定要抓哪些比賽/日期，並直接提供基本命中率數據）。
"""
from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any, Iterable

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ..config import settings
from ..timeutil import parse_utc
from .common import GAME_STATUS_MAP, quarters_from_periods, season_from_game_id, stage_from_game_id

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30

# 標準聯盟分區（近年未變動），static teams 端點不含此資訊，故手動維護
TEAM_META: dict[str, tuple[str, str]] = {
    "BOS": ("East", "Atlantic"), "BKN": ("East", "Atlantic"), "NYK": ("East", "Atlantic"),
    "PHI": ("East", "Atlantic"), "TOR": ("East", "Atlantic"),
    "CHI": ("East", "Central"), "CLE": ("East", "Central"), "DET": ("East", "Central"),
    "IND": ("East", "Central"), "MIL": ("East", "Central"),
    "ATL": ("East", "Southeast"), "CHA": ("East", "Southeast"), "MIA": ("East", "Southeast"),
    "ORL": ("East", "Southeast"), "WAS": ("East", "Southeast"),
    "DEN": ("West", "Northwest"), "MIN": ("West", "Northwest"), "OKC": ("West", "Northwest"),
    "POR": ("West", "Northwest"), "UTA": ("West", "Northwest"),
    "GSW": ("West", "Pacific"), "LAC": ("West", "Pacific"), "LAL": ("West", "Pacific"),
    "PHX": ("West", "Pacific"), "SAC": ("West", "Pacific"),
    "DAL": ("West", "Southwest"), "HOU": ("West", "Southwest"), "MEM": ("West", "Southwest"),
    "NOP": ("West", "Southwest"), "SAS": ("West", "Southwest"),
}

def _sleep_polite() -> None:
    time.sleep(random.uniform(settings.nba_api_min_delay, settings.nba_api_max_delay))


# 標頭一律沿用 nba_api 內建的完整預設值（NBAStatsHTTP.headers，含 Referer / Sec-Ch-Ua 等，
# 且 UA 與 Sec-Ch-Ua 版本互相一致）。**不要**再傳 headers= 或覆寫 User-Agent：
#   - 傳 headers= 會整個取代預設值（遺失 Referer 等）；
#   - 舊版曾把 UA 覆寫為 Chrome 120，但預設 Sec-Ch-Ua 宣告 Chrome 145，
#     UA / client-hints 互相矛盾，是比缺少標頭更容易被 Akamai 判為偽造的訊號。
# 偽裝相關的調整只允許走「升級 nba_api」這條路。


_retry = retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(Exception),
)


# ------------------------------------------------------------------ #
# 靜態球隊清單                                                         #
# ------------------------------------------------------------------ #

def fetch_static_teams() -> list[dict]:
    from nba_api.stats.static import teams as static_teams

    out = []
    for t in static_teams.get_teams():
        conf, div = TEAM_META.get(t["abbreviation"], (None, None))
        out.append({
            "nba_team_id": t["id"],
            "abbr": t["abbreviation"],
            "name": t["full_name"],
            "conference": conf,
            "division": div,
        })
    return out


# ------------------------------------------------------------------ #
# 整季 team-game 資料（賽程 + 基本數據）                                 #
# ------------------------------------------------------------------ #

@_retry
def fetch_season_team_games(season: str, season_type: str = "Regular Season") -> pd.DataFrame:
    from nba_api.stats.endpoints import leaguegamefinder

    _sleep_polite()
    r = leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        league_id_nullable="00",
        season_type_nullable=season_type,
        timeout=REQUEST_TIMEOUT,
    )
    return r.get_data_frames()[0]


def season_game_dates(df: pd.DataFrame) -> list[str]:
    """GameFinder 只用來有效率地找出該季「哪些日期有比賽」，
    避免逐日掃描整季（多數日期沒有比賽）。實際比賽內容一律以
    ScoreboardV3 為準（見 fetch_scoreboard_day）。"""
    return sorted(df["GAME_DATE"].unique().tolist())


# ------------------------------------------------------------------ #
# 賽事層級真相來源：ScoreboardV3（按日期查詢）                            #
# 一次拿到當天所有賽事的隊伍、比分、逐節、精確 UTC 開賽時間               #
# ------------------------------------------------------------------ #

@_retry
def fetch_scoreboard_day(date_str: str, season: str, season_stage: str) -> list[dict]:
    """date_str: YYYY-MM-DD（該日期所屬的任一時區皆可，NBA API 以美東為準，
    這裡只用來決定要查哪一天，實際比賽時間以回傳的 game_time_utc 為準）。"""
    from nba_api.stats.endpoints import scoreboardv3

    _sleep_polite()
    r = scoreboardv3.ScoreboardV3(game_date=date_str, timeout=REQUEST_TIMEOUT)
    raw = r.get_dict()
    out = []
    for g in raw.get("scoreboard", {}).get("games", []):
        out.append({
            "nba_game_id": g["gameId"],
            "season": season,
            "season_stage": season_stage,
            "date_utc": g.get("gameTimeUTC"),
            "status": GAME_STATUS_MAP.get(g.get("gameStatus"), "scheduled"),
            "home_nba_team_id": g["homeTeam"]["teamId"],
            "away_nba_team_id": g["awayTeam"]["teamId"],
            "home_pts": g["homeTeam"].get("score") or None,
            "away_pts": g["awayTeam"].get("score") or None,
            **_periods_to_quarters("home", g["homeTeam"]),
            **_periods_to_quarters("away", g["awayTeam"]),
        })
    return out


def normalize_scoreboard_game(g: dict) -> dict | None:
    """ScoreboardV3 → 與 CDN 相同的正規化 game dict（季前賽/明星賽回傳 None）。"""
    gid = g.get("gameId")
    stage = stage_from_game_id(gid) if gid else None
    when = parse_utc(g.get("gameTimeUTC"))
    home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
    if not stage or not when or not home.get("teamId") or not away.get("teamId"):
        return None
    status = GAME_STATUS_MAP.get(g.get("gameStatus"), "scheduled")
    return {
        "nba_game_id": gid, "source": "nba_api", "season": season_from_game_id(gid),
        "season_stage": stage, "date_utc": when, "status": status,
        "home_nba_team_id": home["teamId"], "away_nba_team_id": away["teamId"],
        "home_abbr": home.get("teamTricode"), "away_abbr": away.get("teamTricode"),
        "home_pts": (home.get("score") or None) if status != "scheduled" else None,
        "away_pts": (away.get("score") or None) if status != "scheduled" else None,
        **quarters_from_periods("home", home.get("periods", [])),
        **quarters_from_periods("away", away.get("periods", [])),
    }


def fetch_games_for_et_dates(et_dates: Iterable[date]) -> list[dict]:
    """逐個美東日期查 ScoreboardV3。fallback 鏈內使用：每日期只重試 2 次、任一日期失敗就
    整體失敗（交給 fallback/斷路器處理，避免被封鎖時 N 天 × 4 次重試空等）。"""
    from nba_api.stats.endpoints import scoreboardv3

    @retry(reraise=True, stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=2, max=10))
    def one_day(d: date) -> dict:
        _sleep_polite()
        return scoreboardv3.ScoreboardV3(
            game_date=d.isoformat(), timeout=REQUEST_TIMEOUT).get_dict()

    out = []
    for d in et_dates:
        for g in one_day(d).get("scoreboard", {}).get("games", []):
            n = normalize_scoreboard_game(g)
            if n:
                out.append(n)
    return out


def _periods_to_quarters(side: str, team: dict) -> dict:
    return quarters_from_periods(side, team.get("periods", []))


# ------------------------------------------------------------------ #
# Box score：traditional（基本數據）+ advanced（進階數據）                #
# ------------------------------------------------------------------ #

@_retry
def fetch_box_traditional(nba_game_id: str) -> dict:
    from nba_api.stats.endpoints import boxscoretraditionalv3

    _sleep_polite()
    r = boxscoretraditionalv3.BoxScoreTraditionalV3(
        game_id=nba_game_id, timeout=REQUEST_TIMEOUT
    )
    box = r.get_dict()["boxScoreTraditional"]
    return {
        "home_team_id": box["homeTeamId"],
        "away_team_id": box["awayTeamId"],
        "home": _shape_traditional_side(box["homeTeam"]),
        "away": _shape_traditional_side(box["awayTeam"]),
    }


def _shape_traditional_side(side: dict) -> dict:
    s = side["statistics"]
    team_stats = {
        "pts": s.get("points"), "fg_pct": s.get("fieldGoalsPercentage"),
        "fg3_pct": s.get("threePointersPercentage"), "ft_pct": s.get("freeThrowsPercentage"),
        "reb": s.get("reboundsTotal"), "oreb": s.get("reboundsOffensive"),
        "dreb": s.get("reboundsDefensive"), "ast": s.get("assists"), "stl": s.get("steals"),
        "blk": s.get("blocks"), "tov": s.get("turnovers"), "pf": s.get("foulsPersonal"),
    }
    players = []
    for p in side.get("players", []):
        ps = p.get("statistics", {})
        if not ps.get("minutes"):
            continue  # 未上場球員略過
        players.append({
            "nba_player_id": p["personId"],
            "name": f"{p['firstName']} {p['familyName']}".strip(),
            "position": p.get("position") or None,
            "started": 1 if p.get("position") else 0,  # V3 只有先發球員有 position 值
            "min": _minutes_to_float(ps.get("minutes")),
            "pts": ps.get("points"), "reb": ps.get("reboundsTotal"), "ast": ps.get("assists"),
            "stl": ps.get("steals"), "blk": ps.get("blocks"), "tov": ps.get("turnovers"),
            "fgm": ps.get("fieldGoalsMade"), "fga": ps.get("fieldGoalsAttempted"),
            "fg3m": ps.get("threePointersMade"), "fg3a": ps.get("threePointersAttempted"),
            "ftm": ps.get("freeThrowsMade"), "fta": ps.get("freeThrowsAttempted"),
            "plus_minus": ps.get("plusMinusPoints"),
        })
    return {"team": team_stats, "players": players}


def _minutes_to_float(m: str | None) -> float | None:
    if not m:
        return None
    try:
        mm, ss = m.split(":")
        return round(int(mm) + int(ss) / 60, 2)
    except (ValueError, AttributeError):
        return None


@_retry
def fetch_box_advanced(nba_game_id: str) -> dict:
    from nba_api.stats.endpoints import boxscoreadvancedv3

    _sleep_polite()
    r = boxscoreadvancedv3.BoxScoreAdvancedV3(
        game_id=nba_game_id, timeout=REQUEST_TIMEOUT
    )
    box = r.get_dict()["boxScoreAdvanced"]
    return {
        "home": _shape_advanced_team(box["homeTeam"]["statistics"]),
        "away": _shape_advanced_team(box["awayTeam"]["statistics"]),
    }


def _shape_advanced_team(s: dict) -> dict:
    return {
        "pace": s.get("pace"), "off_rtg": s.get("offensiveRating"),
        "def_rtg": s.get("defensiveRating"), "net_rtg": s.get("netRating"),
        "ts_pct": s.get("trueShootingPercentage"), "efg_pct": s.get("effectiveFieldGoalPercentage"),
        "ast_ratio": s.get("assistRatio"), "tov_ratio": s.get("turnoverRatio"),
    }
