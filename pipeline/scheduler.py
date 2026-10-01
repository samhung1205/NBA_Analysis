#!/usr/bin/env python
"""
Phase A-1：排程器入口（規格書 §5 排程設計，台灣時間）
------------------------------------------------------------
目前排上線的工作：
  - 每日 12:00：抓隔日 + 今日賽程/比分（今日順便補即時/賽後資料）
  - 每 30 分鐘：抓官方傷病報告

台彩盤口/國際盤（Phase D）、即時比分 60 秒輪詢、模型週重訓尚未實作，
待該階段開工時再掛上。開賽季後應將傷病輪詢頻率依規格書提升
（美東尖峰時段加密到每 15 分），目前先以固定 30 分鐘簡化處理。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.jobs.daily import fetch_injuries_job, fetch_schedule_and_scores_job
from core.logging_conf import setup_logging

log = logging.getLogger(__name__)


def _guarded(fn):
    def wrapper():
        try:
            fn()
        except Exception:
            log.exception("排程工作 %s 失敗", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def main() -> None:
    setup_logging()
    sched = BlockingScheduler(timezone="Asia/Taipei")

    sched.add_job(_guarded(fetch_schedule_and_scores_job), CronTrigger(hour=12, minute=0),
                  id="daily_schedule_scores", next_run_time=None)
    sched.add_job(_guarded(fetch_injuries_job), IntervalTrigger(minutes=30),
                  id="injuries_poll")

    log.info("排程器啟動：daily_schedule_scores (每日 12:00 TPE)、injuries_poll (每 30 分鐘)")
    log.info("立即執行一次以確認可正常運作...")
    _guarded(fetch_schedule_and_scores_job)()
    _guarded(fetch_injuries_job)()

    sched.start()


if __name__ == "__main__":
    main()
