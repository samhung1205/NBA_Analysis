"""
時間與日期邊界工具（UTC / America/New_York / Asia/Taipei）
------------------------------------------------------------
三個「日期」概念必須分清楚，混用就是漏賽的根源：

  - DB：一律 UTC（games.date_utc、injuries.report_time_utc 皆 TIMESTAMPTZ）。
  - NBA / ESPN 的「賽事日期」：以美東 (ET) 的日曆日為準（scoreboard 的 gameDate）。
  - 使用者看的「今日/明日」：台灣時間（階段一 src/lib/time.ts 同一套邏輯）。

台灣早上 10:00 = 美東前一天 21:00（夏令）或 22:00（冬令）：此時台灣的「今天」
裡仍有 ET「昨天」的比賽正在進行或剛結束。因此排程不能把「台灣今天的日期字串」
直接丟給 ET 日期的 API，而是先算出 UTC 視窗，再換算視窗涵蓋哪些 ET 日期；
最後是否屬於某一天，一律以比賽實際的 game_time_utc 判定。

nbainjuries 套件的 timestamp 是「美東當地時間的 naive datetime」（報告檔名就是 ET），
傳 UTC 會差 4~5 小時，見 to_et_naive()。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")


def now_utc() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """naive 視為 UTC；aware 轉成 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_utc(s: str | datetime | None) -> datetime | None:
    """解析 ISO8601（含結尾 Z）為 aware UTC datetime。"""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return ensure_utc(s)
    return ensure_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))


# ------------------------------------------------------------------ #
# 台灣 / 美東 日期                                                      #
# ------------------------------------------------------------------ #

def tpe_date(dt: datetime | None = None) -> date:
    return ensure_utc(dt or now_utc()).astimezone(TPE).date()


def et_date(dt: datetime) -> date:
    """某個時間點（任意時區）對應的美東日曆日。"""
    return ensure_utc(dt).astimezone(ET).date()


def tpe_day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """台灣日期 d 的 [00:00, 次日 00:00) 對應的 UTC 區間。"""
    start = datetime.combine(d, time.min, tzinfo=TPE)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def et_dates_between(start_utc: datetime, end_utc: datetime) -> list[date]:
    """UTC 區間 [start, end) 涵蓋的所有美東日曆日（遞增）。"""
    start, end = ensure_utc(start_utc), ensure_utc(end_utc)
    if end <= start:
        return [et_date(start)]
    first, last = et_date(start), et_date(end - timedelta(microseconds=1))
    out, d = [], first
    while d <= last:
        out.append(d)
        d += timedelta(days=1)
    return out


def refresh_window_utc(now: datetime | None = None, lookback_hours: int = 24) -> tuple[datetime, datetime]:
    """排程每次要刷新的比賽視窗（UTC）：
    [now - lookback, 台灣「明天」結束)。

    - 往回 24 小時：涵蓋台灣早上仍在進行/剛結束的 ET 前一天賽事（即時比分與賽後結算）。
    - 往後到台灣明天結束：涵蓋「隔日（台灣時間）」賽程，這是平台主要用途。
    """
    now = ensure_utc(now or now_utc())
    tomorrow = tpe_date(now) + timedelta(days=1)
    return now - timedelta(hours=lookback_hours), tpe_day_bounds_utc(tomorrow)[1]


def et_dates_for_refresh(now: datetime | None = None) -> list[date]:
    start, end = refresh_window_utc(now)
    return et_dates_between(start, end)


def in_window(game_time_utc: datetime | None, start_utc: datetime, end_utc: datetime) -> bool:
    """以比賽實際開賽時間（game_time_utc）判定是否落在視窗內。"""
    if game_time_utc is None:
        return False
    t = ensure_utc(game_time_utc)
    return ensure_utc(start_utc) <= t < ensure_utc(end_utc)


# ------------------------------------------------------------------ #
# nbainjuries（美東 naive datetime）                                    #
# ------------------------------------------------------------------ #

def to_et_naive(dt_utc: datetime) -> datetime:
    """UTC → 美東當地時間的 naive datetime（nbainjuries 要的格式）。"""
    return ensure_utc(dt_utc).astimezone(ET).replace(tzinfo=None)


def et_naive_to_utc(dt_et_naive: datetime) -> datetime:
    """美東當地 naive datetime → aware UTC（DB 儲存用）。
    秋季回撥重複的那一小時（01:00~01:59）取第一次（EDT, fold=0）。"""
    return dt_et_naive.replace(tzinfo=ET, fold=0).astimezone(UTC)
