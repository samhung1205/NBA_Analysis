#!/usr/bin/env python
"""
Phase D.4：execution-v1 策略回測 CLI（只用 DB 真實觀測的盤口快照；不造盤口、不回填）
------------------------------------------------------------
  python run_strategy_backtest.py --source twsport --from 2026-10-20 --to 2026-12-31
  python run_strategy_backtest.py --source oddsapi --bookmaker pinnacle --from 2026-10-20 --to 2026-12-31   # 國際盤診斷
  python run_strategy_backtest.py --fixture                     # 合成 validation slate（validation_only；不是 ROI 證據）
  python run_strategy_backtest.py ... --starting-bankroll 10000 # 只換算顯示金額；選擇邏輯一律用 normalized bankroll 1.0

期間沒有任何真實盤口 → historical_evidence_available = false（不輸出 ROI = 0）。
台彩（twsport）與 Odds API 各 bookmaker 分開評估；不做 best-book / consensus。不寫入任何資料表。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from core.logging_conf import setup_logging


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["twsport", "oddsapi"], default="twsport")
    ap.add_argument("--bookmaker", help="oddsapi 必填（單一 bookmaker key）；twsport 固定 twsport")
    ap.add_argument("--from", dest="date_from", help="betting day（Asia/Taipei，YYYY-MM-DD）")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--starting-bankroll", type=float, default=1.0, help="只用於顯示金額換算")
    ap.add_argument("--fixture", action="store_true", help="執行合成 validation slate（validation_only）")
    ap.add_argument("--summary", action="store_true", help="不輸出逐場 decisions / days")
    ap.add_argument("--out", help="把完整 JSON 寫到檔案")
    args = ap.parse_args()
    setup_logging()
    if not args.starting_bankroll > 0:
        ap.error("--starting-bankroll 必須 > 0")

    if args.fixture:
        from core.execution.fixture import run_fixture_validation
        res = run_fixture_validation(starting_bankroll=args.starting_bankroll)
    else:
        if not (args.date_from and args.date_to):
            ap.error("需要 --from / --to（或 --fixture）")
        from core.execution.backtest import run_strategy_backtest
        from core.execution.policy import StrategyScope
        if args.source == "oddsapi" and not args.bookmaker:
            ap.error("--source oddsapi 必須指定單一 --bookmaker（不做 best-book / consensus）")
        scope = StrategyScope(args.source, args.bookmaker or "twsport")
        res = run_strategy_backtest(scope, date.fromisoformat(args.date_from), date.fromisoformat(args.date_to),
                                    starting_bankroll=args.starting_bankroll)
    if args.summary:
        res = {k: v for k, v in res.items() if k not in ("decisions", "days")}
    text = json.dumps(res, ensure_ascii=False, indent=1, default=str)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
