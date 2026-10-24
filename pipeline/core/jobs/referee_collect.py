"""
Phase E.2 — 賽前裁判分派收集（referees-v1；只累積資料，不接入模型 / 策略）
------------------------------------------------------------
    collect_referees_job(now)   排程（台灣 20:07–10:37 ≈ ET 08:07–21:37，每 30 分）：今天還有尚未開賽的 eligible 比賽才抓官方頁
        fetch  → core.sources.referees.parse_assignments（只取 NBA 區塊；結構改變 / 缺裁判 → 例外，不當成零場）
        store  → referee_assignment_reports（內容 hash 相同只更新 last_seen_at；變了才新增，舊列不改寫）
                 + referee_assignments（逐場；比對 games.id，比不到保留 NULL 與原因）
        rematch→ 之前 no_game 的列在賽程同步後補上 game_id（不改觀測內容）

as-of 契約：某場比賽「T 時點已知」的裁判 = observed_at ≤ T 的最新 report 中該場的列。observed_at 是回應完成時間；
頁面日期只是 report_date_et。賽後 box score 的 officials 不得寫進這裡。

頁面日期 ≠ 賽程日期（2026-10-24 實測）：entry-meta 是頁面當下的 ET 日期；新一輪分派公布（約 09:00 ET）前，頁面仍列
前一個比賽日的對戰卻標成今天。因此比對範圍是 report_date_et 當天與前一天，整份對到前一天時 state = previous_slate
（正常、不告警）。同一對戰連續兩天都有比賽時不猜，記 ambiguous。
空表（尚未公布）不寫入；被擋 / 失敗不重試、不輪替 IP，只寫心跳。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import requests

from ..production.features import HISTORY_STAGES
from ..sources import nba_cdn, referees
from ..timeutil import ET, ensure_utc, et_date, now_utc, parse_utc

log = logging.getLogger(__name__)

PARSER_VERSION = "referees-v1"
SOURCE = "nba_official_referees"
HEARTBEAT = dict(source_key="nba_referees", display_name="NBA 官方裁判分派（E.2 referees-v1）", category="stats")
POLL_INTERVAL_MIN = 30
LOOKAHEAD = timedelta(hours=16)          # 今天（ET）剩下的比賽：最晚的西岸場次約 22:30 ET 開賽
ROLES = ("crew_chief", "referee", "umpire", "alternate")

# 官方頁使用城市名；洛杉磯兩隊以 "L.A. Lakers" / "LA Clippers" 區分（2026-10-21 實測）。
CITY_TO_ABBR = {
    "atlanta": "ATL", "boston": "BOS", "brooklyn": "BKN", "charlotte": "CHA", "chicago": "CHI", "cleveland": "CLE",
    "dallas": "DAL", "denver": "DEN", "detroit": "DET", "golden state": "GSW", "houston": "HOU", "indiana": "IND",
    "la clippers": "LAC", "l.a. clippers": "LAC", "los angeles clippers": "LAC",
    "la lakers": "LAL", "l.a. lakers": "LAL", "los angeles lakers": "LAL",
    "memphis": "MEM", "miami": "MIA", "milwaukee": "MIL", "minnesota": "MIN", "new orleans": "NOP",
    "new york": "NYK", "oklahoma city": "OKC", "orlando": "ORL", "philadelphia": "PHI", "phoenix": "PHX",
    "portland": "POR", "sacramento": "SAC", "san antonio": "SAS", "toronto": "TOR", "utah": "UTA",
    "washington": "WAS",
}
_OFFICIAL = re.compile(r"^(?P<name>.+?)\s*\(#\s*(?P<num>\d+)\)$")


@dataclass
class RefereeReport:
    as_of: str
    dry_run: bool = False
    state: str | None = None            # ready / previous_slate / waiting / stale / unavailable / idle
    report_date_et: str | None = None
    n_assignments: int = 0
    new_report: bool = False
    matched: int = 0
    unmatched: list[str] = field(default_factory=list)
    rematched: int = 0
    note: str | None = None
    rows: list[dict] = field(default_factory=list)
    slate_days: set[str] = field(default_factory=set)


def parse_official(text: str | None) -> dict | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    m = _OFFICIAL.match(text)
    return {"name": m["name"], "number": int(m["num"])} if m else {"name": text, "number": None}


def team_abbr(label: str) -> str | None:
    return CITY_TO_ABBR.get(" ".join(label.split()).lower())


def split_game(text: str) -> tuple[str, str]:
    away, _, home = text.partition("@")
    return away.strip(), home.strip()


def officials(row: dict) -> list[dict]:
    out = []
    for role in ROLES:
        o = parse_official(row.get(role))
        if o is not None:
            out.append({"role": role, **o})
    return out


def content_hash(report: dict) -> str:
    body = {"report_date_et": report["report_date_et"],
            "assignments": sorted(report["assignments"], key=lambda r: r["game"])}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ------------------------------------------------------------------ #
# DB                                                                   #
# ------------------------------------------------------------------ #

def schema_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('referee_assignment_reports') IS NOT NULL AS ok")
    return bool(cur.fetchone()["ok"])


def has_games_ahead(cur, now: datetime) -> bool:
    cur.execute("SELECT 1 FROM games WHERE status <> 'final' AND season_stage = ANY(%s) AND date_utc > %s "
                "AND date_utc <= %s LIMIT 1", (list(HISTORY_STAGES), now, now + LOOKAHEAD))
    return cur.fetchone() is not None


def games_on_et_day(cur, day: date) -> dict[tuple[str, str], list[tuple[int, date]]]:
    """(away_abbr, home_abbr) → [(games.id, ET 比賽日)]，涵蓋 ET 比賽日 = day 與 day − 1（見檔頭：頁面日期 ≠ 賽程日期）。"""
    cur.execute("""SELECT g.id, g.date_utc, th.abbr AS home, ta.abbr AS away FROM games g
                     JOIN teams th ON th.id = g.home_team_id JOIN teams ta ON ta.id = g.away_team_id
                    WHERE g.date_utc >= %s AND g.date_utc < %s""",
                (datetime.combine(day - timedelta(days=1), datetime.min.time(), tzinfo=ET) - timedelta(hours=6),
                 datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=ET) + timedelta(hours=6)))
    out: dict[tuple[str, str], list[tuple[int, date]]] = {}
    for r in cur.fetchall():
        d = et_date(ensure_utc(r["date_utc"]))
        if d in (day, day - timedelta(days=1)):
            out.setdefault((r["away"], r["home"]), []).append((r["id"], d))
    return out


def match_row(index: dict[tuple[str, str], list[tuple[int, date]]], game_text: str
              ) -> tuple[int | None, str, date | None]:
    """→ (games.id, match_status, 該場 ET 比賽日)。"""
    away, home = (team_abbr(x) for x in split_game(game_text))
    if away is None or home is None:
        return None, "unknown_team", None
    hits = index.get((away, home), [])
    if len(hits) == 1:
        return hits[0][0], "matched", hits[0][1]
    return None, "ambiguous" if hits else "no_game", None


def store_report(cur, report: dict, *, rep: RefereeReport) -> int:
    day = date.fromisoformat(report["report_date_et"])
    observed = parse_utc(report["observed_at"])
    cur.execute("""INSERT INTO referee_assignment_reports (source, report_date_et, content_hash, n_assignments,
                                                           observed_at, last_seen_at, parser_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source, report_date_et, content_hash)
                   DO UPDATE SET last_seen_at = GREATEST(referee_assignment_reports.last_seen_at, EXCLUDED.last_seen_at)
                   RETURNING id, (xmax = 0) AS inserted""",
                (SOURCE, day, content_hash(report), len(report["assignments"]), observed, observed, PARSER_VERSION))
    r = cur.fetchone()
    rep.new_report = bool(r["inserted"])
    if not rep.new_report:
        return r["id"]
    index = games_on_et_day(cur, day)
    for a in report["assignments"]:
        gid, status, slate = match_row(index, a["game"])
        away, home = split_game(a["game"])
        cur.execute("""INSERT INTO referee_assignments (report_id, report_date_et, game_text, away_label, home_label,
                                                        game_id, match_status, officials)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                    (r["id"], day, a["game"], away, home, gid, status, json.dumps(officials(a), ensure_ascii=False)))
        if gid is None:
            rep.unmatched.append(f"{a['game']}:{status}")
        else:
            rep.matched += 1
            rep.slate_days.add(slate.isoformat())
    return r["id"]


def rematch(cur, day: date) -> int:
    """賽程晚於裁判頁同步時：把 no_game / ambiguous 的列補上 game_id（只改比對結果，不改觀測內容）。"""
    cur.execute("SELECT id, game_text FROM referee_assignments WHERE report_date_et = %s AND game_id IS NULL "
                "AND match_status IN ('no_game', 'ambiguous')", (day,))
    rows = cur.fetchall()
    if not rows:
        return 0
    index = games_on_et_day(cur, day)
    n = 0
    for row in rows:
        gid, status, _ = match_row(index, row["game_text"])
        if gid is not None:
            cur.execute("UPDATE referee_assignments SET game_id = %s, match_status = %s WHERE id = %s",
                        (gid, status, row["id"]))
            n += 1
    return n


# ------------------------------------------------------------------ #
# Job                                                                  #
# ------------------------------------------------------------------ #

def _fetch(get: Callable[..., Any]) -> str:
    r = get(referees.URL, headers=nba_cdn._headers(), timeout=15)
    r.raise_for_status()
    return r.text


def collect_referees_job(now: datetime | None = None, *, db=None, get: Callable[..., Any] | None = None,
                         clock: Callable[[], datetime] | None = None, dry_run: bool = False,
                         force: bool = False) -> RefereeReport:
    from .. import db as default_db
    db = db or default_db
    clock = clock or now_utc
    now = ensure_utc(now or clock())
    rep = RefereeReport(now.isoformat(), dry_run)
    with db.cursor() as cur:
        if not schema_ready(cur) and not dry_run:
            rep.state, rep.note = "unavailable", "資料庫尚未套用 migrations/postgres/0011_phase_e2_referees.sql"
            return rep
        if not force and not has_games_ahead(cur, now):
            rep.state, rep.note = "idle", "idle_no_upcoming_game"
            return rep
    try:
        html = _fetch(get or requests.get)
        observed = ensure_utc(clock())                    # 回應完成後的時間（不是頁面日期）
        report = referees.parse_assignments(html, observed_at=observed)
    except (requests.RequestException, ValueError) as e:
        rep.state, rep.note = "unavailable", f"{type(e).__name__}: {str(e)[:160]}"
        return rep
    rep.report_date_et, rep.n_assignments = report["report_date_et"], len(report["assignments"])
    today_et = observed.astimezone(ET).date().isoformat()
    rep.state = "waiting" if not report["assignments"] else ("stale" if report["report_date_et"] != today_et else "ready")
    if dry_run:
        with db.cursor() as cur:
            index = games_on_et_day(cur, date.fromisoformat(report["report_date_et"]))
        rep.rows = [{"game": a["game"], "match": match_row(index, a["game"]), "officials": officials(a)}
                    for a in report["assignments"]]
        rep.slate_days = {m[2].isoformat() for m in (r["match"] for r in rep.rows) if m[2] is not None}
        _classify_slate(rep)
        return rep
    if not report["assignments"]:
        return rep                                        # 尚未公布：不寫空 report
    with db.transaction() as cur:
        store_report(cur, report, rep=rep)
        rep.rematched = rematch(cur, date.fromisoformat(report["report_date_et"]))
    _classify_slate(rep)
    return rep


def _classify_slate(rep: RefereeReport) -> None:
    """比對到的場次全是頁面日期前一天 → 頁面尚未換成新一輪（previous_slate；不是錯誤）。"""
    if rep.state == "ready" and rep.slate_days and rep.report_date_et not in rep.slate_days:
        rep.state = "previous_slate"


def referees_scheduled() -> None:
    """排程進入點；心跳 data_sources.nba_referees（idle → expected_interval NULL，狀態頁不判過期）。"""
    from .. import db
    from ..production import activity

    status, err, n = "ok", None, 0
    try:
        rep = collect_referees_job()
        n = int(rep.new_report) * rep.n_assignments
        if rep.state == "idle":
            activity.log_skip("referees", rep.note or "idle")
            with db.cursor() as cur:
                activity.touch_heartbeat(cur, **HEARTBEAT)          # idle 也留下「有在跑」的紀錄
                activity.sync_expected_interval(cur, HEARTBEAT["source_key"], None)
            return
        if rep.state == "unavailable":
            status, err = "error", rep.note
        elif rep.unmatched:
            status, err = "warn", "未比對到賽事：" + ", ".join(rep.unmatched)[:400]
        elif rep.state == "stale" and now_utc().astimezone(ET).hour >= 12:   # 09:00 ET 前仍是昨天的頁面屬正常
            status, err = "warn", f"頁面日期 {rep.report_date_et} 不是今天（ET 中午後仍未更新）"
    except Exception as e:  # noqa: BLE001
        log.exception("referee collection 失敗")
        status, err = "error", f"{type(e).__name__}: {e}"[:500]
    with db.cursor() as cur:
        db.heartbeat(cur, status=status, error=err, records_updated=n, expected_interval_min=POLL_INTERVAL_MIN,
                     **HEARTBEAT)
