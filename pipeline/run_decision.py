"""
Phase D.5 decision board CLI
------------------------------------------------------------
    python run_decision.py --dry-run                      # 實際注單結算 / ledger / 決策檢視（不寫入）
    python run_decision.py --dry-run --user 1 --date 2026-10-22 --summary
    python run_decision.py                                # 寫入（排程每分鐘自動執行）

只讀 / 寫 decision_snapshots、decision_opportunities、bankroll_day_snapshots、bankroll_ledger（settlement）、
bets 的結算 / bankroll basis 欄位；不登入 bookmaker、不建立任何下注、不修改 risk-v1 / execution-v1。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from core.logging_conf import setup_logging
from core.timeutil import parse_utc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="ISO8601（預設：現在）")
    ap.add_argument("--user", type=int, action="append", help="只物化指定 user id（可重複）")
    ap.add_argument("--date", action="append", help="betting day YYYY-MM-DD（可重複；預設今日 / 明日 / 48h 內）")
    ap.add_argument("--summary", action="store_true", help="dry-run 時只輸出摘要")
    a = ap.parse_args(argv)
    setup_logging()
    from core.decision.job import decision_board_job

    rep = decision_board_job(parse_utc(a.now) if a.now else None, dry_run=a.dry_run, user_ids=a.user,
                             days=[date.fromisoformat(d) for d in a.date] if a.date else None)
    out = rep.__dict__.copy()
    if a.summary:
        out["boards"] = [{"user_id": b["snapshot"]["user_id"], "betting_day": str(b["snapshot"]["betting_day"]),
                          "status": b["snapshot"]["status"], "summary": b["snapshot"]["summary"],
                          "actual_exposure": {k: b["snapshot"]["actual_exposure"].get(k) for k in
                                              ("day_stake", "day_fraction", "remaining_day_fraction", "incomplete")},
                          "bankroll": b["snapshot"]["bankroll"]} for b in rep.boards]
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
