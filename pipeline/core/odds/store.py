"""
盤口快照寫入（Phase D.1）—— 時間序列，不是 current-state table
------------------------------------------------------------
series = (game_id, source, bookmaker, market)。每次輪詢對每個 series：

  1. 取該 series 最新一列（fetched_at 最大）。
  2. fetched_at 早於最新一列的 fetched_at / last_seen_at → stale（亂序或重放舊資料）→ 不寫，計數。
     （未來資料不可寫成較早時間；舊時間的資料也不能插到歷史中間假裝當時看得到）
  3. content_hash 相同（line / 各邊 decimal / 狀態 / outcome_set 都沒變）→ 只把 last_seen_at 往後推（GREATEST），
     **不新增列、不動任何價格/線/時間欄位**。
  4. 內容不同（線動、只有價格動、狀態變 suspended/open…）→ INSERT 新列；舊列永遠不 UPDATE 內容。
     A → B → A 也會產生第三列（與「最新一列」比較，不是與全歷史比較）。

fetched_at 是**我們收到回應的時間**（UTC，aware），不是來源宣稱的時間；source_updated_at 另存，
且晚於 fetched_at + 容許誤差的來源時間視為異常（存在 raw_json，欄位留 NULL）。

冪等 / 併發：
  - 同一 source 的寫入在單一交易內以 pg_advisory_xact_lock 串行化 → 兩個行程同時寫同一份新內容只會有一列。
  - UNIQUE (game_id, source, bookmaker, market, fetched_at) WHERE content_hash IS NOT NULL → 同一次抓取重跑不重複。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from ..timeutil import ensure_utc
from .canonical import NORMALIZER_VERSION, MarketSnapshot, content_hash

SOURCE_TIME_SKEW = timedelta(minutes=2)


@dataclass
class PersistStats:
    new: int = 0
    unchanged: int = 0
    stale: int = 0
    stale_detail: list[dict] = field(default_factory=list)


def _source_time(snap: MarketSnapshot, fetched_at: datetime) -> tuple[datetime | None, str | None]:
    if snap.source_updated_at is None:
        return None, None
    t = ensure_utc(snap.source_updated_at)
    if t > fetched_at + SOURCE_TIME_SKEW:
        return None, t.isoformat()
    return t, None


def persist_snapshots(cur, items: Iterable[tuple[int, MarketSnapshot]], *, source: str, fetched_at: datetime,
                      run_id: int | None = None) -> PersistStats:
    """items：[(game_id, MarketSnapshot)]。必須在交易內呼叫（db.transaction()）。"""
    fetched_at = ensure_utc(fetched_at)
    stats = PersistStats()
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"odds_snapshots:{source}",))
    for game_id, snap in items:
        h = content_hash(game_id, snap)
        cur.execute(
            """
            SELECT id, fetched_at, last_seen_at, content_hash FROM odds_snapshots
             WHERE game_id = %s AND source = %s AND bookmaker = %s AND market = %s AND content_hash IS NOT NULL
             ORDER BY fetched_at DESC, id DESC LIMIT 1
            """,
            (game_id, snap.source, snap.bookmaker, snap.market),
        )
        last = cur.fetchone()
        if last is not None:
            latest_seen = max(ensure_utc(last["fetched_at"]), ensure_utc(last["last_seen_at"] or last["fetched_at"]))
            same = last["content_hash"] == h
            if fetched_at < ensure_utc(last["fetched_at"]) or (fetched_at < latest_seen and not same):
                stats.stale += 1
                stats.stale_detail.append({"game_id": game_id, "market": snap.market, "bookmaker": snap.bookmaker,
                                           "fetched_at": fetched_at.isoformat(), "latest": latest_seen.isoformat()})
                continue
            if same:
                cur.execute("UPDATE odds_snapshots SET last_seen_at = GREATEST(COALESCE(last_seen_at, fetched_at), %s)"
                            " WHERE id = %s", (fetched_at, last["id"]))
                stats.unchanged += 1
                continue
        src_time, future_src = _source_time(snap, fetched_at)
        raw = {**snap.raw, "source_event_id": snap.source_event_id, "canonical_market": snap.canonical_key,
               "normalizer": NORMALIZER_VERSION}
        if future_src:
            raw["source_updated_at_future"] = future_src
        cur.execute(
            """
            INSERT INTO odds_snapshots
              (fetched_at, game_id, source, market, line, home_odds, away_odds, over_odds, under_odds, raw_json,
               bookmaker, market_type, period, outcome_set, away_line, draw_odds, model_target, model_threshold,
               market_status, source_event_id, source_market_id, source_updated_at, last_seen_at, content_hash,
               fetch_run_id, normalizer_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (game_id, source, bookmaker, market, fetched_at) WHERE content_hash IS NOT NULL DO NOTHING
            """,
            (fetched_at, game_id, snap.source, snap.market, snap.line, snap.home_odds, snap.away_odds, snap.over_odds,
             snap.under_odds, Jsonb(raw), snap.bookmaker, snap.market_type, snap.period, snap.outcome_set,
             snap.away_line, snap.draw_odds, snap.model_target, snap.model_threshold, snap.status,
             snap.source_event_id, snap.source_market_id, src_time, fetched_at, h, run_id, NORMALIZER_VERSION),
        )
        if cur.rowcount == 1:
            stats.new += 1
        else:
            stats.unchanged += 1
    return stats


# ------------------------------------------------------------------ #
# 對應層 / 抓取紀錄                                                      #
# ------------------------------------------------------------------ #

def load_links(cur, source: str, event_ids: list[str]) -> dict[str, dict]:
    if not event_ids:
        return {}
    cur.execute("SELECT * FROM odds_event_links WHERE source = %s AND source_event_id = ANY(%s)", (source, event_ids))
    return {r["source_event_id"]: r for r in cur.fetchall()}


def upsert_link(cur, *, source: str, ev, result, seen_at: datetime) -> None:
    """人工對應（manual=true）永不被自動結果覆寫。"""
    cur.execute(
        """
        INSERT INTO odds_event_links
          (source, source_event_id, game_id, status, method, reason, home_name_raw, away_name_raw,
           commence_time_utc, time_delta_min, source_stage, candidates, first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, source_event_id) DO UPDATE SET
          game_id = CASE WHEN odds_event_links.manual THEN odds_event_links.game_id ELSE EXCLUDED.game_id END,
          status = CASE WHEN odds_event_links.manual THEN odds_event_links.status ELSE EXCLUDED.status END,
          method = CASE WHEN odds_event_links.manual THEN odds_event_links.method ELSE EXCLUDED.method END,
          reason = EXCLUDED.reason, home_name_raw = EXCLUDED.home_name_raw, away_name_raw = EXCLUDED.away_name_raw,
          commence_time_utc = EXCLUDED.commence_time_utc, time_delta_min = EXCLUDED.time_delta_min,
          source_stage = EXCLUDED.source_stage, candidates = EXCLUDED.candidates,
          last_seen_at = GREATEST(odds_event_links.last_seen_at, EXCLUDED.last_seen_at)
        """,
        (source, ev.source_event_id, result.game_id, result.status, result.method, result.reason, ev.home_name,
         ev.away_name, ensure_utc(ev.commence_utc), result.time_delta_min, ev.stage, Jsonb(result.candidates),
         seen_at, seen_at),
    )


RUN_COLUMNS = ("fetched_at", "finished_at", "outcome", "n_requests", "latency_ms", "n_events", "n_matched",
               "n_unmatched", "n_ambiguous", "n_rejected", "n_markets", "n_markets_invalid", "n_snapshots_new",
               "n_snapshots_unchanged", "n_snapshots_stale", "quota_remaining", "quota_used", "quota_last", "error",
               "diagnostics")


def start_run(cur, source: str, started_at: datetime) -> int:
    cur.execute("INSERT INTO odds_fetch_runs (source, started_at, outcome) VALUES (%s, %s, 'running') RETURNING id",
                (source, started_at))
    return cur.fetchone()["id"]


def finish_run(cur, run_id: int, **fields: Any) -> None:
    cols = [c for c in RUN_COLUMNS if c in fields]
    vals = [Jsonb(json.loads(json.dumps(fields[c], default=str))) if c == "diagnostics" else fields[c] for c in cols]
    cur.execute(f"UPDATE odds_fetch_runs SET {', '.join(f'{c} = %s' for c in cols)} WHERE id = %s", (*vals, run_id))


def recent_outcomes(cur, source: str, limit: int = 3) -> list[dict]:
    cur.execute("SELECT outcome, started_at FROM odds_fetch_runs WHERE source = %s AND outcome <> 'running'"
                " ORDER BY started_at DESC LIMIT %s", (source, limit))
    return cur.fetchall()


def odds_heartbeat(cur, *, source_key: str, display_name: str, outcome: str, status: str, error: str | None,
                   records_updated: int | None, expected_interval_min: int, meta: dict) -> None:
    """data_sources 心跳：last_status 維持 ok / warn / error；last_outcome 存細分狀態。"""
    cur.execute(
        """
        INSERT INTO data_sources
          (source_key, display_name, category, last_success_at, last_attempt_at, last_status, last_error,
           records_updated, expected_interval_min, last_outcome, meta)
        VALUES (%s, %s, 'odds', CASE WHEN %s = 'ok' THEN NOW() ELSE NULL END, NOW(), %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_key) DO UPDATE SET
          display_name = EXCLUDED.display_name, category = 'odds', last_attempt_at = NOW(),
          last_success_at = CASE WHEN EXCLUDED.last_status = 'ok' THEN NOW() ELSE data_sources.last_success_at END,
          last_status = EXCLUDED.last_status, last_error = EXCLUDED.last_error,
          records_updated = COALESCE(EXCLUDED.records_updated, data_sources.records_updated),
          expected_interval_min = EXCLUDED.expected_interval_min,
          last_outcome = EXCLUDED.last_outcome, meta = EXCLUDED.meta
        """,
        (source_key, display_name, status, status, (error or None) and error[:500], records_updated,
         expected_interval_min, outcome, Jsonb(json.loads(json.dumps(meta, default=str)))),
    )
