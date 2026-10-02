"""
NBA CDN（cdn.nba.com）— 賽程 / 即時比分 / 基本 box score 的首選來源
------------------------------------------------------------
stats.nba.com 對部分 IP 會靜默丟請求（見 README 踩坑 1），但 cdn.nba.com 是靜態 JSON，
不走 stats 的反爬規則，因此賽程、即時比分、逐節、基本 box score 優先用這裡；
stats.nba.com 只留給 CDN 沒有的進階數據（pace / ORtg / DRtg）。

端點：
  - staticData/scheduleLeagueV2.json      整季賽程（含狀態、比分、開賽 UTC 時間）
  - liveData/scoreboard/todaysScoreboard_00.json   當日即時比分（含逐節）
  - liveData/boxscore/boxscore_<id>.json  基本 box score（球隊、球員、逐節）

Headers：沿用 nba_api 內建 *stats* 完整瀏覽器標頭（目前 Chrome 145），僅移除 Host
（requests 會依 URL 自動帶 cdn.nba.com）。2026-10 實測 nba_api 的 live 端點預設標頭
（Chrome 87）會被 cdn.nba.com 回 403，stats 預設標頭則為 200，故不使用 live 預設值。
不自訂 User-Agent、不輪替 proxy。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..timeutil import in_window, parse_utc
from .common import GAME_STATUS_MAP, quarters_from_periods, season_from_game_id, stage_from_game_id

log = logging.getLogger(__name__)

BASE = "https://cdn.nba.com/static/json"
SCHEDULE_URL = f"{BASE}/staticData/scheduleLeagueV2.json"
TODAY_SCOREBOARD_URL = f"{BASE}/liveData/scoreboard/todaysScoreboard_00.json"
BOXSCORE_URL = f"{BASE}/liveData/boxscore/boxscore_{{game_id}}.json"

REQUEST_TIMEOUT = 20


def _headers() -> dict:
    from nba_api.stats.library.http import NBAStatsHTTP

    return {k: v for k, v in NBAStatsHTTP.headers.items() if k.lower() != "host"}


_retry = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=1.5, max=10),
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
)


@_retry
def _get_json(url: str) -> dict:
    resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


class NotFound(Exception):
    """cdn.nba.com 對不存在的檔案回 403/404（S3 風格）。與暫時性錯誤區分：不重試。"""

    def __init__(self, url: str, status: int):
        super().__init__(f"{status} {url}")
        self.url, self.status = url, status


_transient = retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, ValueError)),
)


@_transient
def get_json_or_notfound(url: str, session: requests.Session | None = None) -> dict:
    """403/404 → NotFound（不重試）；429/5xx → requests.HTTPError（由呼叫端決定是否降速）；
    連線/逾時/JSON 解析失敗 → 重試。"""
    resp = (session or requests).get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    if resp.status_code in (403, 404):
        raise NotFound(url, resp.status_code)
    resp.raise_for_status()
    return resp.json()


# ------------------------------------------------------------------ #
# 賽程 / 比分                                                           #
# ------------------------------------------------------------------ #

def normalize_game(g: dict) -> dict | None:
    """schedule 與 todaysScoreboard 兩種格式共用（欄位名稱相同的部分）。
    不收錄的賽事（季前賽/明星賽/隊伍未定）回傳 None。"""
    gid = g.get("gameId")
    stage = stage_from_game_id(gid) if gid else None
    home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
    when = parse_utc(g.get("gameDateTimeUTC") or g.get("gameTimeUTC"))
    if not stage or not when or not home.get("teamId") or not away.get("teamId"):
        return None
    status = GAME_STATUS_MAP.get(g.get("gameStatus"), "scheduled")
    out = {
        "nba_game_id": gid,
        "source": "nba_cdn",
        "season": season_from_game_id(gid),
        "season_stage": stage,
        "date_utc": when,
        "status": status,
        "home_nba_team_id": home["teamId"],
        "away_nba_team_id": away["teamId"],
        "home_abbr": home.get("teamTricode"),
        "away_abbr": away.get("teamTricode"),
        # 尚未開打時 score 為 0，不可當成真實比分寫入
        "home_pts": (home.get("score") or None) if status != "scheduled" else None,
        "away_pts": (away.get("score") or None) if status != "scheduled" else None,
    }
    if home.get("periods") or away.get("periods"):
        out.update(quarters_from_periods("home", home.get("periods") or []))
        out.update(quarters_from_periods("away", away.get("periods") or []))
    return out


def games_from_schedule(raw: dict) -> list[dict]:
    games = []
    for day in raw.get("leagueSchedule", {}).get("gameDates", []):
        for g in day.get("games", []):
            n = normalize_game(g)
            if n:
                games.append(n)
    return games


def fetch_games_in_window(start_utc: datetime, end_utc: datetime) -> list[dict]:
    """視窗內（以 game_time_utc 判定）的所有賽事。整季賽程一次取回後過濾，
    再以當日 scoreboard 覆蓋即時狀態（schedule 檔案更新較慢）；覆蓋失敗不影響結果。"""
    games = [g for g in games_from_schedule(_get_json(SCHEDULE_URL))
             if in_window(g["date_utc"], start_utc, end_utc)]
    try:
        live = {g["nba_game_id"]: g for g in
                (normalize_game(x) for x in _get_json(TODAY_SCOREBOARD_URL)
                 .get("scoreboard", {}).get("games", [])) if g}
    except Exception as e:  # noqa: BLE001
        log.debug("CDN 當日 scoreboard 覆蓋失敗（沿用 schedule 狀態）：%s", e)
        live = {}
    return [{**g, **live[g["nba_game_id"]]} if g["nba_game_id"] in live else g for g in games]


# ------------------------------------------------------------------ #
# 基本 box score                                                       #
# ------------------------------------------------------------------ #

def parse_iso_minutes(m: str | None) -> float | None:
    """'PT22M34.00S' → 22.57；空值/格式不符 → None。"""
    if not m or not m.startswith("PT") or "M" not in m:
        return None
    try:
        mm, rest = m[2:].split("M", 1)
        ss = rest.rstrip("S") or "0"
        return round(int(mm) + float(ss) / 60, 2)
    except ValueError:
        return None


def _total(s: dict, personal: str, team: str, total: str | None = None) -> int | None:
    """球隊失誤：官方球隊 TOV 含「球隊失誤」（turnoversTeam），CDN 的 turnovers 只有球員個人，
    要用 turnoversTotal（= 個人 + 球隊）。2021-22 例行賽驗證：13.76 次/隊/場，與聯盟平均一致。"""
    if total and s.get(total) is not None:
        return s[total]
    if s.get(personal) is None:
        return None
    return s[personal] + (s.get(team) or 0)


def shape_box_side(side: dict) -> dict[str, Any]:
    s = side.get("statistics", {})
    team = {
        "pts": s.get("points"), "fg_pct": s.get("fieldGoalsPercentage"),
        "fg3_pct": s.get("threePointersPercentage"), "ft_pct": s.get("freeThrowsPercentage"),
        # 籃板只算球員個人（reboundsPersonal / Offensive / Defensive）：官方球隊 REB 不含「球隊籃板」。
        # CDN 的 reboundsTotal 含球隊籃板（2021-22 平均 52.4 次/隊/場，聯盟真實約 44），若用它算
        # ORB%/控球數會把 pace 低估約 4%（94.4 vs 官方 98.2）——C.5B 實測發現並修正。
        "reb": s.get("reboundsPersonal") if s.get("reboundsPersonal") is not None else s.get("reboundsTotal"),
        "oreb": s.get("reboundsOffensive"),
        "dreb": s.get("reboundsDefensive"),
        "ast": s.get("assists"), "stl": s.get("steals"), "blk": s.get("blocks"),
        "tov": _total(s, "turnovers", "turnoversTeam", "turnoversTotal"),   # 含球隊失誤
        "pf": s.get("foulsPersonal"),
        "fgm": s.get("fieldGoalsMade"), "fga": s.get("fieldGoalsAttempted"),
        "fg3m": s.get("threePointersMade"), "fg3a": s.get("threePointersAttempted"),
        "ftm": s.get("freeThrowsMade"), "fta": s.get("freeThrowsAttempted"),
        "team_min": parse_iso_minutes(s.get("minutes")),
    }
    players = []
    for p in side.get("players", []):
        ps = p.get("statistics", {})
        minutes = parse_iso_minutes(ps.get("minutes"))
        if str(p.get("played")) != "1" or not minutes:
            continue  # 未上場
        players.append({
            "nba_player_id": p["personId"],
            "name": p.get("name") or f"{p.get('firstName', '')} {p.get('familyName', '')}".strip(),
            "position": p.get("position") or None,
            "started": 1 if str(p.get("starter")) == "1" else 0,
            "min": minutes,
            "pts": ps.get("points"), "reb": ps.get("reboundsTotal"), "ast": ps.get("assists"),
            "stl": ps.get("steals"), "blk": ps.get("blocks"), "tov": ps.get("turnovers"),
            "fgm": ps.get("fieldGoalsMade"), "fga": ps.get("fieldGoalsAttempted"),
            "fg3m": ps.get("threePointersMade"), "fg3a": ps.get("threePointersAttempted"),
            "ftm": ps.get("freeThrowsMade"), "fta": ps.get("freeThrowsAttempted"),
            "plus_minus": ps.get("plusMinusPoints"),
        })
    return {"team": team, "players": players, "periods": side.get("periods", [])}


def box_from_game(game: dict) -> dict:
    """boxscore_<id>.json 的 `game` 節點 → 統一形狀（與 nba_stats.fetch_box_traditional 相容，另附 meta/quarters）。"""
    home, away = shape_box_side(game["homeTeam"]), shape_box_side(game["awayTeam"])
    return {
        "nba_game_id": game.get("gameId"),
        "date_utc": parse_utc(game.get("gameTimeUTC")),
        "status": GAME_STATUS_MAP.get(game.get("gameStatus"), "scheduled"),
        "arena": (game.get("arena") or {}).get("arenaName"),
        "home_team_id": game["homeTeam"]["teamId"],
        "away_team_id": game["awayTeam"]["teamId"],
        "home": home,
        "away": away,
        "quarters": {**quarters_from_periods("home", home["periods"]),
                     **quarters_from_periods("away", away["periods"])},
    }


def fetch_box_basic(nba_game_id: str, session: requests.Session | None = None) -> dict:
    """與 nba_stats.fetch_box_traditional 相同的資料形狀，另外附 quarters（逐節/半場）與 meta。
    檔案不存在 → NotFound（回填用來區分「沒有這場」與暫時性錯誤）。"""
    url = BOXSCORE_URL.format(game_id=nba_game_id)
    return box_from_game(get_json_or_notfound(url, session)["game"])
