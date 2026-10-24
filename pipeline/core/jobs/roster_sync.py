"""
現役名單同步（roster-sync-v1）：players.team_id ← 官方現役名單
------------------------------------------------------------
- NBA playerindex（有 PERSON_ID）：對 nba_player_id 更新球隊；名單上但 DB 沒有的新秀 / 新簽約球員新增；
  DB 掛在某隊、但不在任何一隊名單上的球員（被釋出 / 退休）team_id 設為 NULL。
- ESPN 備援（只有姓名）：只更新「姓名唯一對應」的既有球員；對不到的列為 unmatched，不新增、不釋出。
- 名單不完整（缺隊 / 人數過少）→ 整批不套用並記錄錯誤，不做部分寫入。
- 寫入的球員蓋上 roster_synced_at，refresh_player_current_team() 之後只在「名單同步後又出賽」時才改寫。

players 是名錄表（非注單 / 稽核帳），以單一交易更新目前球隊；歷史球隊仍保存在 player_game_stats 與傷病快照。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..db import cursor, heartbeat, transaction
from ..sources import rosters as rosters_src
from ..sources.injuries import normalize_player_name
from ..timeutil import now_utc

log = logging.getLogger(__name__)

HEARTBEAT = dict(source_key="nba_rosters", display_name="NBA 現役名單（roster-sync-v1）", category="stats",
                 expected_interval_min=24 * 60)


@dataclass
class RosterPlan:
    source: str
    team_ids: dict[str, int]
    moved: list[tuple[int, str, str | None, str]] = field(default_factory=list)   # (player_id, 姓名, 舊隊, 新隊)
    added: list[rosters_src.RosterEntry] = field(default_factory=list)
    released: list[tuple[int, str, str]] = field(default_factory=list)            # (player_id, 姓名, 舊隊)
    unmatched: list[rosters_src.RosterEntry] = field(default_factory=list)
    matched: dict[int, str] = field(default_factory=dict)                          # player_id → 名單球隊

    def summary(self) -> str:
        return (f"來源 {self.source}｜名單球員 {len(self.matched) + len(self.added) + len(self.unmatched)}｜"
                f"轉隊 {len(self.moved)}｜新增 {len(self.added)}｜離隊 {len(self.released)}｜無法對應 {len(self.unmatched)}")


def plan_roster_sync(cur, source: str, entries: list[rosters_src.RosterEntry]) -> RosterPlan:
    cur.execute("SELECT id, abbr FROM teams")
    team_ids = {r["abbr"]: r["id"] for r in cur.fetchall()}
    problem = rosters_src.completeness_problem(entries, set(team_ids))
    if problem:
        raise ValueError(f"名單不完整，不套用：{problem}")
    abbr_by_tid = {v: k for k, v in team_ids.items()}
    cur.execute("SELECT id, nba_player_id, name, team_id FROM players")
    players = cur.fetchall()
    by_nba = {p["nba_player_id"]: p for p in players}
    by_name: dict[str, list[dict]] = {}
    for p in players:
        key = normalize_player_name(p["name"])
        if key:
            by_name.setdefault(key, []).append(p)

    plan = RosterPlan(source, team_ids)
    for e in entries:
        if e.nba_player_id is not None:
            p = by_nba.get(e.nba_player_id)
            if p is None:
                plan.added.append(e)
                continue
        else:
            cands = by_name.get(normalize_player_name(e.name) or "", [])
            if len(cands) != 1:
                plan.unmatched.append(e)     # 同名多人或查無此人：不猜
                continue
            p = cands[0]
        plan.matched[p["id"]] = e.team_abbr
        old = abbr_by_tid.get(p["team_id"])
        if old != e.team_abbr:
            plan.moved.append((p["id"], p["name"], old, e.team_abbr))
    if source == "nba_playerindex":   # 只有帶 id 的完整名單才能斷定「不在名單上 = 已離隊」
        plan.released = [(p["id"], p["name"], abbr_by_tid.get(p["team_id"]))
                         for p in players if p["team_id"] is not None and p["id"] not in plan.matched]
    return plan


def apply_roster_plan(cur, plan: RosterPlan, synced_at: datetime) -> int:
    """單一交易內執行；回傳實際變更（轉隊 + 新增 + 離隊）筆數。"""
    rows = [(plan.team_ids[abbr], synced_at, pid) for pid, abbr in plan.matched.items()]
    rows += [(None, synced_at, pid) for pid, _, _ in plan.released]
    if rows:
        cur.executemany("UPDATE players SET team_id = %s, roster_synced_at = %s WHERE id = %s", rows)
    for e in plan.added:
        cur.execute(
            """
            INSERT INTO players (nba_player_id, name, team_id, position, roster_synced_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (nba_player_id) DO UPDATE SET
              team_id = EXCLUDED.team_id, roster_synced_at = EXCLUDED.roster_synced_at
            """,
            (e.nba_player_id, e.name, plan.team_ids[e.team_abbr], e.position, synced_at))
    return len(plan.moved) + len(plan.added) + len(plan.released)


def sync_rosters_job(*, dry_run: bool = False,
                     fetch: Callable[[], tuple[str, list[rosters_src.RosterEntry]]] | None = None,
                     now: datetime | None = None) -> RosterPlan | None:
    """抓名單 → 比對 → （非 dry-run）單一交易套用 + 心跳。失敗記錄心跳、不拋出（排程不中斷）。"""
    try:
        source, entries = (fetch or rosters_src.fetch_rosters)()
        with cursor() as cur:
            plan = plan_roster_sync(cur, source, entries)
    except Exception as e:  # noqa: BLE001
        log.error("名單同步失敗：%s", e)
        if not dry_run:
            with cursor() as cur:
                heartbeat(cur, **HEARTBEAT, status="error", error=str(e)[:500])
        return None
    log.info("名單比對：%s", plan.summary())
    if dry_run:
        return plan
    with transaction() as cur:
        n = apply_roster_plan(cur, plan, now or now_utc())
        heartbeat(cur, **HEARTBEAT, status="warn" if plan.unmatched else "ok", records_updated=n,
                  error=f"{len(plan.unmatched)} 位名單球員無法對應" if plan.unmatched else None)
    return plan
