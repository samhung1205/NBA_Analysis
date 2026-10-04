"""
盤口抓取工作（Phase D.1）：fetch → parse → normalize → match game → persist snapshots → run log / heartbeat
------------------------------------------------------------
每個來源獨立執行（台彩失敗不影響 Odds API，也不影響賽程 / 傷病 / 預測 job）：

  run_odds_job("twsport" | "oddsapi", dry_run=False)

dry_run：照常抓取、解析、讀 DB 做比對，但**不寫任何東西**（快照、對應、抓取紀錄、心跳都不寫）。
outcome（odds_fetch_runs.outcome / data_sources.last_outcome）見 errors.OUTCOME_STATUS：
  success / partial / no_nba_markets / parser_changed / quota_low / rate_limited / auth_failed /
  network_failure / blocked / not_configured / error
「今天沒有盤」（no_nba_markets，ok）與「parser 壞掉」（parser_changed，error）絕不會同時是 records=0 / ok。
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from ..timeutil import ensure_utc, now_utc, refresh_window_utc
from . import oddsapi, twsport
from .errors import OUTCOME_STATUS, OddsSourceError, ParserChanged
from .matching import MATCHED, WIDE_WINDOW, CandidateGame, ExistingLink, match_event

log = logging.getLogger(__name__)

SOURCES = {
    "twsport": {"display_name": "台灣運彩盤口（Playwright）", "expected_interval_min": 30},
    "oddsapi": {"display_name": "The Odds API（國際盤）", "expected_interval_min": 6 * 60},
}
BLOCKED_BACKOFF = timedelta(hours=6)       # 連續 3 次 blocked → 6 小時內不再開瀏覽器（尊重對方、避免反覆觸發）
RAW_SAMPLE_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "odds_raw"
RAW_SAMPLE_KEEP = 30


@dataclass
class RunReport:
    source: str
    outcome: str = "error"
    dry_run: bool = False
    run_id: int | None = None
    fetched_at: datetime | None = None
    latency_ms: int | None = None
    n_requests: int | None = None
    n_events: int = 0
    n_matched: int = 0
    n_unmatched: int = 0
    n_ambiguous: int = 0
    n_rejected: int = 0
    n_markets: int = 0
    n_markets_invalid: int = 0
    n_snapshots_new: int = 0
    n_snapshots_unchanged: int = 0
    n_snapshots_stale: int = 0
    quota_remaining: int | None = None
    quota_used: int | None = None
    quota_last: int | None = None
    error: str | None = None
    market_counts: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    skipped: str | None = None

    def summary(self) -> dict:
        d = asdict(self)
        d["fetched_at"] = self.fetched_at.isoformat() if self.fetched_at else None
        return d


# ------------------------------------------------------------------ #
# 抓取（可注入，測試 / 檔案匯入用）                                        #
# ------------------------------------------------------------------ #

def _fetch_twsport(report: RunReport) -> dict:
    c = twsport.TwsportClient(channel=settings.twsport_browser_channel, headless=settings.twsport_headless)
    try:
        with c:
            return twsport.fetch_raw(c)
    finally:
        report.n_requests = c.n_requests


def _fetch_oddsapi(report: RunReport, now: datetime) -> dict:
    client = oddsapi.OddsApiClient(settings.odds_api_key, regions=settings.odds_api_regions,
                                   bookmakers=settings.odds_api_bookmakers,
                                   quota_reserve=settings.odds_api_quota_reserve)
    try:
        return oddsapi.fetch_raw(client, now=now, include_h1=settings.odds_api_include_h1)
    finally:
        report.n_requests = client.n_requests
        report.quota_remaining, report.quota_used, report.quota_last = (
            client.quota.remaining, client.quota.used, client.quota.last)


def _parse(source: str, raw: dict):
    return (twsport.parse_raw if source == "twsport" else oddsapi.parse_raw)(raw)


# ------------------------------------------------------------------ #
# DB 讀取（dry-run 也會讀）                                               #
# ------------------------------------------------------------------ #

def schema_ready(cur) -> bool:
    """migrations/postgres/0004_phase_d1.sql 是否已套用。"""
    cur.execute("SELECT to_regclass('odds_event_links') IS NOT NULL AND to_regclass('odds_fetch_runs') IS NOT NULL AS ok")
    return bool(cur.fetchone()["ok"])


def load_context(cur, source: str, events) -> tuple[list[CandidateGame], dict[str, int], dict[str, dict]]:
    cur.execute("SELECT id, abbr FROM teams")
    team_ids = {r["abbr"]: r["id"] for r in cur.fetchall()}
    if not events:
        return [], team_ids, {}
    times = [ensure_utc(e.commence_utc) for e in events]
    cur.execute(
        "SELECT id, nba_game_id, home_team_id, away_team_id, date_utc, status, season_stage FROM games"
        " WHERE date_utc BETWEEN %s AND %s",
        (min(times) - WIDE_WINDOW, max(times) + WIDE_WINDOW))
    games = [CandidateGame(r["id"], r["home_team_id"], r["away_team_id"], r["date_utc"], r["status"],
                           r["season_stage"], r["nba_game_id"]) for r in cur.fetchall()]
    from .store import load_links
    links = load_links(cur, source, [e.source_event_id for e in events]) if schema_ready(cur) else {}
    return games, team_ids, links


def _save_raw_sample(source: str, raw: Any, when: datetime) -> str | None:
    try:
        d = RAW_SAMPLE_DIR / source
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{when:%Y%m%dT%H%M%SZ}.json.gz"
        data = json.dumps(raw, ensure_ascii=False, default=str).encode()[: 4 * 1024 * 1024]
        p.write_bytes(gzip.compress(data))
        for old in sorted(d.glob("*.json.gz"))[:-RAW_SAMPLE_KEEP]:
            old.unlink()
        return str(p)
    except Exception:  # noqa: BLE001
        log.exception("保存 raw 樣本失敗")
        return None


# ------------------------------------------------------------------ #
# 主流程                                                                #
# ------------------------------------------------------------------ #

def run_odds_job(source: str, *, dry_run: bool = False, now: datetime | None = None,
                 fetch: Callable[[RunReport], dict] | None = None, fetched_at: datetime | None = None,
                 db=None) -> RunReport:
    """fetch：注入的抓取函式（測試 / 檔案匯入）；fetched_at：匯入檔案時的實際觀測時間。"""
    if source not in SOURCES:
        raise ValueError(f"未知來源 {source}")
    if db is None:
        from .. import db as db_mod
        db = db_mod
    meta = SOURCES[source]
    started = ensure_utc(now or now_utc())
    report = RunReport(source=source, dry_run=dry_run)

    if not dry_run:
        with db.cursor() as cur:
            ready = schema_ready(cur)
        if not ready:
            report.outcome = "error"
            report.error = "資料庫尚未套用 migrations/postgres/0004_phase_d1.sql（npm run db:migrate:pg），不寫入"
            log.error("[odds:%s] %s", source, report.error)
            return report

    if not dry_run and fetch is None and source == "twsport":
        if not settings.twsport_enabled:
            report.outcome, report.skipped = "not_configured", "TWSPORT_ENABLED=false"
            _heartbeat(db, report, meta)
            return report
        with db.cursor() as cur:
            from .store import recent_outcomes
            recent = recent_outcomes(cur, source, 3)
        if len(recent) == 3 and all(r["outcome"] == "blocked" for r in recent) \
                and started - ensure_utc(recent[0]["started_at"]) < BLOCKED_BACKOFF:
            report.outcome, report.skipped = "blocked", "backoff：最近 3 次皆 blocked，6 小時內不重試"
            log.warning("[odds:%s] %s", source, report.skipped)
            return report

    run_id = None
    if not dry_run:
        from .store import start_run
        with db.cursor() as cur:
            run_id = start_run(cur, source, started)
        report.run_id = run_id

    raw: Any = None
    t0 = time.monotonic()
    try:
        if fetch is not None:
            raw = fetch(report)
        elif source == "twsport":
            raw = _fetch_twsport(report)
        else:
            raw = _fetch_oddsapi(report, started)
        report.fetched_at = ensure_utc(fetched_at or now_utc())
        report.latency_ms = int((time.monotonic() - t0) * 1000)
        _process(db, source, raw, report, dry_run=dry_run, run_id=run_id)
    except OddsSourceError as e:
        report.outcome, report.error = e.outcome, str(e)[:500]
        report.diagnostics["error_detail"] = e.detail
        report.latency_ms = report.latency_ms or int((time.monotonic() - t0) * 1000)
        if isinstance(e, ParserChanged) and raw is not None and not dry_run:
            report.diagnostics["raw_sample"] = _save_raw_sample(source, raw, started)
        log.warning("[odds:%s] %s：%s", source, e.outcome, e)
    except Exception as e:  # noqa: BLE001 — 任何未預期錯誤都只影響本來源本次執行
        report.outcome, report.error = "error", f"{type(e).__name__}: {e}"[:500]
        log.exception("[odds:%s] 未預期錯誤", source)

    if not dry_run:
        from .store import finish_run
        with db.cursor() as cur:
            finish_run(cur, run_id, finished_at=now_utc(), diagnostics={**report.diagnostics,
                                                                        "events": report.events[:60],
                                                                        "market_counts": report.market_counts},
                       **{k: v for k, v in report.summary().items()
                          if k in ("outcome", "n_requests", "latency_ms", "n_events", "n_matched", "n_unmatched",
                                   "n_ambiguous", "n_rejected", "n_markets", "n_markets_invalid", "n_snapshots_new",
                                   "n_snapshots_unchanged", "n_snapshots_stale", "quota_remaining", "quota_used",
                                   "quota_last", "error")},
                       fetched_at=report.fetched_at)
        _heartbeat(db, report, meta)
    log.info("[odds:%s] %s events=%d matched=%d markets=%d new=%d unchanged=%d stale=%d%s", source, report.outcome,
             report.n_events, report.n_matched, report.n_markets, report.n_snapshots_new,
             report.n_snapshots_unchanged, report.n_snapshots_stale, " (dry-run)" if dry_run else "")
    return report


def _process(db, source: str, raw: Any, report: RunReport, *, dry_run: bool, run_id: int | None) -> None:
    parsed = _parse(source, raw)
    report.diagnostics.update(parsed.diagnostics)
    listed = parsed.n_nba_events_listed if source == "twsport" else parsed.n_listed
    report.n_events = len(parsed.events)
    report.n_markets = parsed.n_snapshots
    report.n_markets_invalid = parsed.n_invalid
    for e in parsed.events:
        for s in e.snapshots:
            report.market_counts[s.canonical_key] = report.market_counts.get(s.canonical_key, 0) + 1

    if not parsed.events:
        if source == "twsport" and listed > 0:
            raise ParserChanged(f"聯賽節點宣稱 {listed} 場 NBA 賽事，但解析不到任何賽事")
        report.outcome = "no_nba_markets"
        return
    if parsed.n_snapshots == 0:
        if parsed.n_invalid > 0:
            raise ParserChanged(f"{len(parsed.events)} 場 NBA 賽事但沒有任何合法市場（{parsed.n_invalid} 個被拒）",
                                detail={"invalid": [i for e in parsed.events for i in e.invalid][:20]})
        report.outcome = "no_nba_markets"
        return

    fetched_at = report.fetched_at
    with db.cursor() as cur:
        games, team_ids, links = load_context(cur, source, parsed.events)
    horizon = refresh_window_utc(fetched_at)[1]
    to_write: list[tuple[int, Any]] = []
    results = []
    alert = False
    for ev in parsed.events:
        lk = links.get(ev.source_event_id)
        existing = ExistingLink(lk["game_id"], lk["status"], bool(lk["manual"])) if lk else None
        res = match_event(ev, games, team_ids, existing=existing, schedule_horizon_utc=horizon)
        results.append((ev, res))
        alert |= res.alert
        report.n_matched += res.status == MATCHED
        report.n_unmatched += res.status == "unmatched"
        report.n_ambiguous += res.status == "ambiguous"
        report.n_rejected += res.status == "rejected"
        started = fetched_at >= ensure_utc(ev.commence_utc)
        report.events.append({"source_event_id": ev.source_event_id, "home": ev.home_name, "away": ev.away_name,
                              "commence_utc": ensure_utc(ev.commence_utc).isoformat(), "stage": ev.stage,
                              "status": res.status, "game_id": res.game_id, "method": res.method,
                              "reason": res.reason, "time_delta_min": res.time_delta_min,
                              "n_snapshots": len(ev.snapshots), "invalid": ev.invalid[:5],
                              "skipped_in_play": bool(res.status == MATCHED and started)})
        if res.status == MATCHED and not started:      # D.1 只收賽前盤
            to_write.extend((res.game_id, s) for s in ev.snapshots)

    if not dry_run:
        from .store import persist_snapshots, upsert_link
        with db.transaction() as cur:
            stats = persist_snapshots(cur, to_write, source=source, fetched_at=fetched_at, run_id=run_id)
            for ev, res in results:
                upsert_link(cur, source=source, ev=ev, result=res, seen_at=fetched_at)
        report.n_snapshots_new, report.n_snapshots_unchanged, report.n_snapshots_stale = (
            stats.new, stats.unchanged, stats.stale)
        if stats.stale_detail:
            report.diagnostics["stale"] = stats.stale_detail[:20]
    report.diagnostics["would_write"] = len(to_write)
    report.outcome = "partial" if (alert or parsed.n_invalid) else "success"


def _heartbeat(db, report: RunReport, meta: dict) -> None:
    from .store import odds_heartbeat
    status = OUTCOME_STATUS.get(report.outcome, "error")
    msg = report.error
    if report.outcome == "no_nba_markets":
        msg = "目前沒有 NBA 盤（休賽 / 尚未開盤）；來源與 parser 正常"
    elif report.outcome == "partial":
        msg = (f"未對應 {report.n_unmatched} / 不唯一 {report.n_ambiguous} / 拒絕 {report.n_rejected} 場，"
               f"無效市場 {report.n_markets_invalid}")
    if (report.source == "oddsapi" and report.quota_remaining is not None
            and report.quota_remaining < settings.odds_api_quota_warn and status == "ok"):
        status, msg = "warn", f"額度剩 {report.quota_remaining}（警戒 {settings.odds_api_quota_warn}）"
    with db.cursor() as cur:
        odds_heartbeat(cur, source_key=report.source, display_name=meta["display_name"], outcome=report.outcome,
                       status=status, error=msg, records_updated=report.n_snapshots_new,
                       expected_interval_min=meta["expected_interval_min"],
                       meta={"run_id": report.run_id, "n_events": report.n_events, "n_matched": report.n_matched,
                             "n_markets": report.n_markets, "quota_remaining": report.quota_remaining,
                             "quota_used": report.quota_used, "latency_ms": report.latency_ms})


# ------------------------------------------------------------------ #
# 排程入口（guarded 由 scheduling.py 包）                                 #
# ------------------------------------------------------------------ #

def twsport_odds_job() -> RunReport:
    return run_odds_job("twsport")


def oddsapi_odds_job() -> RunReport:
    return run_odds_job("oddsapi")
