"""
Phase C.5B：歷史傷病報告回填（官方 PDF，nbainjuries）
------------------------------------------------------------
只抓「對賽前特徵有用」的報告時間點（不窮舉每小時 × 全年）：

  對每個賽事日 D（ET）：
    - D-1 17:00（官方申報截止，賽事首次出現）與 D 10:00（當日早晨）
    - 對每個開賽時間 T：嚴格早於 T 的最後三份整點報告（T 之前 0~2 小時），
      2025-12-22 起報告為 15 分鐘制，再加上最後四份 15 分鐘報告
  → 供「開賽前最後已知狀態」與「開賽前 1~2 小時決策」兩種 as-of 使用。

特性：
  - 重用 C.5A 的 ET naive 時間、名稱正規化；每份報告以單一交易寫入完整快照（冪等、併發安全）。
  - 可續傳：已寫入的報告與已記錄 missing 的時間點直接跳過；--retry-missing 重探。
  - 單份報告失敗（下載/解析）只記錄，不中止；--shard i/N 可多行程並行（每個行程各有自己的 JVM）。
  - 不寫 injuries（API 讀取的最新狀態表）：歷史回填不應影響「今日傷病」顯示。
  - 只回填「已發生」的時間點，不會寫入未來資訊。
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Iterable

from ..db import cursor, fetch_log_keys, heartbeat, log_fetch, transaction
from ..injury_history import ingest_snapshot, load_directories, parse_report, reresolve_unmatched
from ..sources import injuries as injuries_src
from ..sources.injuries import NEW_FORMAT_START_ET, ReportNotPublished
from ..timeutil import et_naive_to_utc, now_utc, to_et_naive

log = logging.getLogger(__name__)

EARLIEST_ET = datetime(2021, 10, 18, 17, 0)     # 實測最早可取得的報告（2021-22 開季前一天）
HEARTBEAT_KEY = "nbainjuries_backfill"


def _floor(dt: datetime, minutes: int) -> datetime:
    return dt.replace(minute=(dt.minute // minutes) * minutes, second=0, microsecond=0)


def plan_report_times(tips_utc: Iterable[datetime], *, now: datetime | None = None,
                      hourly_back: int = 3, quarter_back: int = 4) -> list[datetime]:
    """賽事開賽時間（UTC）→ 要回填的報告時間點（ET naive，遞增、去重）。純函式。"""
    now_et = to_et_naive(now or now_utc())
    out: set[datetime] = set()
    for tip in tips_utc:
        tip_et = to_et_naive(tip)
        d = tip_et.replace(hour=0, minute=0, second=0, microsecond=0)
        out.add(d - timedelta(days=1) + timedelta(hours=17))     # D-1 17:00
        out.add(d + timedelta(hours=10))                           # D 10:00
        last = tip_et - timedelta(seconds=1)                       # 嚴格早於開賽
        h0 = _floor(last, 60)
        out.update(h0 - timedelta(hours=i) for i in range(hourly_back))
        if tip_et >= NEW_FORMAT_START_ET:
            q0 = _floor(last, 15)
            out.update(q0 - timedelta(minutes=15 * i) for i in range(quarter_back))
    return sorted(t for t in out if EARLIEST_ET <= t <= now_et)


def report_key(et: datetime) -> str:
    return f"{et:%Y-%m-%dT%H:%M} ET"


def load_plan(cur, seasons: list[str] | None = None) -> list[datetime]:
    if seasons:
        cur.execute("SELECT DISTINCT date_utc FROM games WHERE season = ANY(%s) AND status = 'final'"
                    " AND season_stage IN ('regular','playoffs','playin')", (seasons,))
    else:
        cur.execute("SELECT DISTINCT date_utc FROM games WHERE season_stage IN ('regular','playoffs','playin')")
    return plan_report_times(r["date_utc"] for r in cur.fetchall())


def run(plan: list[datetime], *, shard: tuple[int, int] = (0, 1), retry_missing: bool = False,
        limit: int | None = None, source: str = "nba_official", progress_every: int = 50,
        fetch: Callable[[datetime], list[dict]] = injuries_src.fetch_injury_report_strict,
        sleep: Callable[[float], None] = time.sleep) -> dict:
    idx, n = shard
    mine = [t for i, t in enumerate(plan) if i % n == idx]
    with cursor() as cur:
        cur.execute("SELECT report_time_utc FROM injury_reports WHERE source = %s", (source,))
        have = {r["report_time_utc"] for r in cur.fetchall()}
        skip_missing = set() if retry_missing else fetch_log_keys(cur, "injury_report", ("missing",))
        teams, players = load_directories(cur)

    todo = [t for t in mine if et_naive_to_utc(t) not in have and report_key(t) not in skip_missing]
    if limit:
        todo = todo[:limit]
    stats = {"planned": len(mine), "todo": len(todo), "ingested": 0, "already": len(mine) - len(todo),
             "missing": 0, "errors": 0, "entries": 0, "unmatched": 0, "failed": []}
    log.info("[shard %d/%d] 計畫 %d 份報告，已完成/已知不存在 %d，待處理 %d", idx + 1, n, len(mine),
             stats["already"], len(todo))
    t0 = time.time()

    for i, et in enumerate(todo, 1):
        key = report_key(et)
        try:
            try:
                rows = fetch(et)
            except ReportNotPublished:
                sleep(1.5)                       # 靜態檔偶發暫時性 403：再試一次才算 missing
                rows = fetch(et)
            parsed = parse_report(rows, et_naive_to_utc(et), teams, players)
            with transaction() as cur:
                rid = ingest_snapshot(cur, parsed, source=source)
                log_fetch(cur, kind="injury_report", key=key, status="ok")
            if rid:
                stats["ingested"] += 1
                stats["entries"] += len(parsed.entries)
                stats["unmatched"] += parsed.n_unmatched
        except ReportNotPublished as e:
            stats["missing"] += 1
            with cursor() as cur:
                log_fetch(cur, kind="injury_report", key=key, status="missing", error=str(e))
        except Exception as e:  # noqa: BLE001 — 單份報告下載/解析失敗不中止
            stats["errors"] += 1
            stats["failed"].append(key)
            log.warning("報告 %s 失敗：%s", key, str(e)[:200])
            try:
                with cursor() as cur:
                    log_fetch(cur, kind="injury_report", key=key, status="error", error=f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        if i % progress_every == 0 or i == len(todo):
            log.info("[shard %d/%d] 進度 %d/%d｜寫入 %d 份（%d 列，未對應 %d）｜不存在 %d｜錯誤 %d｜%.0fs",
                     idx + 1, n, i, len(todo), stats["ingested"], stats["entries"], stats["unmatched"],
                     stats["missing"], stats["errors"], time.time() - t0)
            try:
                with cursor() as cur:
                    heartbeat(cur, source_key=HEARTBEAT_KEY, display_name="NBA 官方傷病報告（歷史回填）",
                              category="injury", status="warn" if stats["errors"] else "ok",
                              records_updated=stats["ingested"],
                              error=f"{stats['errors']} 份失敗" if stats["errors"] else None)
            except Exception:  # noqa: BLE001
                pass
    return stats


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="歷史傷病報告回填")
    ap.add_argument("--seasons", nargs="+", default=None)
    ap.add_argument("--shard", default="1/1", help="i/N，例如 2/4（多行程並行）")
    ap.add_argument("--retry-missing", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    i, n = (int(x) for x in args.shard.split("/"))
    with cursor() as cur:
        plan = load_plan(cur, args.seasons)
    stats = run(plan, shard=(i - 1, n), retry_missing=args.retry_missing, limit=args.limit)
    with cursor() as cur:
        fixed = reresolve_unmatched(cur)
    log.info("完成：%s；重新對應球員 %d 列", {k: v for k, v in stats.items() if k != "failed"}, fixed)
    if stats["failed"]:
        log.warning("失敗的報告（重跑會重試）：%s", stats["failed"])


if __name__ == "__main__":
    main()
