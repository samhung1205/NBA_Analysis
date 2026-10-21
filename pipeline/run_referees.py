"""
Phase E.2 賽前裁判分派收集 CLI（referees-v1）
------------------------------------------------------------
    python run_referees.py --dry-run          # 抓官方頁 → 解析 → 與 DB 賽事比對（讀 DB，不寫入）
    python run_referees.py                    # 寫入 referee_assignment_reports / referee_assignments（排程自動執行）
    python run_referees.py --force            # 今天沒有未開賽比賽也照抓（例如補看頁面）

只發出一次 HTTP 請求（official.nba.com）；不連 stats.nba.com、不讀 HAR、不花 Odds API 額度。
"""
from __future__ import annotations

import argparse
import json
import sys

from core.logging_conf import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="略過「今天還有比賽」的檢查")
    a = ap.parse_args(argv)
    setup_logging()
    from core.jobs.referee_collect import collect_referees_job

    rep = collect_referees_job(dry_run=a.dry_run, force=a.force)
    json.dump(rep.__dict__, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()
    return 1 if rep.state == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
