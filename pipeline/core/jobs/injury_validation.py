"""
傷病資料語意驗證（用實際出賽結果檢驗，不用於特徵）
------------------------------------------------------------
回答兩個問題：
  1. 官方狀態（Out / Doubtful / Questionable / Probable / Available）與「實際有沒有上場」的對應比例，
     供檢驗 injury_asof.P_ABSENT 先驗是否合理。
  2. 「球員不在名單上」（Not Listed）與「該隊整隊無人列入」（implied_empty）的推定是否站得住腳：
     把賽前的「常規先發」（過去 5 場先發 ≥2 次）依賽前狀態分群，看實際缺陣率。
     - Not Listed 的常規先發缺陣率應該很低（接近 0）才表示「沒列 = 可出賽」成立；
     - Unknown（沒有報告涵蓋）作為對照，缺陣率不應顯著低於 Not Listed。

注意：這是診斷用，結果只寫進報告，不回饋到特徵或模型（避免用結果調參造成洩漏）。
"""
from __future__ import annotations

import json
from collections import defaultdict, deque

from .. import injury_asof as ia
from ..timeutil import et_date

__all__ = ["validate", "main"]


def validate(cur, *, offset_min: int = 0) -> dict:
    idx = ia.load_index(cur)
    cur.execute("SELECT game_id, team_id, player_id, started FROM player_game_stats")
    played: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    starts: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    for r in cur.fetchall():
        played[r["game_id"]][r["team_id"]].add(r["player_id"])
        if r["started"]:
            starts[r["game_id"]][r["team_id"]].add(r["player_id"])
    cur.execute(
        """SELECT g.id, g.date_utc, g.home_team_id, g.away_team_id, ht.abbr AS h, at.abbr AS a
             FROM games g JOIN teams ht ON ht.id = g.home_team_id JOIN teams at ON at.id = g.away_team_id
            WHERE g.status = 'final' AND g.season_stage IN ('regular','playin','playoffs')
              AND EXISTS (SELECT 1 FROM player_game_stats p WHERE p.game_id = g.id)
            ORDER BY g.date_utc, g.id""")
    games = cur.fetchall()

    recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=5))     # team → 最近 5 場先發集合
    by_status = defaultdict(lambda: [0, 0])                             # status → [上場, 總數]（官方列出的球員）
    starters_by_state = defaultdict(lambda: [0, 0])                     # 狀態分群 → [上場, 總數]（常規先發）

    from datetime import timedelta
    for g in games:
        cutoff = g["date_utc"] - timedelta(minutes=offset_min)
        for tid, abbr in ((g["home_team_id"], g["h"]), (g["away_team_id"], g["a"])):
            st = ia.team_state_asof(idx, team_abbr=abbr, game_date=et_date(g["date_utc"]), cutoff_utc=cutoff)
            did_play = played[g["id"]][tid]
            if st.known:
                for p in st.players:
                    if p.player_id is not None:
                        c = by_status[p.status]
                        c[0] += p.player_id in did_play
                        c[1] += 1
            tally = defaultdict(int)
            for s in recent[tid]:
                for pid in s:
                    tally[pid] += 1
            regulars = [pid for pid, n in tally.items() if n >= 2]
            for pid in regulars:
                if not st.known:
                    group = "unknown (no covering report)"
                else:
                    s = st.status_of(pid)
                    group = ("implied_empty_team: " + s) if (st.implied_empty and s == ia.NOT_LISTED) else s
                    if s == ia.NOT_LISTED and st.skipped_unsubmitted:
                        group = "Not Listed (after skipping NOT YET SUBMITTED)"
                c = starters_by_state[group]
                c[0] += pid in did_play
                c[1] += 1
        for tid in (g["home_team_id"], g["away_team_id"]):
            recent[tid].append(set(starts[g["id"]][tid]))

    fmt = lambda d: {k: {"n": v[1], "played": v[0], "dnp_rate": round(1 - v[0] / v[1], 4) if v[1] else None}
                     for k, v in sorted(d.items())}
    return {"official_status_vs_played": fmt(by_status), "regular_starters_by_pregame_state": fmt(starters_by_state)}


def main() -> None:
    import argparse
    from ..db import cursor
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()
    with cursor() as cur:
        print(json.dumps(validate(cur, offset_min=args.offset), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
