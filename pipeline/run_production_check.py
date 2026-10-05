#!/usr/bin/env python
"""
Production readiness（唯讀）：python run_production_check.py [--post-deploy] [--json] [--strict]

  不寫 DB、不呼叫任何外部 API（不花 The Odds API 額度）、不印 secret。
  狀態：PASS / WAITING（預期等待）/ WARN / BLOCKED（已知外部阻擋，如台彩 Cloudflare）/ FAIL。
  結束碼：有 FAIL → 1；--strict 且有 WARN → 2；否則 0。
  Railway 上可用 `railway run python run_production_check.py --post-deploy`，或在 service shell 內執行。
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-deploy", action="store_true", help="部署後模式：關鍵 job 心跳過期 → FAIL（預設只 WARN）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="有 WARN 也回傳非 0")
    args = ap.parse_args()

    try:   # 載入 pipeline/.env（若有）；缺 DATABASE_URL 時 import 會丟錯，由 readiness 回報 FAIL
        import core.config  # noqa: F401
    except RuntimeError:
        pass
    from core.production import readiness as rd
    checks = rd.run_checks(post_deploy=args.post_deploy)
    print(rd.to_json(checks) if args.json else rd.render_text(checks))
    overall = rd.worst(checks)
    if overall == rd.FAIL:
        return 1
    if args.strict and overall == rd.WARN:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
