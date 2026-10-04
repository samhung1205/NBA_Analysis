#!/usr/bin/env python
"""
Phase D.3：理論注碼 CLI（qualification / push-aware Kelly / risk-v1 exposure caps；不是投注推薦、不寫入 bets）
------------------------------------------------------------
  python run_sizing.py --dry-run                      # T = 現在：只計算、輸出 JSON，不寫入
  python run_sizing.py                                # 寫入 bet_sizing_snapshots（冪等）
  python run_sizing.py --dry-run --bankroll 10000     # 另外輸出 stake_amount = bankroll × final_stake_fraction（不寫入 DB）
  python run_sizing.py --dry-run --now 2026-10-21T22:00:00Z
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime

from core.logging_conf import setup_logging


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="sizing 時點 T（UTC ISO8601；預設現在）")
    ap.add_argument("--bankroll", type=float, help="只用於輸出 stake_amount；DB 只存 bankroll 比例")
    args = ap.parse_args()
    setup_logging()

    from core.sizing.job import sizing_job

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    if args.bankroll is not None and not args.bankroll >= 0:
        ap.error("--bankroll 必須 ≥ 0")
    rep = sizing_job(now, dry_run=args.dry_run, bankroll=args.bankroll)
    print(json.dumps(asdict(rep), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
