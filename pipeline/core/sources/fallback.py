"""
資料來源 fallback 鏈 + 簡易斷路器 + 來源心跳
------------------------------------------------------------
規格書 §1.1：stats.nba.com 會封鎖/逾時，不可讓整條 pipeline 因單一來源中止。
這裡提供三件事：

  1. fetch_with_fallback()：依序嘗試來源鏈，回傳第一個成功者，並保留每個來源的
     嘗試紀錄（成功/失敗/被斷路器跳過、錯誤、耗時），讓「用了哪個來源、為什麼
     fallback」可追蹤。全部失敗才丟 AllSourcesFailed。
  2. CircuitBreaker：同一來源連續失敗 N 次後，冷卻期內直接跳過，避免 stats.nba.com
     被封鎖時每次排程都白等「逾時 × 重試」。這不是繞過封鎖，而是「別再打」。
  3. report_attempts()：把嘗試結果寫入 data_sources 心跳（系統狀態頁）：
       - 勝出來源 → ok；若為備援，last_error 註記「備援 primary」
       - 被備援取代的來源 → warn（降級但資料仍有人補上）
       - 全部失敗 → error
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

SOURCE_DISPLAY = {
    "nba_cdn": ("NBA CDN（cdn.nba.com 即時資料）", "games"),
    "nba_api": ("NBA Stats API (stats.nba.com)", "stats"),
    "espn": ("ESPN 隱藏 API（備援）", "stats"),
    "nbainjuries": ("NBA 官方傷病報告", "injury"),
}

# 備援來源平常不會被呼叫，過期門檻放寬，避免系統狀態頁誤報 stale
SOURCE_EXPECTED_INTERVAL_MIN = {
    "nba_cdn": 60 * 24,
    "nba_api": 60 * 24 * 7,
    "espn": 60 * 24 * 7,
    "nbainjuries": 30,
}


class AllSourcesFailed(RuntimeError):
    def __init__(self, label: str, attempts: list["Attempt"]):
        self.label, self.attempts = label, attempts
        detail = "; ".join(f"{a.source}:{a.status}" + (f"({a.error})" if a.error else "") for a in attempts)
        super().__init__(f"{label}：所有來源皆失敗 → {detail}")


@dataclass
class Attempt:
    source: str
    status: str          # ok | error | skipped
    error: str | None = None
    elapsed_s: float = 0.0
    records: int | None = None


@dataclass
class FetchResult:
    label: str
    data: Any
    source: str                       # 實際提供資料的來源
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def primary(self) -> str:
        return self.attempts[0].source

    @property
    def fallback_used(self) -> bool:
        return self.source != self.primary


class CircuitBreaker:
    """同一來源連續失敗 threshold 次 → 開路 cooldown_s 秒（期間直接跳過）。"""

    def __init__(self, threshold: int = 2, cooldown_s: float = 600.0,
                 clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown_s, self._clock = threshold, cooldown_s, clock
        self._fails: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, source: str) -> bool:
        opened = self._opened_at.get(source)
        if opened is None:
            return False
        if self._clock() - opened >= self.cooldown_s:
            # 冷卻結束：半開，允許再試一次；再失敗會立刻重新開路
            del self._opened_at[source]
            self._fails[source] = self.threshold - 1
            return False
        return True

    def record_success(self, source: str) -> None:
        self._fails.pop(source, None)
        self._opened_at.pop(source, None)

    def record_failure(self, source: str) -> None:
        n = self._fails.get(source, 0) + 1
        self._fails[source] = n
        if n >= self.threshold:
            self._opened_at[source] = self._clock()

    def reset(self) -> None:
        self._fails.clear()
        self._opened_at.clear()


default_breaker = CircuitBreaker()


def fetch_with_fallback(label: str, chain: list[tuple[str, Callable[[], Any]]],
                        breaker: CircuitBreaker | None = None) -> FetchResult:
    """chain: [(source_key, zero-arg callable)]，第一個是 primary。
    callable 回傳 list/dict；回傳空 list 視為成功（今天可能真的沒有比賽）。"""
    breaker = breaker or default_breaker
    attempts: list[Attempt] = []
    for source, fn in chain:
        if breaker.is_open(source):
            attempts.append(Attempt(source, "skipped", "斷路器開啟（近期連續失敗，冷卻中）"))
            log.warning("[%s] 來源 %s 斷路器開啟，跳過", label, source)
            continue
        t0 = time.monotonic()
        try:
            data = fn()
        except Exception as e:  # noqa: BLE001 — 任何來源錯誤都只是「換下一個」
            breaker.record_failure(source)
            attempts.append(Attempt(source, "error", f"{type(e).__name__}: {e}"[:300],
                                    time.monotonic() - t0))
            log.warning("[%s] 來源 %s 失敗：%s", label, source, attempts[-1].error)
            continue
        breaker.record_success(source)
        n = len(data) if hasattr(data, "__len__") else None
        attempts.append(Attempt(source, "ok", None, time.monotonic() - t0, n))
        result = FetchResult(label, data, source, attempts)
        if result.fallback_used:
            log.warning("[%s] 使用備援來源 %s（primary %s 不可用）", label, source, result.primary)
        return result
    raise AllSourcesFailed(label, attempts)


def report_attempts(label: str, attempts: list[Attempt], *, optional: bool = False) -> None:
    """把嘗試紀錄寫進 data_sources（延遲 import db，讓純邏輯測試不需連線）。
    optional=True：該資料缺了不影響主流程（如進階數據），全部失敗只標 warn 不標 error。"""
    from ..db import cursor, heartbeat

    winner = next((a for a in attempts if a.status == "ok"), None)
    primary = attempts[0].source if attempts else None
    with cursor() as cur:
        for a in attempts:
            name, category = SOURCE_DISPLAY.get(a.source, (a.source, "stats"))
            interval = SOURCE_EXPECTED_INTERVAL_MIN.get(a.source)
            if a.status == "skipped":
                continue  # 沒有實際嘗試，不動心跳
            if a.status == "ok":
                note = None if a.source == primary else f"備援 {primary}（{label}）"
                heartbeat(cur, source_key=a.source, display_name=name, category=category,
                          status="ok", error=note, records_updated=a.records,
                          expected_interval_min=interval)
            else:
                covered = winner is not None
                msg = f"[{label}] {a.error}" + (f"；已由 {winner.source} 備援" if covered else "")
                heartbeat(cur, source_key=a.source, display_name=name, category=category,
                          status="warn" if (covered or optional) else "error", error=msg[:500],
                          expected_interval_min=interval)
