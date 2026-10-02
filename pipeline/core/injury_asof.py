"""
賽前傷病狀態還原（as-of lookup）
------------------------------------------------------------
問題：「某場比賽開打前，某隊每位球員最後已知的出賽狀態是什麼？」

規則（只看 report_time_utc **嚴格早於** cutoff 的報告；cutoff = 開賽時間 − decision_offset）：

  1. 由新到舊找「涵蓋該隊該賽事日（ET）」的報告：
       - 沒涵蓋 → 繼續往前（賽程還沒出現在報告裡）。
       - 涵蓋但該隊 NOT YET SUBMITTED → 繼續往前（這份對該隊沒有資訊，不可當成全員健康）。
       - 涵蓋且已申報（或推定已申報）→ 停下，以這份報告為準。
  2. 以該報告為準時：球員在名單上 → 其官方狀態；不在 → Not Listed（視同可出賽）。
     這就是「球員消失」的語意：更晚的報告不再列出他，他就不再被視為 Out。
  3. 找不到任何涵蓋報告 → Unknown（known=False）。

狀態是 (賽事日, 球隊, 球員) 為單位：某球員因今天的比賽被列 Out，不會延續到明天的比賽——
明天的報告若沒有列他，就是 Not Listed。

這個模組是純函式 + 記憶體索引：load_index() 一次載入全部快照，之後對數千場比賽逐場查詢不再打 DB。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from .injury_history import NOT_LISTED, UNKNOWN
from .timeutil import ET, ensure_utc

LOOKBACK_DAYS = 4   # 報告最早在賽事日前幾天就會出現；超過就不再往前找（避免把很舊的狀態當成賽前資訊）

# 缺陣機率先驗（用於「分鐘加權傷病衝擊」特徵；固定常數，非擬合值）
# 官方定義 Out=確定不出賽；Doubtful 約 25% 出賽；Questionable 約 50%；Probable 約 75~90% 出賽。
# 實際比例見 docs/phase-c5b-report.md 的「狀態 vs 實際出賽」稽核表。
P_ABSENT = {"Out": 1.0, "Doubtful": 0.75, "Questionable": 0.5, "Probable": 0.15,
            "Available": 0.0, NOT_LISTED: 0.0}


@dataclass(frozen=True)
class PlayerState:
    player_id: int | None
    player_name: str
    status: str
    reason: str | None = None


@dataclass
class TeamInjuryState:
    known: bool                              # 是否有涵蓋報告
    report_id: int | None = None
    report_time_utc: datetime | None = None
    implied_empty: bool = False              # 該隊整隊沒列任何人（推定已申報）
    skipped_unsubmitted: int = 0             # 為了找到已申報報告，跳過了幾份 NOT YET SUBMITTED
    players: list[PlayerState] = field(default_factory=list)

    def status_of(self, player_id: int) -> str:
        if not self.known:
            return UNKNOWN
        for p in self.players:
            if p.player_id == player_id:
                return p.status
        return NOT_LISTED

    def absent_prob(self, player_id: int) -> float | None:
        s = self.status_of(player_id)
        return None if s == UNKNOWN else P_ABSENT.get(s, 0.0)


@dataclass
class Snapshot:
    id: int
    report_time_utc: datetime
    coverage: dict[str, dict[str, dict]]
    # (game_date_iso, team_abbr) → 列出的球員；key 用縮寫以便與 coverage 對齊
    entries: dict[tuple[str, str], list[PlayerState]] = field(default_factory=dict)


@dataclass
class InjuryIndex:
    snapshots: list[Snapshot]                # 依 report_time_utc 遞增
    times: list[datetime] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.snapshots.sort(key=lambda s: s.report_time_utc)
        self.times = [s.report_time_utc for s in self.snapshots]


def build_index(reports: Iterable[dict], entries: Iterable[dict], team_abbr_by_id: dict[int, str]) -> InjuryIndex:
    """reports: {id, report_time_utc, coverage}；entries: {report_id, game_date, team_id, player_id, player_name, status, reason}"""
    snaps = {r["id"]: Snapshot(r["id"], ensure_utc(r["report_time_utc"]), r["coverage"] or {}) for r in reports}
    for e in entries:
        snap = snaps.get(e["report_id"])
        abbr = team_abbr_by_id.get(e["team_id"]) if e.get("team_id") is not None else None
        if snap is None or abbr is None:
            continue
        key = (e["game_date"].isoformat() if hasattr(e["game_date"], "isoformat") else str(e["game_date"]), abbr)
        snap.entries.setdefault(key, []).append(
            PlayerState(e.get("player_id"), e["player_name"], e["status"], e.get("reason")))
    return InjuryIndex(list(snaps.values()))


def load_index(cur, *, since: datetime | None = None) -> InjuryIndex:
    cur.execute("SELECT id, abbr FROM teams")
    abbr = {r["id"]: r["abbr"] for r in cur.fetchall()}
    where, params = ("WHERE report_time_utc >= %s", (since,)) if since else ("", ())
    cur.execute(f"SELECT id, report_time_utc, coverage FROM injury_reports {where} ORDER BY report_time_utc", params)
    reports = cur.fetchall()
    cur.execute(
        f"SELECT e.report_id, e.game_date, e.team_id, e.player_id, e.player_name, e.status, e.reason"
        f" FROM injury_report_entries e JOIN injury_reports r ON r.id = e.report_id {where.replace('report_time_utc', 'r.report_time_utc')}",
        params)
    return build_index(reports, cur.fetchall(), abbr)


def et_game_date(tip_utc: datetime) -> date:
    return ensure_utc(tip_utc).astimezone(ET).date()


def team_state_asof(index: InjuryIndex, *, team_abbr: str, game_date: date, cutoff_utc: datetime,
                    lookback_days: int = LOOKBACK_DAYS) -> TeamInjuryState:
    """某隊在某賽事日（ET）的比賽，於 cutoff 之前（嚴格早於）最後已知的傷病狀態。"""
    cutoff = ensure_utc(cutoff_utc)
    iso = game_date.isoformat()
    earliest = cutoff - timedelta(days=lookback_days)
    i = bisect.bisect_left(index.times, cutoff) - 1          # 最後一份「嚴格早於 cutoff」
    skipped = 0
    while i >= 0:
        snap = index.snapshots[i]
        if snap.report_time_utc < earliest:
            break
        cov = snap.coverage.get(iso, {}).get(team_abbr)
        if cov is not None:
            if cov.get("nys"):
                skipped += 1
            else:
                return TeamInjuryState(
                    known=True, report_id=snap.id, report_time_utc=snap.report_time_utc,
                    implied_empty=bool(cov.get("implied")), skipped_unsubmitted=skipped,
                    players=list(snap.entries.get((iso, team_abbr), [])))
        i -= 1
    return TeamInjuryState(known=False, skipped_unsubmitted=skipped)
