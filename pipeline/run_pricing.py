#!/usr/bin/env python
"""
Phase D.2：盤口定價 CLI（去水 / 模型機率 / edge / EV；不做 Kelly / 推薦 / 下注金額）
------------------------------------------------------------
  python run_pricing.py                                   # 未來 48 小時未開賽比賽：最新盤口 × 當時最新有效預測 → 寫入
  python run_pricing.py --dry-run                         # 只計算、輸出 JSON，不寫入
  python run_pricing.py --game-id 123 --as-of 2026-10-21T22:00:00Z
                                                          # 歷史重建：只用該時點以前存在的盤口 / 預測 / artifact（不寫入）
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
    ap.add_argument("--now", help="live 定價的時點（UTC ISO8601；預設現在）")
    ap.add_argument("--game-id", type=int, help="歷史重建的比賽（需搭配 --as-of）")
    ap.add_argument("--as-of", help="歷史重建時點（UTC ISO8601）")
    args = ap.parse_args()
    setup_logging()

    from core.pricing.job import pricing_job, reconstruct_game

    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    if args.game_id is not None or args.as_of:
        if args.game_id is None or not args.as_of:
            ap.error("歷史重建需要同時指定 --game-id 與 --as-of")
        rep = reconstruct_game(args.game_id, parse(args.as_of))
    else:
        rep = pricing_job(parse(args.now) if args.now else None, dry_run=args.dry_run)
    print(json.dumps(asdict(rep), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
