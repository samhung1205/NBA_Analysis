"""
ESPN 隱藏 API — 備援與交叉驗證（規格書 §1.1）
------------------------------------------------------------
無需金鑰，直接 HTTP GET。2026-10 實測：
  - 不可偽造瀏覽器 User-Agent（"Mozilla/5.0" 會被 403），用 requests 預設 UA 即可。
  - scoreboard 只接受單日 dates=YYYYMMDD，日期區間會回 400。
stats.nba.com 的 /stats/* 對部分 IP 會被 Akamai 靜默丟棄請求（見 README），
此時以本模組作為賽程/比分/逐節的主要來源。
"""
from __future__ import annotations

import logging

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"

# ESPN 縮寫 → NBA 官方縮寫（其餘相同）
ESPN_TO_NBA_ABBR = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}

# ESPN season.type：1 季前賽、2 例行賽、3 季後賽、5 附加賽(Play-In)
STAGE_BY_TYPE = {2: "regular", 3: "playoffs", 5: "playin"}


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def fetch_scoreboard(yyyymmdd: str) -> list[dict]:
    resp = requests.get(f"{BASE}/scoreboard", params={"dates": yyyymmdd}, timeout=20)
    resp.raise_for_status()
    out = []
    for ev in resp.json().get("events", []):
        comp = ev["competitions"][0]
        teams = comp["competitors"]
        home = next(t for t in teams if t["homeAway"] == "home")
        away = next(t for t in teams if t["homeAway"] == "away")
        out.append({
            "espn_event_id": ev["id"],
            "date_utc": ev.get("date"),
            "season_type": ev.get("season", {}).get("type"),
            "status": comp.get("status", {}).get("type", {}).get("name"),
            "home_abbr": ESPN_TO_NBA_ABBR.get(home["team"]["abbreviation"], home["team"]["abbreviation"]),
            "away_abbr": ESPN_TO_NBA_ABBR.get(away["team"]["abbreviation"], away["team"]["abbreviation"]),
            "home_score": int(home["score"]) if home.get("score") not in (None, "") else None,
            "away_score": int(away["score"]) if away.get("score") not in (None, "") else None,
            "home_linescores": [int(float(ls["value"])) for ls in home.get("linescores", [])],
            "away_linescores": [int(float(ls["value"])) for ls in away.get("linescores", [])],
        })
    return out
