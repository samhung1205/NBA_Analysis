"""
重算 / 補算 team_game_derived（Phase C.5B）
------------------------------------------------------------
新比賽的衍生指標在寫入 box score 時就一併計算（jobs/box_store.py）。這支 job 負責：
  - 補算：有原始計數但缺衍生指標的比賽（例如先前只寫了原始計數）
  - 公式版本升級：metrics.FORMULA_VERSION 改變後整批重算（--force 可無視版本重算全部）
完全由 DB 內的基本 box score 計算，不呼叫任何外部來源。冪等。
"""
from __future__ import annotations

import argparse
import logging

from .. import metrics
from ..db import cursor, upsert_team_derived

log = logging.getLogger(__name__)

_RAW = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "tov", "ast", "team_min")


def games_needing_derive(cur, *, force: bool = False) -> list[dict]:
    cur.execute(
        f"""
        SELECT g.id AS game_id, g.home_team_id, g.away_team_id,
               {', '.join(f'h.{c} AS h_{c}' for c in _RAW)},
               {', '.join(f'a.{c} AS a_{c}' for c in _RAW)}
          FROM games g
          JOIN team_game_stats h ON h.game_id = g.id AND h.team_id = g.home_team_id
          JOIN team_game_stats a ON a.game_id = g.id AND a.team_id = g.away_team_id
         WHERE h.fga IS NOT NULL AND a.fga IS NOT NULL
           AND (%s OR (SELECT count(*) FROM team_game_derived d
                        WHERE d.game_id = g.id AND d.formula_version = %s AND d.ortg IS NOT NULL) < 2)
         ORDER BY g.date_utc
        """,
        (force, metrics.FORMULA_VERSION),
    )
    return cur.fetchall()


def derive_all(cur, *, force: bool = False) -> dict[str, int]:
    done = skipped = 0
    for row in games_needing_derive(cur, force=force):
        home = {c: row[f"h_{c}"] for c in _RAW}
        away = {c: row[f"a_{c}"] for c in _RAW}
        if not (metrics.has_raw(home) and metrics.has_raw(away)):
            skipped += 1
            continue
        h, a = metrics.derive_game(home, away)
        upsert_team_derived(cur, game_id=row["game_id"], team_id=row["home_team_id"],
                            formula_version=metrics.FORMULA_VERSION, **h)
        upsert_team_derived(cur, game_id=row["game_id"], team_id=row["away_team_id"],
                            formula_version=metrics.FORMULA_VERSION, **a)
        done += 1
    return {"derived": done, "skipped_incomplete_raw": skipped}


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="補算/重算 team_game_derived")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    with cursor() as cur:
        log.info("結果：%s（公式版本 %s）", derive_all(cur, force=args.force), metrics.FORMULA_VERSION)


if __name__ == "__main__":
    main()
