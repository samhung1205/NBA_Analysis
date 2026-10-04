#!/usr/bin/env python
"""
Phase A-1：排程器入口（規格書 §5 排程設計，台灣時間）
------------------------------------------------------------
排程定義在 core/scheduling.py（可獨立測試 next-run-time）。目前排上線的工作：
  - 每日 12:00：昨日 ET 殘留 + 今日 + 明日 賽程/比分/賽後結算
  - 每 5 分鐘：依 game_time_utc 刷新進行中 / 待結算賽事（無則不打外部來源）
  - 傷病報告：台灣 00:00~10:59 每 15 分、11:00~23:59 每 30 分
  - 預測（C.5D）：每日 12:20 early、每 5 分鐘檢查開賽前 ~60 分鐘 final、每 15 分鐘傷病觸發 refresh
  - 模型重訓：每週一 16:00（原子寫入新 artifact，失敗不影響現有模型）

  - 盤口快照（D.1）：台彩每 30 分鐘（:03/:33）、The Odds API 每日 4 次（00/06/12/18:40，不在啟動時補跑）
  - 定價 + 理論注碼（D.2 / D.3）：每 5 分鐘（:01/:06/…）
  - Paper strategy ledger（D.4 execution-v1）：每 5 分鐘（:02/:07/…）記錄 T-60 decision、結算已完賽的 paper bets
各 job 可單獨執行：python run_predict.py --kind early|final|refresh、python run_retrain.py（--list / --rollback）、
python run_odds.py --source twsport|oddsapi [--dry-run]。
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
