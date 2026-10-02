#!/usr/bin/env python
"""
Phase A-1：排程器入口（規格書 §5 排程設計，台灣時間）
------------------------------------------------------------
排程定義在 core/scheduling.py（可獨立測試 next-run-time）。目前排上線的工作：
  - 每日 12:00：昨日 ET 殘留 + 今日 + 明日 賽程/比分/賽後結算
  - 每 5 分鐘：依 game_time_utc 刷新進行中 / 待結算賽事（無則不打外部來源）
  - 傷病報告：台灣 00:00~10:59 每 15 分、11:00~23:59 每 30 分

台彩盤口/國際盤（Phase D）、模型週重訓尚未實作。
啟動時每個 job 會「額外」立即跑一次作為 catch-up，之後完全依 trigger 排程。
"""
from __future__ import annotations

import argparse
import logging

from core.logging_conf import setup_logging
from core.scheduling import build_scheduler, job_specs, next_run_times

log = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-startup-run", action="store_true", help="啟動時不立即補跑一次")
    ap.add_argument("--print-next-runs", action="store_true", help="只印出各 job 下次觸發時間後離開")
    args = ap.parse_args()
    setup_logging()

    if args.print_next_runs:
        for job_id, t in next_run_times(job_specs()).items():
            print(f"{job_id:24s} {t.isoformat() if t else 'PAUSED/NEVER'}")
        return

    sched = build_scheduler(blocking=True, run_on_start=not args.no_startup_run)
    for job in sched.get_jobs():
        log.info("已登錄 job：%s — %s", job.id, job.name)
    sched.start()


if __name__ == "__main__":
    main()
