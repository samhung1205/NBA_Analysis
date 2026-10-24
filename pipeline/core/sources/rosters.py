"""
現役名單（roster-sync-v1）—— 每位現役球員目前所屬球隊
------------------------------------------------------------
主來源：stats.nba.com/stats/playerindex（Active=1；帶 NBA PERSON_ID，可直接對 players.nba_player_id）。
  2026-10-21 本機實測可讀；README 提到部分 IP 會被 Akamai 靜默丟棄 → 失敗時改用 ESPN。
備援：ESPN 各隊 roster（無 NBA id，只能以正規化姓名對既有球員；對不到的不新增、不猜）。

兩個來源都回傳 RosterEntry 清單；是否「完整到足以套用」由 completeness_problem() 判斷，
不完整的名單不能拿來把「不在名單上」的球員視為已離隊。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import requests

from ..timeutil import ET, now_utc
from . import espn

log = logging.getLogger(__name__)

PLAYER_INDEX_URL = "https://stats.nba.com/stats/playerindex"
MIN_PER_TEAM = 13          # NBA 規定每隊至少 13 人（含傷兵）；少於此視為抓取不完整


@dataclass(frozen=True)
class RosterEntry:
    team_abbr: str
    name: str
    nba_player_id: int | None      # ESPN 備援為 None
    position: str | None


def season_for(dt: datetime | None = None) -> str:
    """NBA 賽季字串（'2026-27'）：以美東日期 7 月起算新賽季（休賽季交易後名單即屬新賽季）。"""
    d = (dt or now_utc()).astimezone(ET).date()
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def parse_player_index(raw: dict) -> list[RosterEntry]:
    rs = raw["resultSets"][0]
    out = []
    for row in rs["rowSet"]:
        r = dict(zip(rs["headers"], row))
        if not r.get("TEAM_ID") or not r.get("TEAM_ABBREVIATION") or not r.get("ROSTER_STATUS"):
            continue   # 自由球員 / 非名單球員
        name = f"{r.get('PLAYER_FIRST_NAME') or ''} {r.get('PLAYER_LAST_NAME') or ''}".strip()
        out.append(RosterEntry(r["TEAM_ABBREVIATION"], name, int(r["PERSON_ID"]), r.get("POSITION") or None))
    return out


def fetch_nba_rosters(season: str | None = None) -> list[RosterEntry]:
    from .nba_cdn import _headers
    resp = requests.get(PLAYER_INDEX_URL, headers=_headers(), timeout=(5, 30),
                        params={"LeagueID": "00", "Season": season or season_for(), "Historical": 0, "Active": 1})
    resp.raise_for_status()
    return parse_player_index(resp.json())


def fetch_espn_rosters() -> list[RosterEntry]:
    resp = requests.get(f"{espn.BASE}/teams", timeout=20)
    resp.raise_for_status()
    teams = [t["team"] for t in resp.json()["sports"][0]["leagues"][0]["teams"]]
    out = []
    for t in teams:
        abbr = espn.ESPN_TO_NBA_ABBR.get(t["abbreviation"], t["abbreviation"])
        r = requests.get(f"{espn.BASE}/teams/{t['id']}/roster", timeout=20)
        r.raise_for_status()
        for a in r.json().get("athletes", []):
            out.append(RosterEntry(abbr, a.get("fullName") or a.get("displayName") or "", None,
                                   (a.get("position") or {}).get("abbreviation")))
    return out


def fetch_rosters(season: str | None = None) -> tuple[str, list[RosterEntry]]:
    """回傳 (來源, 名單)。NBA 失敗 → ESPN；兩者都失敗則拋出最後一個例外。"""
    try:
        return "nba_playerindex", fetch_nba_rosters(season)
    except Exception as e:  # noqa: BLE001
        log.warning("stats.nba.com playerindex 失敗（%s），改用 ESPN 名單", type(e).__name__)
    return "espn", fetch_espn_rosters()


def completeness_problem(entries: list[RosterEntry], known_abbrs: set[str]) -> str | None:
    """名單不完整（缺隊、某隊人數過少、出現未知球隊）→ 回傳原因；完整 → None。"""
    per: dict[str, int] = {}
    for e in entries:
        per[e.team_abbr] = per.get(e.team_abbr, 0) + 1
    unknown = sorted(set(per) - known_abbrs)
    if unknown:
        return f"未知球隊縮寫：{unknown}"
    missing = sorted(known_abbrs - set(per))
    if missing:
        return f"缺少球隊名單：{missing}"
    thin = sorted(a for a, n in per.items() if n < MIN_PER_TEAM)
    if thin:
        return f"名單人數不足 {MIN_PER_TEAM}：{thin}"
    return None
