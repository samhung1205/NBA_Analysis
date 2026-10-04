#!/usr/bin/env python
"""
Phase D.4：prospective paper-decision ledger CLI（execution-v1；不寫 bets、不自動下注）
------------------------------------------------------------
  python run_paper.py --dry-run                       # 到期（T-60 已過）的比賽：以 T 重建、決策，只輸出不寫入
  python run_paper.py                                 # 寫入 paper_strategy_decisions / _bets（冪等；已有 decision 不重做）
  python run_paper.py --now 2026-10-21T22:02:00Z      # 指定 job 執行時間（analysis_as_of 仍是各場 T）
  python run_paper.py --settle-only [--dry-run]       # 只結算 pending / ungradable paper bets
  python run_paper.py --report [--starting-bankroll 10000]   # prospective performance（ledger → metrics）
  python run_paper.py --scope oddsapi:pinnacle ...    # 國際盤診斷 strategy（另一個 strategy_id；不是台彩）
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
    ap.add_argument("--now", help="job 執行時間（UTC ISO8601；預設現在）")
    ap.add_argument("--scope", action="append", help="twsport（預設）或 oddsapi:<bookmaker>；可重複")
    ap.add_argument("--settle-only", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--starting-bankroll", type=float, default=1.0, help="--report 顯示金額換算用")
    args = ap.parse_args()
    setup_logging()

    from core.execution import ledger
    from core.execution.policy import PRIMARY_SCOPE, parse_scope

    scopes = tuple(parse_scope(s) for s in args.scope) if args.scope else (PRIMARY_SCOPE,)
    if args.report:
        out = [ledger.paper_performance(s, starting_bankroll=args.starting_bankroll) for s in scopes]
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        return
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    rep = None
    if not args.settle_only:
        rep = ledger.paper_decision_job(now, scopes=scopes, dry_run=args.dry_run)
    rep = ledger.paper_settlement_job(now, dry_run=args.dry_run, rep=rep)
    print(json.dumps(asdict(rep), ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
