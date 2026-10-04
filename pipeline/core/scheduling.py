"""
排程定義（台灣時間）
------------------------------------------------------------
把「有哪些 job、什麼時候跑」獨立出來，才能在不啟動排程器、不連資料庫的情況下
驗證 next-run-time（tests/test_scheduler.py）。

兩個曾經讓排程「看起來有啟動、實際永遠不跑」的坑：
  1. add_job(..., next_run_time=None) 在 APScheduler 3.x 的語意是「建立成暫停狀態」，
     job 永遠不會被觸發（只靠啟動時手動跑一次才有資料）。→ 不傳 next_run_time。
  2. CronTrigger(hour=12) 若沒指定 timezone，傳入 trigger 物件時**不會**套用 scheduler 的
     timezone，而是用機器本地時區（Docker/Railway 多半是 UTC → 變成台灣 20:00 才跑）。
     → 每個 CronTrigger 都明確帶 timezone=TPE。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .timeutil import TPE, now_utc

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobSpec:
    id: str
    func: Callable
    trigger: object
    description: str
    startup_catchup: bool = True   # run_on_start 時是否額外立即跑一次


def guarded(fn: Callable) -> Callable:
    """job 內任何例外都只記 log，不讓排程器行程中止。"""
    def wrapper():
        try:
            return fn()
        except Exception:  # noqa: BLE001
            log.exception("排程工作 %s 失敗", fn.__name__)
    wrapper.__name__ = fn.__name__
    return wrapper


def job_specs() -> list[JobSpec]:
    # 延遲 import：純排程時間測試不需要載入資料層
    from .jobs.daily import fetch_injuries_job, fetch_schedule_and_scores_job, refresh_live_and_final_job
    from .production.predict import early_prediction_job, final_prediction_job, injury_refresh_job
    from .production.retrain import retrain_job
    from .odds.ingest import oddsapi_odds_job, twsport_odds_job
    from .sizing.job import pricing_and_sizing_scheduled
    from .execution.ledger import paper_strategy_scheduled
    from .decision.job import decision_board_scheduled
    from .config import settings

    return [
        JobSpec("daily_schedule_scores", fetch_schedule_and_scores_job,
                CronTrigger(hour=12, minute=0, timezone=TPE),
                "每日 12:00（台灣）：昨日 ET 殘留 + 今日 + 明日賽程/比分/結算"),
        JobSpec("live_final_refresh", refresh_live_and_final_job,
                IntervalTrigger(minutes=5, timezone=TPE),
                "每 5 分鐘：依 game_time_utc 判斷有無進行中/待結算賽事，沒有就不打外部來源"),
        # 規格書 §5：比賽日每 30 分鐘，美東尖峰時段加密到每 15 分。
        # 美東 11:00~22:00 ≈ 台灣 00:00~11:00（夏令差 12 小時、冬令差 13 小時，取聯集）。
        JobSpec("injuries_peak", fetch_injuries_job,
                CronTrigger(hour="0-10", minute="*/15", timezone=TPE),
                "台灣 00:00~10:59 每 15 分鐘（美東白天至傍晚，官方報告集中發布）"),
        JobSpec("injuries_offpeak", fetch_injuries_job,
                CronTrigger(hour="11-23", minute="0,30", timezone=TPE),
                "台灣 11:00~23:59 每 30 分鐘", startup_catchup=False),
        # ---- C.5D：production 預測 / 重訓 ---- #
        JobSpec("predict_early", early_prediction_job,
                CronTrigger(hour=12, minute=20, timezone=TPE),
                "每日 12:20（台灣，賽程同步後）：未來 36 小時比賽的 early 預測（≈ ET 賽事日 00:00，前一晚傷病報告）"),
        JobSpec("predict_final", final_prediction_job,
                IntervalTrigger(minutes=5, timezone=TPE),
                "每 5 分鐘：開賽前 5~75 分鐘、尚未有 final 版本的比賽 → 開賽前約 60 分鐘重算"),
        JobSpec("predict_injury_refresh", injury_refresh_job,
                CronTrigger(minute="7,22,37,52", timezone=TPE),
                "每 15 分鐘（傷病抓取之後）：最後一次預測後有新傷病報告 → 重算，實質變化才寫入", startup_catchup=False),
        JobSpec("weekly_retrain", retrain_job,
                CronTrigger(day_of_week="mon", hour=16, minute=0, timezone=TPE),
                "每週一 16:00（台灣，美東凌晨無比賽）：重訓 → 檢查 → 原子寫入 → promote（失敗不影響現有模型）",
                startup_catchup=False),
        # ---- D.1：盤口快照（各來源獨立；失敗只寫心跳，不影響其他 job） ---- #
        JobSpec("odds_twsport", twsport_odds_job,
                CronTrigger(minute="3,33", timezone=TPE),
                "每 30 分鐘（:03 / :33）：台灣運彩 NBA 盤口快照（連續 3 次 blocked → 6 小時退避）"),
        JobSpec("odds_oddsapi", oddsapi_odds_job,
                CronTrigger(hour=settings.odds_api_cron_hours, minute=40, timezone=TPE),
                "每日 4 次（台灣 00/06/12/18:40，ODDS_API_CRON_HOURS 可調）：The Odds API 全場盤（3 credits/次，"
                "免費方案 500/月；先打不扣額度的 events，無比賽不花額度）", startup_catchup=False),
        # ---- D.2：定價（只讀 DB + 本機 artifact，不打外部來源；冪等） ---- #
        # ---- D.3：同一個 T 接著做 sizing（qualification / Kelly / risk-v1 exposure；不推薦、不寫 bets） ---- #
        JobSpec("market_pricing", pricing_and_sizing_scheduled,
                CronTrigger(minute="1-59/5", timezone=TPE),
                "每 5 分鐘（:01/:06/…，盤口 :03/:33/:40 與預測寫入之後）：最新盤口 × 當時最新有效預測 → "
                "去水 / 模型機率 / edge / EV（D.2）→ 同一 T 的理論注碼 sizing（D.3；不是推薦）"),
        # ---- D.4：prospective paper ledger（execution-v1；獨立 job，不讀寫 D.2 / D.3 的表、不寫 bets） ---- #
        JobSpec("paper_strategy", paper_strategy_scheduled,
                CronTrigger(minute="2-59/5", timezone=TPE),
                "每 5 分鐘（:02/:07/…）：已到 T = 開賽 − 60 分、尚未決策的比賽 → 以 T 為 as-of 重建定價 / sizing → "
                "記錄 decision（含 no_bet）與 paper bets（冪等、不重做；晚 > 15 分 → decision_window_missed）→ 結算已完賽的 paper bets"),
        # ---- D.5：決策中心物化（actual bets 納入 risk-v1 額度；bankroll ledger；Node 只讀） ---- #
        JobSpec("decision_board", decision_board_scheduled,
                CronTrigger(minute="*", second=30, timezone=TPE),
                "每分鐘（:30 秒）：實際注單 settle-v1 → bankroll ledger 損益同步 → 每位使用者 × betting day 的 "
                "decision board（台彩 D.3 理論機會 − 實際下注 exposure → 新增額度；內容不變只更新確認時間）"),
    ]


def register_jobs(sched: BaseScheduler, *, run_on_start: bool = False) -> list[JobSpec]:
    """把 job 掛到 scheduler。**不傳 next_run_time**，讓 trigger 自己算下一次；
    run_on_start=True 時只讓「第一次」立即觸發，之後仍照 trigger 排程。"""
    specs = job_specs()
    first_run = datetime.now(TPE) if run_on_start else None
    for s in specs:
        kwargs = {"next_run_time": first_run} if first_run and s.startup_catchup else {}
        sched.add_job(guarded(s.func), s.trigger, id=s.id, name=s.description,
                      replace_existing=True, **kwargs)
    return specs


def next_run_times(specs: list[JobSpec], now: datetime | None = None) -> dict[str, datetime | None]:
    """各 job 在 now 之後的下一次觸發時間（tz-aware）。不需啟動排程器。"""
    now = (now or now_utc()).astimezone(TPE)
    return {s.id: s.trigger.get_next_fire_time(None, now) for s in specs}


def build_scheduler(*, blocking: bool = True, run_on_start: bool = True) -> BaseScheduler:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler

    cls = BlockingScheduler if blocking else BackgroundScheduler
    sched = cls(timezone=TPE, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600})
    register_jobs(sched, run_on_start=run_on_start)
    return sched
