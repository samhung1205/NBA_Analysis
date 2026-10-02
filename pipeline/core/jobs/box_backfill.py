"""
Phase C.5B：五季基本 box score 回填（NBA CDN，不依賴 stats.nba.com）
------------------------------------------------------------
現況：DB 的歷史賽事是 ESPN 備援回填的（nba_game_id = 'espn:<id>'），沒有官方 gameId，所以
`games_sync` 的賽後結算（只處理官方 ID）碰不到它們。這支 job 反過來做：

  1. 依 NBA gameId 規則枚舉每季候選 ID（例行賽 002YY00001..01230、季後賽 004YY00RSG、
     附加賽 005YY00XXX、NBA Cup 決賽 006YY00001）。
  2. 抓 cdn.nba.com/…/boxscore_<id>.json（靜態檔，不存在回 403/404）。
  3. 用「主客隊 + 開賽時間 ±12 小時」認領既有的 'espn:' 列（改寫為官方 ID）；DB 完全沒有該場才新增。
  4. 同一個交易寫入球隊/球員統計、逐節/半場、衍生指標；失敗只影響該場。

特性：
  - 可續傳 / 冪等：已完整的場次（官方 ID + 球隊 raw 計數 + 球員 + 逐節）直接跳過；source_fetch_log
    記錄 missing / error，重跑只重試 error（--retry-missing 連 missing 也重探）。
  - 單場錯誤不中止整季：記錄後繼續，結尾彙整失敗清單。
  - 進度 log + data_sources 心跳（source_key=nba_cdn）。
  - 封鎖偵測：連續多個 403/404 時用 canary（已知存在的場次）確認；canary 也失敗 = 被限流，
    退避等待，仍失敗就優雅中止（重跑接續）。
"""
from __future__ import annotations

import argparse
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Iterator

import requests

from ..db import (
    adopt_espn_game, cursor, fetch_log_keys, find_game_by_matchup, heartbeat, log_fetch,
    refresh_player_current_team, transaction, upsert_game,
)
from ..sources import nba_cdn
from ..sources.fallback import SOURCE_DISPLAY
from ..sources.common import season_from_game_id, stage_from_game_id
from .box_store import store_box_basic

log = logging.getLogger(__name__)

DEFAULT_SEASONS = ["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]
CANARY_ID = "0022100001"          # 2021-22 開幕戰，已確認存在
HEARTBEAT_KEY = "nba_cdn"
HEARTBEAT_NAME, _ = SOURCE_DISPLAY["nba_cdn"]
REGULAR_GAMES = 1230
# 附加賽 ID 末三碼（2021-22 ~ 2025-26 五季逐一實測，每季恰 6 場；列出而非窮舉）
PLAYIN_SUFFIXES = (101, 111, 121, 131, 201, 211)
PLAYOFF_ROUND_SERIES = {1: 8, 2: 4, 3: 2, 4: 1}   # 每輪系列賽數


# ------------------------------------------------------------------ #
# 候選 gameId                                                          #
# ------------------------------------------------------------------ #

def season_yy(season: str) -> str:
    return season[2:4]


def candidate_ids(season: str, stages: tuple[str, ...] = ("regular", "playin", "playoffs")) -> list[str]:
    yy = season_yy(season)
    ids: list[str] = []
    if "regular" in stages:
        ids += [f"002{yy}{n:05d}" for n in range(1, REGULAR_GAMES + 1)]
        if int(yy) >= 23:                                # NBA Cup 決賽（2023-24 起）
            ids.append(f"006{yy}00001")
    if "playin" in stages:
        ids += [f"005{yy}00{x}" for x in PLAYIN_SUFFIXES]
    if "playoffs" in stages:
        for rnd, n_series in PLAYOFF_ROUND_SERIES.items():
            for s in range(n_series):
                ids += [f"004{yy}00{rnd}{s}{g}" for g in range(1, 8)]
    return ids


# ------------------------------------------------------------------ #
# DB 狀態                                                              #
# ------------------------------------------------------------------ #

def complete_game_ids(cur, season: str) -> set[str]:
    """已完整回填的官方 nba_game_id：雙方都有 raw 計數、至少一位球員、逐節齊全、衍生指標在。"""
    cur.execute(
        """
        SELECT g.nba_game_id FROM games g
         WHERE g.season = %s AND g.nba_game_id NOT LIKE 'espn:%%'
           AND g.home_q1 IS NOT NULL AND g.away_q1 IS NOT NULL
           AND (SELECT count(*) FROM team_game_stats t WHERE t.game_id = g.id AND t.fga IS NOT NULL) = 2
           AND (SELECT count(*) FROM team_game_derived d WHERE d.game_id = g.id) = 2
           AND EXISTS (SELECT 1 FROM player_game_stats p WHERE p.game_id = g.id)
        """,
        (season,),
    )
    return {r["nba_game_id"] for r in cur.fetchall()}


def _team_map(cur) -> dict[int, int]:
    cur.execute("SELECT id, nba_team_id FROM teams WHERE nba_team_id IS NOT NULL")
    return {r["nba_team_id"]: r["id"] for r in cur.fetchall()}


@dataclass
class Resolution:
    game: dict | None = None          # {id, home_team_id, away_team_id}
    action: str = ""                  # existing / adopted / inserted
    pts_mismatch: bool = False


def resolve_game(cur, box: dict, team_map: dict[int, int]) -> Resolution | None:
    """把 box score 對應到本地 games 列：官方 ID 已在 → 沿用；'espn:' 暫存列 → 認領；都沒有 → 新增。"""
    gid = box["nba_game_id"]
    home, away = team_map.get(box["home_team_id"]), team_map.get(box["away_team_id"])
    if not home or not away:
        return None
    cur.execute("SELECT id, home_pts, away_pts FROM games WHERE nba_game_id = %s", (gid,))
    row = cur.fetchone()
    action = "existing"
    if not row:
        near = find_game_by_matchup(cur, home_team_id=home, away_team_id=away, date_utc=box["date_utc"])
        if near and near["nba_game_id"].startswith("espn:"):
            adopt_espn_game(cur, nba_game_id=gid, home_team_id=home, away_team_id=away,
                            date_utc=box["date_utc"].isoformat())
            cur.execute("SELECT id, home_pts, away_pts FROM games WHERE nba_game_id = %s", (gid,))
            row, action = cur.fetchone(), "adopted"
        elif near:
            # 同場比賽已有另一個官方 ID：資料異常，不硬塞
            raise ValueError(f"{gid} 與既有官方賽事 {near['nba_game_id']} 的主客隊/時間相同")
    if not row:
        game_id = upsert_game(
            cur, nba_game_id=gid, season=season_from_game_id(gid), season_stage=stage_from_game_id(gid),
            date_utc=box["date_utc"], home_team_id=home, away_team_id=away, arena=box.get("arena"),
            status="final", home_pts=box["home"]["team"]["pts"], away_pts=box["away"]["team"]["pts"],
        )
        row, action = {"id": game_id, "home_pts": box["home"]["team"]["pts"],
                       "away_pts": box["away"]["team"]["pts"]}, "inserted"
    res = Resolution(game={"id": row["id"], "home_team_id": home, "away_team_id": away}, action=action)
    res.pts_mismatch = (row["home_pts"], row["away_pts"]) != (box["home"]["team"]["pts"], box["away"]["team"]["pts"])
    return res


def store_one(box: dict, team_map: dict[int, int]) -> Resolution | None:
    """單場原子寫入。box 必須是 final。"""
    with transaction() as cur:
        res = resolve_game(cur, box, team_map)
        if res is None:
            return None
        # box score 為權威：比分/狀態/場館與官方一致（ESPN 備援列可能有差異）
        cur.execute(
            "UPDATE games SET status='final', home_pts=%s, away_pts=%s, arena=COALESCE(arena, %s),"
            " period=%s, updated_at=NOW() WHERE id=%s",
            (box["home"]["team"]["pts"], box["away"]["team"]["pts"], box.get("arena"),
             4 + sum(1 for p in box["home"]["periods"] if (p.get("period") or 0) >= 5), res.game["id"]),
        )
        store_box_basic(cur, res.game, box, keep_player_team=True)
        log_fetch(cur, kind="box_basic", key=box["nba_game_id"], status="ok")
        return res


# ------------------------------------------------------------------ #
# 抓取（執行緒池）+ 封鎖偵測                                             #
# ------------------------------------------------------------------ #

_local = threading.local()

# cdn.nba.com 對「不存在」與「被限流」都回 403；高頻連續請求時實測曾出現大量暫時性 403
# （同一個 ID 稍後重試就 200）。所以 NotFound 要多試幾次才算 missing，且請求要放慢。
NOTFOUND_RETRY_WAITS = (1.5, 4.0)


def _session() -> requests.Session:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def fetch_one(gid: str, delay: tuple[float, float] = (0.2, 0.6),
              sleep: Callable[[float], None] = time.sleep) -> tuple[str, dict | None, str | None]:
    """→ (status, box, error)；status ∈ ok / missing / error。"""
    sleep(random.uniform(*delay))
    err = None
    for attempt in range(len(NOTFOUND_RETRY_WAITS) + 1):
        try:
            return "ok", nba_cdn.fetch_box_basic(gid, _session()), None
        except nba_cdn.NotFound as e:
            err = str(e)
            if attempt < len(NOTFOUND_RETRY_WAITS):
                sleep(NOTFOUND_RETRY_WAITS[attempt])
        except Exception as e:  # noqa: BLE001 — 單場錯誤（逾時/5xx/JSON）不中止整季
            return "error", None, f"{type(e).__name__}: {e}"[:300]
    return "missing", None, err


def source_reachable(fetch: Callable[[str], tuple] = fetch_one) -> bool:
    status, _, _ = fetch(CANARY_ID)
    return status == "ok"


@dataclass
class Outcome:
    gid: str
    status: str                    # stored / missing / not_final / unmapped / error
    action: str = ""               # existing / adopted / inserted
    pts_mismatch: bool = False
    error: str | None = None


def _store_with_retry(store, box, team_map, attempts: int = 4):
    """並行寫入偶發的死結 / 序列化衝突：交易已回滾，稍等後重試（冪等）。"""
    from psycopg import errors as pgerr

    for i in range(attempts):
        try:
            return store(box, team_map)
        except (pgerr.DeadlockDetected, pgerr.SerializationFailure):
            if i == attempts - 1:
                raise
            time.sleep(random.uniform(0.2, 1.0) * (i + 1))


def process_one(gid: str, team_map: dict[int, int], fetch: Callable[[str], tuple] = fetch_one,
                store: Callable[[dict, dict[int, int]], Resolution | None] = store_one) -> Outcome:
    """抓取 + 寫入一場（在工作執行緒內執行，各自取用連線池連線）。永不拋例外。"""
    try:
        status, box, err = fetch(gid)
        if status == "missing":
            with cursor() as cur:
                log_fetch(cur, kind="box_basic", key=gid, status="missing", error=err)
            return Outcome(gid, "missing", error=err)
        if status != "ok":
            with cursor() as cur:
                log_fetch(cur, kind="box_basic", key=gid, status="error", error=err)
            return Outcome(gid, "error", error=err)
        if box["status"] != "final":
            with cursor() as cur:
                log_fetch(cur, kind="box_basic", key=gid, status="not_final")
            return Outcome(gid, "not_final")
        res = _store_with_retry(store, box, team_map)
        if res is None:
            with cursor() as cur:
                log_fetch(cur, kind="box_basic", key=gid, status="error", error="球隊無法對應")
            return Outcome(gid, "unmapped", error="球隊無法對應")
        return Outcome(gid, "stored", action=res.action, pts_mismatch=res.pts_mismatch)
    except Exception as e:  # noqa: BLE001 — 寫入失敗（資料異常 / DB 暫時錯誤）也只影響該場
        msg = f"{type(e).__name__}: {e}"[:300]
        log.warning("%s 處理失敗：%s", gid, msg)
        try:
            with cursor() as cur:
                log_fetch(cur, kind="box_basic", key=gid, status="error", error=msg)
        except Exception:  # noqa: BLE001
            pass
        return Outcome(gid, "error", error=msg)


@dataclass
class SeasonStats:
    season: str
    candidates: int = 0
    skipped_done: int = 0
    skipped_logged_missing: int = 0
    stored: int = 0
    adopted: int = 0
    inserted: int = 0
    missing: int = 0
    not_final: int = 0
    unmapped: int = 0
    errors: int = 0
    pts_mismatch: int = 0
    aborted: str | None = None
    failed_ids: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)

    def add(self, o: Outcome) -> None:
        if o.status == "stored":
            self.stored += 1
            self.adopted += o.action == "adopted"
            self.inserted += o.action == "inserted"
            self.pts_mismatch += o.pts_mismatch
        elif o.status == "missing":
            self.missing += 1
            self.missing_ids.append(o.gid)
        elif o.status == "not_final":
            self.not_final += 1
        else:
            self.errors += 1
            self.unmapped += o.status == "unmapped"
            self.failed_ids.append(o.gid)


def _chunks(seq: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def backfill_season(season: str, *, stages: tuple[str, ...] = ("regular", "playin", "playoffs"),
                    retry_missing: bool = False, force: bool = False, limit: int | None = None, workers: int = 3,
                    fetch: Callable[[str], tuple] = fetch_one,
                    store: Callable[[dict, dict[int, int]], Resolution | None] = store_one,
                    sleep: Callable[[float], None] = time.sleep,
                    backoff_seconds: tuple[int, ...] = (60, 180, 600),
                    block_streak: int = 12, progress_every: int = 100) -> SeasonStats:
    st = SeasonStats(season=season)
    with cursor() as cur:
        done = set() if force else complete_game_ids(cur, season)     # force：來源定義/公式修正後整批重寫
        logged_missing = set() if retry_missing else fetch_log_keys(cur, "box_basic", ("missing", "not_final"))
        team_map = _team_map(cur)

    all_ids = candidate_ids(season, stages)
    st.candidates = len(all_ids)
    todo = []
    for gid in all_ids:
        if gid in done:
            st.skipped_done += 1
        elif gid in logged_missing:
            st.skipped_logged_missing += 1
        else:
            todo.append(gid)
    if limit:
        todo = todo[:limit]
    log.info("[%s] 候選 %d：已完整 %d、已知不存在 %d、待處理 %d", season, st.candidates, st.skipped_done,
             st.skipped_logged_missing, len(todo))

    t0 = time.time()
    processed = 0
    streak = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for chunk in _chunks(todo, max(workers * 4, 8)):
            outcomes = list(pool.map(lambda g: process_one(g, team_map, fetch, store), chunk))
            for o in outcomes:
                processed += 1
                st.add(o)
                streak = streak + 1 if o.status == "missing" else 0
            if processed % progress_every < len(chunk) or processed == len(todo):
                log.info("[%s] 進度 %d/%d｜寫入 %d（認領 %d／新增 %d）｜不存在 %d｜錯誤 %d｜%.0fs",
                         season, processed, len(todo), st.stored, st.adopted, st.inserted,
                         st.missing, st.errors, time.time() - t0)
                _beat(st, "ok" if not st.errors else "warn")

            # 封鎖偵測：連續多個「不存在」→ 確認 canary 是否仍可取
            if streak >= block_streak:
                if source_reachable(fetch):
                    streak = 0                       # 來源正常，這些真的不存在
                else:
                    recovered = False
                    for wait in backoff_seconds:
                        log.warning("[%s] 疑似被限流（canary 失敗），%ds 後重試", season, wait)
                        sleep(wait)
                        if source_reachable(fetch):
                            recovered, streak = True, 0
                            break
                    if not recovered:
                        st.aborted = "來源持續不可用（canary 失敗），已中止；重跑會接續"
                        log.error("[%s] %s", season, st.aborted)
                        # 這一串被誤記為 missing 的不能留下，否則重跑會跳過
                        with cursor() as cur:
                            cur.execute("DELETE FROM source_fetch_log WHERE kind='box_basic' AND status='missing'"
                                        " AND key = ANY(%s)", (st.missing_ids[-block_streak * 2:],))
                        break
    return st


def _beat(st: SeasonStats, status: str, error: str | None = None) -> None:
    try:
        with cursor() as cur:
            heartbeat(cur, source_key=HEARTBEAT_KEY, display_name=HEARTBEAT_NAME, category="games",
                      status=status, records_updated=st.stored,
                      error=error or (f"{st.season}：{st.errors} 場失敗" if st.errors else None),
                      expected_interval_min=60 * 24)
    except Exception:  # noqa: BLE001 — 心跳失敗不影響回填
        log.debug("heartbeat 失敗", exc_info=True)


def run(seasons: list[str], **kw) -> list[SeasonStats]:
    results = []
    with cursor() as cur:
        cur.execute("SELECT 1")
    for season in seasons:
        log.info("=== 回填 %s ===", season)
        try:
            st = backfill_season(season, **kw)
        except Exception as e:  # noqa: BLE001
            log.exception("%s 回填例外：%s", season, e)
            st = SeasonStats(season=season, aborted=f"例外：{e}")
        results.append(st)
        _beat(st, "error" if st.aborted else ("warn" if st.errors else "ok"), st.aborted)
        log.info("[%s] 完成：寫入 %d（認領 %d／新增 %d）｜不存在 %d｜非 final %d｜錯誤 %d｜比分不一致 %d%s",
                 season, st.stored, st.adopted, st.inserted, st.missing, st.not_final, st.errors,
                 st.pts_mismatch, f"｜中止：{st.aborted}" if st.aborted else "")
        if st.failed_ids:
            log.warning("[%s] 失敗場次（重跑會重試）：%s", season, st.failed_ids)
    with cursor() as cur:
        n = refresh_player_current_team(cur)
    log.info("球員目前球隊/先發旗標已依最近出賽更新 %d 位", n)
    return results


def main() -> None:
    from ..logging_conf import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser(description="五季基本 box score 回填（NBA CDN）")
    ap.add_argument("--seasons", nargs="+", default=DEFAULT_SEASONS)
    ap.add_argument("--stages", nargs="+", default=["regular", "playin", "playoffs"])
    ap.add_argument("--retry-missing", action="store_true", help="連已記錄為不存在的 ID 也重探")
    ap.add_argument("--force", action="store_true", help="已完整的場次也重新抓取並覆寫（修正欄位定義後用）")
    ap.add_argument("--limit", type=int, default=None, help="每季最多處理幾場（試跑用）")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    import os
    os.environ.setdefault("PG_POOL_MAX", str(args.workers + 1))   # 連線有限（Supabase session pooler 共 15 條）
    results = run(args.seasons, stages=tuple(args.stages), retry_missing=args.retry_missing,
                  force=args.force, limit=args.limit, workers=args.workers)
    bad = [r for r in results if r.aborted or r.failed_ids]
    if bad:
        log.warning("=== 結束，仍有未完成：%s（重跑即可接續）===", [r.season for r in bad])
    else:
        log.info("=== 全部完成 ===")


if __name__ == "__main__":
    main()
