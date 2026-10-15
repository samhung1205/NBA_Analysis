"""
Phase E.1 Telegram 推播 CLI（notify-v1）
------------------------------------------------------------
    python run_notify.py --test                          # 送一則測試訊息，確認 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 正確
    python run_notify.py --job actionable --dry-run      # 列出現在會推播的 ACTIONABLE 訊息（不送、不寫）
    python run_notify.py --job daily --dry-run [--date 2026-10-21]
    python run_notify.py --job system --dry-run          # 依 readiness FAIL 集合產生的告警（不送、不寫）
    python run_notify.py --job daily                     # 實際送出（寫 notification_events；排程會自動執行）

--dry-run 只讀資料庫；不印出 token / chat id。
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
    ap.add_argument("--test", action="store_true", help="送一則測試訊息")
    ap.add_argument("--job", choices=("actionable", "daily", "system"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="ISO8601（預設：現在）")
    ap.add_argument("--date", help="daily：台灣日期 YYYY-MM-DD（預設：明日）")
    a = ap.parse_args(argv)
    setup_logging()
    from core.config import settings
    from core.notify import job as nj
    from core.notify.telegram import TelegramError, client_from_settings

    client = client_from_settings(settings)
    if a.test:
        if client is None:
            print("未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
            return 1
        try:
            client.send("✅ NBA 預測平台：Telegram 推播設定成功（notify-v1 測試訊息）")
        except TelegramError as e:
            print(f"送出失敗：{e}", file=sys.stderr)
            return 1
        print("已送出測試訊息")
        return 0
    if not a.job:
        ap.error("請指定 --test 或 --job")
    now = parse_utc(a.now) if a.now else None
    if a.job == "actionable":
        rep = nj.actionable_job(now, dry_run=a.dry_run)
    elif a.job == "daily":
        rep = nj.daily_summary_job(now, dry_run=a.dry_run, day=date.fromisoformat(a.date) if a.date else None)
    else:
        rep = nj.system_alert_job(now, dry_run=a.dry_run, ignore_grace=True)
    json.dump(rep.__dict__, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
