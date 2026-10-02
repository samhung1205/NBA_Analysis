"""
NBA 官方傷病報告 fetcher（套件 nbainjuries，解析官方 PDF）
------------------------------------------------------------
規格書 §1.1：2021-22 起有資料；賽前一日 17:00（ET）前申報，賽前會多次更新。

時間語意（C.5A 修正的根因）：
  nbainjuries 的 timestamp 是「美東當地時間的 naive datetime」——報告 PDF 檔名就是 ET
  （例如 Injury-Report_2026-01-15_05_30PM.pdf）。舊版直接傳 UTC，會差 4~5 小時，
  永遠找不到「剛發布」的報告。這裡一律：UTC → to_et_naive() → 傳給套件；
  DB 的 report_time_utc 再由 et_naive_to_utc() 轉回 UTC。

報告頻率：
  - 2025-12-22 09:00 ET 起為每 15 分鐘一份（檔名 HH_MMAM/PM）；
  - 之前為整點（檔名 HHAM/PM）。套件內部會把舊制的分鐘歸零，所以舊制時段只需探測整點。
  - 搜尋最近一份報告時以 interval_minutes（預設 15，可用 30 / 60）往回探測。

需要本機/容器有 Java 執行環境（套件透過 jpype+tabula 解析 PDF）。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Callable, Iterator

import pandas as pd

from ..timeutil import et_naive_to_utc, ensure_utc, to_et_naive

log = logging.getLogger(__name__)

# nbainjuries._util 的舊/新檔名制度分界（美東 naive）
NEW_FORMAT_START_ET = datetime(2025, 12, 22, 9, 0)
VALID_INTERVALS = (15, 30, 60)


def check_report_valid(ts_et: datetime) -> bool:
    """ts_et：美東 naive datetime。"""
    from nbainjuries import injury

    try:
        return bool(injury.check_reportvalid(ts_et))
    except Exception as e:  # noqa: BLE001
        log.debug("injury report 尚未發布 %s: %s", ts_et, e)
        return False


def fetch_injury_report(ts_et: datetime) -> list[dict]:
    """ts_et：美東 naive datetime。回傳 [{game_date, matchup, team, player_name, status, reason}]"""
    from nbainjuries import injury

    df: pd.DataFrame = injury.get_reportdata(ts_et, return_df=True)
    if df is None or df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        out.append({
            "game_date": row.get("Game Date"),
            "matchup": row.get("Matchup"),
            "team": row.get("Team"),
            "player_name": row.get("Player Name"),
            "status": row.get("Current Status"),
            "reason": row.get("Reason"),
        })
    return out


# ------------------------------------------------------------------ #
# 找最近一份已發布報告                                                   #
# ------------------------------------------------------------------ #

def candidate_report_times(now_utc: datetime, *, interval_minutes: int = 15,
                           lookback_hours: int = 6) -> Iterator[datetime]:
    """由新到舊產生要探測的美東 naive 時間點。

    在 UTC 軸上以 interval 步進（美東與 UTC 差整數小時，15/30/60 分鐘網格一致），
    再轉成 ET naive，因此不受夏令/冬令切換影響；秋季回撥重複的那一小時以 ET 值去重。
    舊制（整點）時段略過非整點候選。
    """
    if interval_minutes not in VALID_INTERVALS:
        raise ValueError(f"interval_minutes 必須是 {VALID_INTERVALS}，收到 {interval_minutes}")
    now = ensure_utc(now_utc)
    floored = now.replace(minute=(now.minute // interval_minutes) * interval_minutes,
                          second=0, microsecond=0)
    seen: set[datetime] = set()
    steps = lookback_hours * 60 // interval_minutes
    for i in range(steps + 1):
        et = to_et_naive(floored - timedelta(minutes=interval_minutes * i))
        if et in seen:
            continue
        seen.add(et)
        if et < NEW_FORMAT_START_ET and et.minute != 0:
            continue
        yield et


def find_latest_report(now_utc: datetime, *, interval_minutes: int = 15, lookback_hours: int = 6,
                       check: Callable[[datetime], bool] | None = None) -> datetime | None:
    """回傳最近一份已發布報告的美東 naive 時間；找不到回 None。"""
    check = check or check_report_valid
    for et in candidate_report_times(now_utc, interval_minutes=interval_minutes,
                                     lookback_hours=lookback_hours):
        if check(et):
            return et
    return None


def report_time_utc(ts_et: datetime) -> datetime:
    """報告的 ET naive 時間 → DB 要存的 aware UTC。"""
    return et_naive_to_utc(ts_et)


# ------------------------------------------------------------------ #
# 球員姓名對應                                                          #
# ------------------------------------------------------------------ #

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_player_name(raw) -> str | None:
    """官方報告是 'Last, First'（例 'Jackson Jr., Jaren'），球員表是 'First Last'
    （'Jaren Jackson Jr.'）。兩邊都轉成同一個比對 key：去重音、去標點、去 Jr./III 後綴。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    name = raw.strip()
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c)).lower()
    name = re.sub(r"[.'’`]", "", name)
    name = re.sub(r"[^a-z0-9\s-]", " ", name)
    tokens = [t for t in name.split() if t not in _SUFFIXES]
    return " ".join(tokens) or None
