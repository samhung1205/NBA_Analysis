#!/usr/bin/env python
"""免費來源唯讀檢查：四次請求、無 DB / .env / stats.nba.com / 台彩存取。

這是執行主機當下的可用性檢查，不保證歷史覆蓋率或正式主機也可取得。
"""
from __future__ import annotations

import argparse
import json
import re

import requests

from core.sources import nba_cdn, referees
from core.timeutil import ET, now_utc


def check_sources(game_id="0022400001", *, get=requests.get):
    if not re.fullmatch(r"\d{10}", game_id):
        raise ValueError("game id 須為 10 位數字；請選已結束且已知存在的場次")
    results = []
    urls = {
        "schedule": nba_cdn.SCHEDULE_URL,
        "boxscore": nba_cdn.BOXSCORE_URL.format(game_id=game_id),
        "playbyplay": f"{nba_cdn.BASE}/liveData/playbyplay/playbyplay_{game_id}.json",
        "referees": referees.URL,
    }
    for name, url in urls.items():
        try:
            response = get(url, headers=nba_cdn._headers(), timeout=15)
            response.raise_for_status()
            observed = now_utc()  # 回應完成後，不把頁面日期冒充發布時間
            if name == "referees":
                data = referees.parse_assignments(response.text, observed_at=observed)
                state = "ready" if data["assignments"] else "waiting"
                if data["report_date_et"] != observed.astimezone(ET).date().isoformat():
                    state = "stale"
                detail = data
            else:
                raw = response.json()
                if name == "schedule":
                    games = [g for d in raw["leagueSchedule"]["gameDates"] for g in d["games"]]
                    if not games:
                        raise ValueError("schedule empty")
                    detail = {"games": len(games), "with_venue": sum(bool(g.get("arenaName")) for g in games)}
                else:
                    game = raw["game"]
                    if game.get("gameId") != game_id:
                        raise ValueError("game id mismatch")
                    if name == "boxscore":
                        players = [p for side in ("homeTeam", "awayTeam") for p in game[side]["players"]]
                        if not players:
                            raise ValueError("boxscore players empty")
                        detail = {"game_id": game_id, "players": len(players),
                                  "officials": len(game.get("officials") or []),
                                  "venue_timezone": (game.get("arena") or {}).get("arenaTimezone")}
                    else:
                        actions = game["actions"]
                        if not actions:
                            raise ValueError("playbyplay actions empty")
                        detail = {"game_id": game_id, "actions": len(actions),
                                  "substitutions": sum(a.get("actionType") == "substitution" for a in actions),
                                  "validated_five_player_lineups": False}
                state = "ready"
            results.append({"source": name, "status": state, "http_status": response.status_code,
                            "observed_at": observed.isoformat(), "detail": detail})
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            # 不印 response、headers、或任意 exception 字串；一次失敗即停該來源，不重試 challenge。
            status = getattr(getattr(e, "response", None), "status_code", None)
            results.append({"source": name, "status": "unavailable", "http_status": status,
                            "error_type": type(e).__name__})
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game-id", default="0022400001", help="已知存在的歷史 CDN canary，非待開打比賽")
    args = ap.parse_args()
    if not re.fullmatch(r"\d{10}", args.game_id):
        ap.error("--game-id 須為 10 位數字")
    results = check_sources(args.game_id)
    print(json.dumps({"scope": "current_host_read_only", "checks": results}, ensure_ascii=False, indent=2))
    return int(any(r["status"] in ("unavailable", "stale") for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
