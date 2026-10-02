"""
衍生指標對照驗證（stats.nba.com 不可用時的替代驗證）
------------------------------------------------------------
隨機抽樣已入庫的比賽，重新抓 cdn.nba.com box score 並檢查：
  1. eFG% / TS%：CDN 球隊統計自帶 fieldGoalsEffectiveAdjusted / trueShootingPercentage（官方公式值），
     與 team_game_derived 的值必須幾乎相等（精確公式，容許 1e-6）。
  2. 入庫的原始計數 = CDN 當下回傳值（資料沒被破壞、欄位定義正確：球隊 REB 不含球隊籃板）。
  3. 控球數：BBR 公式 vs 簡化公式 (FGA + 0.44·FTA − OREB + TOV) 的差異（聯盟平均與逐場）。
  4. 球員分鐘 / 得分加總 = 球隊分鐘 / 得分。
輸出 JSON，供 docs/phase-c5b-report.md 引用。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics

import requests

from .. import metrics
from ..sources import nba_cdn


def _simple_poss(t: dict) -> float:
    return t["fga"] + 0.44 * t["fta"] - t["oreb"] + t["tov"]


def validate(cur, sample: int = 100, seed: int = 7) -> dict:
    cur.execute("SELECT id, nba_game_id, home_team_id, away_team_id FROM games"
                " WHERE nba_game_id NOT LIKE 'espn:%%' AND status = 'final'"
                "   AND EXISTS (SELECT 1 FROM team_game_derived d WHERE d.game_id = games.id)")
    pool = cur.fetchall()
    random.Random(seed).shuffle(pool)
    sess = requests.Session()
    out = {"sampled": 0, "efg_max_abs_err": 0.0, "ts_max_abs_err": 0.0, "raw_mismatch": 0,
           "player_minutes_mismatch": 0, "player_points_mismatch": 0, "poss_bbr_vs_simple_diffs": []}
    for g in pool[:sample]:
        try:
            game = nba_cdn.get_json_or_notfound(nba_cdn.BOXSCORE_URL.format(game_id=g["nba_game_id"]), sess)["game"]
        except Exception:  # noqa: BLE001
            continue
        out["sampled"] += 1
        box = nba_cdn.box_from_game(game)
        cur.execute("SELECT * FROM team_game_stats WHERE game_id = %s", (g["id"],))
        stored = {r["team_id"]: r for r in cur.fetchall()}
        cur.execute("SELECT * FROM team_game_derived WHERE game_id = %s", (g["id"],))
        derived = {r["team_id"]: r for r in cur.fetchall()}
        for side, tid in (("home", g["home_team_id"]), ("away", g["away_team_id"])):
            raw = game[f"{side}Team"]["statistics"]
            d, s = derived[tid], stored[tid]
            out["efg_max_abs_err"] = max(out["efg_max_abs_err"], abs(d["efg_pct"] - raw["fieldGoalsEffectiveAdjusted"]))
            out["ts_max_abs_err"] = max(out["ts_max_abs_err"], abs(d["ts_pct"] - raw["trueShootingPercentage"]))
            if any(s[k] != box[side]["team"][k] for k in ("pts", "fgm", "fga", "fg3m", "fta", "oreb", "dreb", "tov", "reb")):
                out["raw_mismatch"] += 1
            players = box[side]["players"]
            if abs(sum(p["min"] for p in players) - box[side]["team"]["team_min"]) > 1.0:
                out["player_minutes_mismatch"] += 1
            if sum(p["pts"] for p in players) != box[side]["team"]["pts"]:
                out["player_points_mismatch"] += 1
        h, a = box["home"]["team"], box["away"]["team"]
        out["poss_bbr_vs_simple_diffs"].append(
            metrics.game_possessions(h, a) - (_simple_poss(h) + _simple_poss(a)) / 2)
    diffs = out.pop("poss_bbr_vs_simple_diffs")
    if diffs:
        out["poss_bbr_minus_simple"] = {"mean": round(statistics.mean(diffs), 3),
                                        "stdev": round(statistics.pstdev(diffs), 3),
                                        "max_abs": round(max(abs(x) for x in diffs), 3)}
    return out


def main() -> None:
    from ..db import cursor
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=100)
    args = ap.parse_args()
    with cursor() as cur:
        print(json.dumps(validate(cur, args.sample), indent=1))


if __name__ == "__main__":
    main()
