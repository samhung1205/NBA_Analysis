"""
現役名單同步 CLI（roster-sync-v1）
------------------------------------------------------------
    python run_roster_sync.py --dry-run     # 抓名單並列出轉隊 / 新增 / 離隊 / 無法對應（只讀 DB，不寫）
    python run_roster_sync.py               # 單一交易套用到 players.team_id（需先套用 migration 0012）

主來源 stats.nba.com playerindex，失敗時改用 ESPN（只更新姓名唯一對應的既有球員）。
"""
from __future__ import annotations

import argparse
import sys

from core.logging_conf import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    setup_logging()
    from core.jobs.roster_sync import sync_rosters_job

    plan = sync_rosters_job(dry_run=a.dry_run)
    if plan is None:
        return 1
    print(plan.summary())
    for _, name, old, new in sorted(plan.moved, key=lambda m: (m[3], m[1])):
        print(f"  轉隊  {name:<28} {old or '—':>4} → {new}")
    for e in sorted(plan.added, key=lambda e: (e.team_abbr, e.name)):
        print(f"  新增  {e.name:<28} {e.team_abbr}")
    for _, name, old in sorted(plan.released, key=lambda r: (r[2] or "", r[1])):
        print(f"  離隊  {name:<28} {old or '—':>4} → （不在現役名單）")
    for e in plan.unmatched:
        print(f"  無法對應  {e.name:<24} {e.team_abbr}")
    print("（dry-run：未寫入）" if a.dry_run else "已套用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
