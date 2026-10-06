"""
scheduler-efficiency-v1：game-aware 活動判斷（只讀；不改任何模型 / 定價 / 風控 / 執行語意）
------------------------------------------------------------
Railway 排程器 24/7 常駐；省的是「沒事可做時的外部請求與重運算」，不是關機。
每個高頻 job 先用便宜的、走索引的 Postgres 查詢判斷有沒有事，沒有就立刻返回（idle no-op 是正常狀態，不是錯誤）。

「production-eligible 比賽」沿用既有定義（production/inputs._SCHED_SQL）：status <> 'final' 且
season_stage ∈ HISTORY_STAGES（regular / playin / playoffs）；季前賽等不會讓排程變活躍。

傷病輪詢（以「下一場 eligible 且尚未開賽」的比賽為準；全部用 UTC 瞬間計算，所以不受夏令時間影響）：
    > 36h 或沒有比賽      不抓
    12h < t ≤ 36h        最多每 120 分鐘一次
    3h  < t ≤ 12h        最多每 60 分鐘一次
    0   < t ≤ 3h         最多每 15 分鐘一次
    比賽已開賽           該場不再觸發賽前傷病輪詢
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..timeutil import ensure_utc
from .features import HISTORY_STAGES

log = logging.getLogger(__name__)

POLICY_VERSION = "scheduler-efficiency-v1"
INJURY_WINDOW = timedelta(hours=36)
# (上界 inclusive, 最小輪詢間隔分鐘, tier 名稱)；由近到遠
INJURY_TIERS: tuple[tuple[timedelta, int, str], ...] = (
    (timedelta(hours=3), 15, "0-3h"),
    (timedelta(hours=12), 60, "3-12h"),
    (INJURY_WINDOW, 120, "12-36h"),
)
CADENCE_GRACE = timedelta(minutes=2)       # 排程在 15 分鐘網格上醒來；上一輪若晚幾秒結束，不能因此多等整整一格
DECISION_IDLE_FULL_REFRESH = timedelta(minutes=30)   # idle 時完整重算物化的安全網（內容不變時其餘分鐘只「確認」）


# ------------------------------------------------------------------ #
# 觀測：為什麼被略過（INFO 只在原因改變 / 每小時一次；其餘 DEBUG；計數另存）                  #
# ------------------------------------------------------------------ #

_LOG_STATE: dict[str, tuple[str, float, int]] = {}
COUNTERS: Counter = Counter()


def log_skip(job: str, reason: str, detail: str = "", *, every_s: float = 3600.0) -> None:
    COUNTERS[(job, reason)] += 1
    now = time.monotonic()
    prev = _LOG_STATE.get(job)
    if prev is None or prev[0] != reason or now - prev[1] >= every_s:
        n = COUNTERS[(job, reason)] - (prev[2] if prev and prev[0] == reason else 0)
        _LOG_STATE[job] = (reason, now, COUNTERS[(job, reason)])
        log.info("[scheduler-efficiency] %s skipped: %s%s%s", job, reason, f" {detail}" if detail else "",
                 f" (x{n} since last log)" if n > 1 else "")
    else:
        log.debug("[scheduler-efficiency] %s skipped: %s", job, reason)


def reset_observability() -> None:       # 測試用
    _LOG_STATE.clear()
    COUNTERS.clear()


# ------------------------------------------------------------------ #
# 傷病：純函式                                                           #
# ------------------------------------------------------------------ #

def injury_tier(until_next: timedelta | None) -> tuple[str, int | None]:
    """距離下一場 eligible 比賽的時間 → (tier 名稱, 最小間隔分鐘)；窗口外 → ('idle', None)。"""
    if until_next is None or until_next <= timedelta(0) or until_next > INJURY_WINDOW:
        return "idle", None
    for upper, minutes, name in INJURY_TIERS:
        if until_next <= upper:
            return name, minutes
    return "idle", None   # pragma: no cover


@dataclass(frozen=True)
class InjuryDecision:
    run: bool
    reason: str                 # run / idle_no_upcoming_game / outside_injury_window / cadence_not_due
    tier: str
    interval_min: int | None
    next_game_utc: datetime | None
    hours_to_next: float | None


def injury_poll_decision(next_game_utc: datetime | None, last_attempt_utc: datetime | None, now: datetime
                         ) -> InjuryDecision:
    now = ensure_utc(now)
    if next_game_utc is None:
        return InjuryDecision(False, "idle_no_upcoming_game", "idle", None, None, None)
    nxt = ensure_utc(next_game_utc)
    until = nxt - now
    hours = round(until.total_seconds() / 3600, 3)
    tier, interval = injury_tier(until)
    if interval is None:
        return InjuryDecision(False, "outside_injury_window", tier, None, nxt, hours)
    if last_attempt_utc is not None:
        elapsed = now - ensure_utc(last_attempt_utc)
        if elapsed < timedelta(minutes=interval) - CADENCE_GRACE:
            return InjuryDecision(False, "cadence_not_due", tier, interval, nxt, hours)
    return InjuryDecision(True, "run", tier, interval, nxt, hours)


# ------------------------------------------------------------------ #
# DB 讀取（全部走索引；不載入歷史資料）                                       #
# ------------------------------------------------------------------ #

def next_eligible_game(cur, now: datetime) -> datetime | None:
    """下一場尚未開賽的 production-eligible 比賽的開賽時間（沒有 → None）。"""
    cur.execute("SELECT MIN(date_utc) AS t FROM games WHERE status <> 'final' AND season_stage = ANY(%s) "
                "AND date_utc > %s", (list(HISTORY_STAGES), ensure_utc(now)))
    row = cur.fetchone()
    return ensure_utc(row["t"]) if row and row["t"] else None


def has_eligible_game_between(cur, start: datetime, end: datetime) -> bool:
    cur.execute("SELECT 1 FROM games WHERE status <> 'final' AND season_stage = ANY(%s) AND date_utc > %s "
                "AND date_utc <= %s LIMIT 1", (list(HISTORY_STAGES), ensure_utc(start), ensure_utc(end)))
    return cur.fetchone() is not None


def last_attempt(cur, source_key: str) -> datetime | None:
    cur.execute("SELECT last_attempt_at FROM data_sources WHERE source_key = %s", (source_key,))
    row = cur.fetchone()
    return ensure_utc(row["last_attempt_at"]) if row and row["last_attempt_at"] else None


def sync_expected_interval(cur, source_key: str, minutes: int | None) -> None:
    """讓 data_sources.expected_interval_min 反映目前輪詢 tier（idle → NULL，系統狀態頁不會因 idle 判成過期）。
    值沒變就不寫（IS DISTINCT FROM → 0 列）。"""
    cur.execute("UPDATE data_sources SET expected_interval_min = %s WHERE source_key = %s "
                "AND expected_interval_min IS DISTINCT FROM %s", (minutes, source_key, minutes))


def touch_heartbeat(cur, **meta: Any) -> None:
    """idle / 無新輸入的 no-op：只更新既有列的 last_attempt_at（上次 status 是 ok 時一併推進 last_success_at），
    不覆寫上次的 warn / error 與訊息（真正的問題不會被 idle 蓋掉）；列不存在才用一般 heartbeat 建立。"""
    cur.execute("UPDATE data_sources SET last_attempt_at = NOW(), "
                "last_success_at = CASE WHEN last_status = 'ok' THEN NOW() ELSE last_success_at END "
                "WHERE source_key = %s", (meta["source_key"],))
    if cur.rowcount == 0:
        from ..db import heartbeat
        heartbeat(cur, status="ok", records_updated=0, **meta)


def injury_state(cur, now: datetime) -> InjuryDecision:
    return injury_poll_decision(next_eligible_game(cur, now), last_attempt(cur, "nbainjuries"), now)


# ------------------------------------------------------------------ #
# 重訓：有沒有新的訓練資料                                                  #
# ------------------------------------------------------------------ #

def eligible_final_games_before(cur, limit: datetime) -> int:
    """與 production/inputs.load_history 相同的比賽集合（final、有比分、eligible stage、開賽 < limit）的筆數。"""
    cur.execute("SELECT COUNT(*) AS n FROM games WHERE status = 'final' AND season_stage = ANY(%s) "
                "AND home_pts IS NOT NULL AND away_pts IS NOT NULL AND date_utc < %s",
                (list(HISTORY_STAGES), ensure_utc(limit)))
    return int(cur.fetchone()["n"])


@dataclass(frozen=True)
class RetrainState:
    has_new_data: bool
    reason: str                  # new_training_data / no_new_training_data / no_current_artifact
    n_games_now: int | None = None
    n_games_artifact: int | None = None


def retrain_state(cur, now: datetime, manifest_n_games_history: int | None, *, buffer: timedelta) -> RetrainState:
    """目前 production artifact 訓練時用了 n_games_history 場；現在（cutoff = now）符合訓練條件的場數若沒增加 → 沒有新資料。
    無法判斷（沒有 artifact / manifest 缺欄位）一律視為有新資料（保守：照常重訓）。"""
    if manifest_n_games_history is None:
        return RetrainState(True, "no_current_artifact")
    n_now = eligible_final_games_before(cur, ensure_utc(now) - buffer)
    if n_now > int(manifest_n_games_history):
        return RetrainState(True, "new_training_data", n_now, int(manifest_n_games_history))
    return RetrainState(False, "no_new_training_data", n_now, int(manifest_n_games_history))


def current_artifact_n_games(root=None) -> int | None:
    import json

    from . import artifact as art
    try:
        v = art.current_version(root)
        if v is None:
            return None
        meta = json.loads((art.artifact_root(root) / "versions" / v / art.MANIFEST).read_text()).get("metadata", {})
        n = meta.get("n_games_history")
        return int(n) if n is not None else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------------ #
# 唯讀摘要（run_production_check 用；不寫入）                               #
# ------------------------------------------------------------------ #

def activity_summary(cur, now: datetime, *, root=None) -> dict[str, Any]:
    from datetime import timedelta as td

    from . import spec
    now = ensure_utc(now)
    nxt = next_eligible_game(cur, now)
    inj = injury_poll_decision(nxt, last_attempt(cur, "nbainjuries"), now)
    tier, interval = injury_tier(nxt - now if nxt else None)
    pricing_games = has_eligible_game_between(cur, now, now + td(hours=48))
    cur.execute("SELECT (SELECT COUNT(*) FROM paper_strategy_bets WHERE settlement_status IN ('pending','ungradable')) AS pb, "
                "(SELECT COUNT(*) FROM bets WHERE record_status = 'active' AND COALESCE(result,'pending') = 'pending') AS ab")
    r = cur.fetchone()
    rs = retrain_state(cur, now, current_artifact_n_games(root), buffer=spec.TRAINING_GAME_BUFFER)
    return {"policy": POLICY_VERSION, "next_game_utc": nxt, "hours_to_next": inj.hours_to_next,
            "injury_tier": tier, "injury_interval_min": interval, "injury_would_run_now": inj.run,
            "injury_reason": inj.reason, "pricing_window_has_games": pricing_games,
            "open_paper_bets": int(r["pb"]), "open_actual_bets": int(r["ab"]),
            "retrain_has_new_data": rs.has_new_data, "retrain_reason": rs.reason,
            "retrain_games_now": rs.n_games_now, "retrain_games_artifact": rs.n_games_artifact}
