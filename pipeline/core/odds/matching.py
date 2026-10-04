"""
來源事件 → games.id 對應層（Phase D.1）
------------------------------------------------------------
純函式（不連 DB），輸入：
  - SourceEvent（隊名、開賽時間 UTC、來源宣稱的賽事階段）
  - 候選比賽（games 列：id / home_team_id / away_team_id / date_utc / status / season_stage）
  - 既有對應（odds_event_links）
輸出 MatchResult(status, game_id, method, reason, …)。只有 status='matched' 的事件會寫入盤口。

規則（依序；任何「不唯一」都不猜）：
  0. 隊名 → abbr（team_aliases，完全相等）；查不到 → unmatched / unknown_team。主客同隊 → rejected。
  1. 人工對應（manual）→ 直接採用。
  2. 既有自動對應仍一致（同主客、開賽差 ≤ 寬視窗）→ matched / link。不一致 → 重新比對（記錄 link_invalidated）。
  3. 同主客、開賽差 ≤ 嚴格視窗（預設 3 小時）：唯一 → matched / teams_time；多筆 → ambiguous。
  4. 嚴格視窗內只有「主客相反」的比賽 → rejected / home_away_reversed（可能是 parser 把主客寫反或中立場地；
     寫進去會讓所有正負號顛倒，所以絕不自動採用）。
  5. 同主客、開賽差 ≤ 寬視窗（預設 36 小時；改期）：唯一且比賽尚未開打 → matched / rescheduled（附 time_delta）；
     多筆 → ambiguous；只有主客相反 → rejected。
  6. 否則 unmatched：來源標示季前賽 → preseason_not_tracked（games 依設計不收季前賽）；
     開賽時間超出賽程同步範圍 → beyond_schedule_horizon；其餘 no_candidate。
  比賽狀態為 postponed / cancelled → rejected / game_postponed。
時間一律 aware UTC（台彩 +08:00、Odds API Z 都在 adapter 轉好）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from ..timeutil import ensure_utc
from .canonical import SourceEvent
from .team_aliases import resolve_abbr

STRICT_WINDOW = timedelta(hours=3)
WIDE_WINDOW = timedelta(hours=36)
INACTIVE_GAME_STATUSES = {"postponed", "cancelled", "canceled"}

MATCHED, AMBIGUOUS, UNMATCHED, REJECTED = "matched", "ambiguous", "unmatched", "rejected"
# 需要告警的未對應原因（其餘是預期內：季前賽、超出賽程同步範圍）
ALERT_REASONS = {"unknown_team", "no_candidate", "ambiguous", "home_away_reversed", "same_team",
                 "game_postponed", "link_invalidated"}


@dataclass(frozen=True)
class CandidateGame:
    id: int
    home_team_id: int
    away_team_id: int
    date_utc: datetime
    status: str = "scheduled"
    season_stage: str | None = None
    nba_game_id: str | None = None


@dataclass(frozen=True)
class ExistingLink:
    game_id: int | None
    status: str
    manual: bool = False


@dataclass
class MatchResult:
    status: str
    game_id: int | None = None
    method: str | None = None
    reason: str | None = None
    home_abbr: str | None = None
    away_abbr: str | None = None
    time_delta_min: float | None = None
    candidates: list[dict] = field(default_factory=list)

    @property
    def alert(self) -> bool:
        if self.status == MATCHED:
            return self.method == "rescheduled"      # 改期對應會寫入，但要人看一眼（時區錯誤也會長這樣）
        return self.reason in ALERT_REASONS or self.status == AMBIGUOUS


def _delta(g: CandidateGame, t: datetime) -> timedelta:
    return abs(ensure_utc(g.date_utc) - t)


def _cand(g: CandidateGame, t: datetime) -> dict:
    return {"game_id": g.id, "nba_game_id": g.nba_game_id, "date_utc": ensure_utc(g.date_utc).isoformat(),
            "status": g.status, "delta_min": round((ensure_utc(g.date_utc) - t).total_seconds() / 60, 1)}


def match_event(ev: SourceEvent, games: Iterable[CandidateGame], team_ids: dict[str, int], *,
                existing: ExistingLink | None = None, schedule_horizon_utc: datetime | None = None,
                strict: timedelta = STRICT_WINDOW, wide: timedelta = WIDE_WINDOW) -> MatchResult:
    """team_ids：abbr → teams.id。schedule_horizon_utc：DB 賽程已同步到的時間（超出 → 不告警）。"""
    t = ensure_utc(ev.commence_utc)
    h_abbr, a_abbr = resolve_abbr(ev.home_name), resolve_abbr(ev.away_name)
    base = dict(home_abbr=h_abbr, away_abbr=a_abbr)
    if h_abbr is None or a_abbr is None or h_abbr not in team_ids or a_abbr not in team_ids:
        bad = [n for n, ab in ((ev.home_name, h_abbr), (ev.away_name, a_abbr)) if ab is None or ab not in team_ids]
        return MatchResult(UNMATCHED, reason="unknown_team", candidates=[{"unresolved": bad}], **base)
    if h_abbr == a_abbr:
        return MatchResult(REJECTED, reason="same_team", **base)
    hid, aid = team_ids[h_abbr], team_ids[a_abbr]
    games = list(games)
    by_id = {g.id: g for g in games}

    def finish(g: CandidateGame, method: str, reason: str | None = None) -> MatchResult:
        d = (ensure_utc(g.date_utc) - t).total_seconds() / 60
        if (g.status or "").lower() in INACTIVE_GAME_STATUSES:
            return MatchResult(REJECTED, reason="game_postponed", time_delta_min=d, candidates=[_cand(g, t)], **base)
        return MatchResult(MATCHED, game_id=g.id, method=method, reason=reason, time_delta_min=round(d, 1),
                           candidates=[_cand(g, t)], **base)

    invalidated = False
    if existing and existing.game_id is not None and existing.status == MATCHED:
        g = by_id.get(existing.game_id)
        if existing.manual and g is not None:
            return finish(g, "manual")
        if g is not None and g.home_team_id == hid and g.away_team_id == aid and _delta(g, t) <= wide:
            return finish(g, "link")
        invalidated = True

    same = [g for g in games if g.home_team_id == hid and g.away_team_id == aid]
    rev = [g for g in games if g.home_team_id == aid and g.away_team_id == hid]
    reason_extra = "link_invalidated" if invalidated else None

    for window, method in ((strict, "teams_time"), (wide, "rescheduled")):
        hits = sorted((g for g in same if _delta(g, t) <= window), key=lambda g: _delta(g, t))
        if len(hits) > 1:
            return MatchResult(AMBIGUOUS, reason="ambiguous", candidates=[_cand(g, t) for g in hits], **base)
        if len(hits) == 1:
            g = hits[0]
            if method == "rescheduled" and (g.status or "scheduled") not in {"scheduled"} | INACTIVE_GAME_STATUSES:
                # 已開打 / 已結束的比賽不會被「改期」對上
                return MatchResult(UNMATCHED, reason="no_candidate", candidates=[_cand(g, t)], **base)
            return finish(g, method, reason_extra)
        rev_hits = [g for g in rev if _delta(g, t) <= window]
        if rev_hits:
            return MatchResult(REJECTED, reason="home_away_reversed", candidates=[_cand(g, t) for g in rev_hits], **base)

    if ev.stage == "preseason":
        return MatchResult(UNMATCHED, reason="preseason_not_tracked", **base)
    if schedule_horizon_utc is not None and t > ensure_utc(schedule_horizon_utc):
        return MatchResult(UNMATCHED, reason="beyond_schedule_horizon", **base)
    return MatchResult(UNMATCHED, reason=reason_extra or "no_candidate", **base)
