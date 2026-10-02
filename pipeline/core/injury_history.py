"""
官方傷病報告 → 完整快照（injury_reports / injury_report_entries）
------------------------------------------------------------
為什麼要存「完整快照」而不是只存狀態變化：

  官方報告只列出「有狀態的球員」。球員後來從報告消失 = 已不在名單上（康復/不再列出），
  只存「變化事件」很難表達這件事，而且補抓較舊報告（亂序）時，事件序列會被插錯。
  完整快照 + 涵蓋範圍（coverage）讓「某場比賽開打前最後已知狀態」可以被精確還原（見 injury_asof.py）。

狀態語意（本專案自訂，官方只有前 5 種）：
  Out / Doubtful / Questionable / Probable / Available   官方列出的狀態
  Not Listed   該隊該日比賽已被某份「已申報」的報告涵蓋，但球員不在名單上 → 視同可出賽（衍生）
  Unknown      賽前沒有任何報告涵蓋該隊該場比賽 → 無資訊，不是 Available

涵蓋範圍（coverage）：{賽事日期(ET): {隊伍縮寫: {"n": 列出的球員數, "nys": 是否 NOT YET SUBMITTED,
                                                  "implied": 比賽在報告中但該隊一列都沒有}}}
  - nys=True：該隊尚未申報 → 這份報告對該隊「沒有資訊」，查詢時要往前找較早的報告（不可當成全員健康）。
  - implied=True：對手有列、該隊整隊沒有任何一列。官方未申報會顯示 NOT YET SUBMITTED，
    所以推定為「已申報、無人列入」。（此推定的檢驗見 docs/phase-c5b-report.md）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from .sources.injuries import normalize_player_name

log = logging.getLogger(__name__)

LISTED_STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available")
NOT_LISTED = "Not Listed"
UNKNOWN = "Unknown"

# 官方報告全名 → 縮寫（DB teams.name 與官方報告不同的別名）
TEAM_NAME_ALIASES = {"la clippers": "LAC", "los angeles clippers": "LAC", "la lakers": "LAL"}


def parse_game_date(raw: Any) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if not isinstance(raw, str):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def split_matchup(raw: Any) -> tuple[str, str] | None:
    """'MIL@MEM' → ('MIL', 'MEM')（客, 主）。"""
    if not isinstance(raw, str) or "@" not in raw:
        return None
    away, _, home = raw.partition("@")
    away, home = away.strip().upper(), home.strip().upper()
    return (away, home) if away and home else None


@dataclass
class TeamDirectory:
    """隊伍名稱/縮寫 ↔ teams.id。"""
    abbr_to_id: dict[str, int]
    name_to_abbr: dict[str, str]

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "TeamDirectory":
        rows = list(rows)
        names = {r["name"].lower(): r["abbr"] for r in rows if r.get("name")}
        names.update(TEAM_NAME_ALIASES)
        return cls({r["abbr"]: r["id"] for r in rows if r.get("abbr")}, names)

    def abbr_for_name(self, name: Any) -> str | None:
        return self.name_to_abbr.get(name.strip().lower()) if isinstance(name, str) else None


@dataclass
class PlayerDirectory:
    """正規化姓名 → [(player_id, team_id)]。同名多人時以報告的隊伍消歧，仍無法判定則不對應（不猜）。"""
    by_key: dict[str, list[tuple[int, int | None]]]

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "PlayerDirectory":
        by: dict[str, list[tuple[int, int | None]]] = {}
        for r in rows:
            key = normalize_player_name(r["name"])
            if key:
                by.setdefault(key, []).append((r["id"], r.get("team_id")))
        return cls(by)

    def resolve(self, raw_name: Any, team_id: int | None) -> int | None:
        key = normalize_player_name(raw_name)
        cands = self.by_key.get(key or "", [])
        if len(cands) == 1:
            return cands[0][0]
        if len(cands) > 1 and team_id is not None:
            same_team = [pid for pid, tid in cands if tid == team_id]
            if len(same_team) == 1:
                return same_team[0]
        return None


@dataclass
class Entry:
    game_date: date
    team_abbr: str | None
    team_id: int | None
    player_id: int | None
    player_name: str
    status: str
    reason: str | None


@dataclass
class ParsedReport:
    report_time_utc: datetime
    n_rows: int = 0
    entries: list[Entry] = field(default_factory=list)
    coverage: dict[str, dict[str, dict]] = field(default_factory=dict)
    n_unmatched: int = 0
    n_skipped: int = 0            # 無狀態 / 無日期等無法使用的列（不含 NOT YET SUBMITTED）

    @property
    def game_dates(self) -> list[str]:
        return sorted(self.coverage)


def _is_nys(row: dict) -> bool:
    reason = row.get("reason")
    return isinstance(reason, str) and "NOT YET SUBMITTED" in reason.upper()


def parse_report(rows: list[dict], report_time_utc: datetime, teams: TeamDirectory,
                 players: PlayerDirectory) -> ParsedReport:
    """把 injuries.fetch_injury_report() 的列轉成快照（entries + coverage）。純函式，不碰 DB。"""
    out = ParsedReport(report_time_utc=report_time_utc, n_rows=len(rows))
    seen: set[tuple[date, str | None, str]] = set()
    unmapped_pairs: set[tuple[str, tuple[str, str]]] = set()

    for row in rows:
        gdate = parse_game_date(row.get("game_date"))
        pair = split_matchup(row.get("matchup"))
        if gdate is None:
            out.n_skipped += 1
            continue
        iso = gdate.isoformat()
        cov = out.coverage.setdefault(iso, {})
        if pair:
            for abbr in pair:   # 比賽在報告中 → 雙方都在涵蓋範圍內（先假設 implied，之後被實際列覆寫）
                cov.setdefault(abbr, {"n": 0, "nys": False, "implied": True})

        team_abbr = teams.abbr_for_name(row.get("team"))   # 對不到 → None（entry 照存，team_id 為 NULL，供稽核）
        if team_abbr is None and pair:
            unmapped_pairs.add((iso, pair))
        if team_abbr and team_abbr not in cov:
            cov[team_abbr] = {"n": 0, "nys": False, "implied": True}

        if _is_nys(row):
            if team_abbr:
                cov[team_abbr].update(nys=True, implied=False)
            continue

        name, status = row.get("player_name"), row.get("status")
        if not isinstance(name, str) or not name.strip() or not isinstance(status, str) or not status.strip():
            out.n_skipped += 1
            continue
        status = status.strip()
        team_id = teams.abbr_to_id.get(team_abbr) if team_abbr else None
        if team_abbr:
            cov[team_abbr]["n"] += 1
            cov[team_abbr]["implied"] = False
        dedupe = (gdate, team_abbr, name.strip())
        if dedupe in seen:
            continue
        seen.add(dedupe)
        pid = players.resolve(name, team_id)
        if pid is None:
            out.n_unmatched += 1
        reason = row.get("reason") if isinstance(row.get("reason"), str) else None
        out.entries.append(Entry(gdate, team_abbr, team_id, pid, name.strip(), status, reason))

    # 保守處理：報告中有「隊名對不到」的列時，該場兩隊中仍是「推定無人列入」者不可當成已申報、全員健康
    # （那一列可能正是它的球員）→ 標成無資訊（nys），查詢時往前找較早的報告。
    for iso, pair in unmapped_pairs:
        for abbr in pair:
            c = out.coverage[iso].get(abbr)
            if c and c["implied"]:
                c.update(nys=True, implied=False)
    return out


# ------------------------------------------------------------------ #
# DB                                                                   #
# ------------------------------------------------------------------ #

def load_directories(cur) -> tuple[TeamDirectory, PlayerDirectory]:
    cur.execute("SELECT id, abbr, name FROM teams")
    teams = TeamDirectory.from_rows(cur.fetchall())
    cur.execute("SELECT id, name, team_id FROM players")
    return teams, PlayerDirectory.from_rows(cur.fetchall())


def report_exists(cur, report_time_utc: datetime, source: str = "nba_official") -> bool:
    cur.execute("SELECT 1 FROM injury_reports WHERE source = %s AND report_time_utc = %s",
                (source, report_time_utc))
    return cur.fetchone() is not None


def ingest_snapshot(cur, parsed: ParsedReport, *, source: str = "nba_official") -> int | None:
    """寫入一份快照；**必須在交易內呼叫**（整份報告要嘛完整寫入要嘛完全沒有）。

    冪等 / 併發：injury_reports 有 UNIQUE(source, report_time_utc)。重複寫入或兩個行程同時寫同一份報告時，
    後到者的 INSERT ... ON CONFLICT DO NOTHING 不回傳列 → 回 None（已存在，什麼都不做）。
    回傳新建的 report_id。
    """
    import json

    cur.execute(
        """
        INSERT INTO injury_reports (source, report_time_utc, n_rows, n_entries, n_unmatched, coverage)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (source, report_time_utc) DO NOTHING
        RETURNING id
        """,
        (source, parsed.report_time_utc, parsed.n_rows, len(parsed.entries), parsed.n_unmatched,
         json.dumps(parsed.coverage)),
    )
    row = cur.fetchone()
    if not row:
        return None
    report_id = row["id"]
    if parsed.entries:
        ph = "(%s, %s, %s, %s, %s, %s, %s)"
        sql = ("INSERT INTO injury_report_entries (report_id, game_date, team_id, player_id, player_name, status, reason)"
               f" VALUES {', '.join([ph] * len(parsed.entries))}"
               " ON CONFLICT (report_id, game_date, team_id, player_name) DO NOTHING")
        cur.execute(sql, [v for e in parsed.entries
                          for v in (report_id, e.game_date, e.team_id, e.player_id, e.player_name, e.status, e.reason)])
    return report_id


def reresolve_unmatched(cur) -> int:
    """回填球員表（box score 進來後）可能多了新球員：把先前對不到的 entry 重新對應（只填 NULL，不覆寫）。
    單一批次 UPDATE（逐列更新在遠端資料庫上要數小時）。"""
    cur.execute("SELECT id, name, team_id FROM players")
    players = PlayerDirectory.from_rows(cur.fetchall())
    cur.execute("SELECT id, team_id, player_name FROM injury_report_entries WHERE player_id IS NULL")
    ids, pids = [], []
    for r in cur.fetchall():
        pid = players.resolve(r["player_name"], r["team_id"])
        if pid:
            ids.append(r["id"])
            pids.append(pid)
    for i in range(0, len(ids), 20000):
        cur.execute(
            "UPDATE injury_report_entries e SET player_id = v.pid"
            " FROM (SELECT unnest(%s::bigint[]) AS id, unnest(%s::int[]) AS pid) v"
            " WHERE e.id = v.id AND e.player_id IS NULL", (ids[i:i + 20000], pids[i:i + 20000]))
    if ids:
        cur.execute(
            "UPDATE injury_reports r SET n_unmatched = c.n FROM ("
            " SELECT report_id, count(*) FILTER (WHERE player_id IS NULL) AS n"
            " FROM injury_report_entries GROUP BY report_id) c"
            " WHERE c.report_id = r.id AND r.n_unmatched IS DISTINCT FROM c.n")
    return len(ids)
