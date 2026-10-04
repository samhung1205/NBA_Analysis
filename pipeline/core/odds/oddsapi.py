"""
The Odds API v4 adapter（Phase D.1）
------------------------------------------------------------
文件：https://the-odds-api.com/liveapi/guides/v4/
  GET /v4/sports/basketball_nba/events          → 不扣額度：先確認未來視窗內有沒有比賽，沒有就不花額度
  GET /v4/sports/basketball_nba/odds            → 扣額度 = markets 數 × regions 數（h2h,spreads,totals × us = 3）
  GET /v4/sports/basketball_nba/events/{id}/odds → 上半場（h2h_h1 / spreads_h1 / totals_h1）只能逐場抓，
                                                   每場 3 × regions；免費方案（500/月）負擔不起 → 預設關閉
  回應標頭 x-requests-remaining / x-requests-used / x-requests-last → 寫入 odds_fetch_runs。
  歷史端點（/v4/historical/...）只限付費方案：本專案不使用、不假設可用。

保留每一家 bookmaker 的每一個 market（bookmaker key、last_update、market key、outcome name/price/point）；
**不做** 平均 / consensus / 最佳盤 / 去水（留給 D.2）。

outcome 對應：h2h / spreads 以 outcome.name == event.home_team / away_team（同一份回應內的字串，完全相等）；
totals 以 Over / Under。名稱對不上或缺邊 → 該 market 拒收（記錄原因），不猜。
上半場獨贏（h2h_h1）為兩向；若平手時的結算（退款 / 輸）依 bookmaker 而定，D.1 只照實保存（outcome_set=two_way）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import requests

from ..timeutil import now_utc, parse_utc
from .canonical import (
    AWAY, DRAW, FULL_GAME, H1, HOME, MONEYLINE, OPEN, OVER, SPREAD, THREE_WAY, TOTAL, TWO_WAY, UNDER,
    InvalidMarket, SourceEvent, decimal_from_american, make_snapshot,
)
from .errors import AuthFailed, NetworkFailure, NotConfigured, ParserChanged, QuotaLow, RateLimited

log = logging.getLogger(__name__)

SOURCE = "oddsapi"
PARSER_VERSION = "oddsapi-parser-v1"
BASE_URL = "https://api.the-odds-api.com/v4"
SPORT = "basketball_nba"
FEATURED_MARKETS = ("h2h", "spreads", "totals")
H1_MARKETS = ("h2h_h1", "spreads_h1", "totals_h1")

MARKET_KEYS = {
    "h2h": (MONEYLINE, FULL_GAME), "spreads": (SPREAD, FULL_GAME), "totals": (TOTAL, FULL_GAME),
    "h2h_h1": (MONEYLINE, H1), "spreads_h1": (SPREAD, H1), "totals_h1": (TOTAL, H1),
}


@dataclass
class Quota:
    remaining: int | None = None
    used: int | None = None
    last: int | None = None

    def update(self, headers: Any) -> None:
        def num(k):
            v = headers.get(k) if headers is not None else None
            try:
                return int(float(v)) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None
        r, u, last = num("x-requests-remaining"), num("x-requests-used"), num("x-requests-last")
        self.remaining = r if r is not None else self.remaining
        self.used = u if u is not None else self.used
        self.last = last if last is not None else self.last


@dataclass
class OddsApiClient:
    api_key: str | None
    regions: str = "us"
    bookmakers: str | None = None          # 指定 bookmaker 時優先於 regions（額度以 bookmaker 數 / 10 計）
    timeout_s: float = 20.0
    quota_reserve: int = 25                # 剩餘額度低於此值就不再呼叫付費端點
    session: requests.Session | None = None
    http_get: Callable | None = None       # 測試注入
    quota: Quota = field(default_factory=Quota)
    n_requests: int = 0

    def __post_init__(self):
        if not self.api_key:
            raise NotConfigured("ODDS_API_KEY 未設定")

    def _get(self, path: str, params: dict) -> Any:
        params = {**params, "apiKey": self.api_key}
        getter = self.http_get or (self.session or requests).get
        self.n_requests += 1
        try:
            r = getter(f"{BASE_URL}{path}", params=params, timeout=self.timeout_s)
        except requests.RequestException as e:
            raise NetworkFailure(f"{path}: {type(e).__name__}: {e}"[:300]) from e
        self.quota.update(r.headers)
        if r.status_code == 401:
            raise AuthFailed(f"{path}: HTTP 401（API key 無效或停用）")
        if r.status_code == 429:
            raise RateLimited(f"{path}: HTTP 429")
        if r.status_code == 422:
            raise ParserChanged(f"{path}: HTTP 422（參數不被接受：{r.text[:200]}）")
        if r.status_code != 200:
            raise NetworkFailure(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ParserChanged(f"{path}: 回應不是 JSON") from e

    def _check_quota(self, cost: int) -> None:
        if self.quota.remaining is not None and self.quota.remaining - cost < self.quota_reserve:
            raise QuotaLow(f"剩餘額度 {self.quota.remaining}，本次需要 {cost}，保留門檻 {self.quota_reserve}",
                           detail={"remaining": self.quota.remaining})

    def _base_params(self, frm: datetime, to: datetime) -> dict:
        p = {"dateFormat": "iso", "commenceTimeFrom": frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "commenceTimeTo": to.strftime("%Y-%m-%dT%H:%M:%SZ")}
        return p

    def events(self, frm: datetime, to: datetime) -> list[dict]:
        """免費端點。"""
        data = self._get(f"/sports/{SPORT}/events", self._base_params(frm, to))
        if not isinstance(data, list):
            raise ParserChanged("events 回應不是 list")
        return data

    def _cost_units(self) -> int:
        return max(1, len(self.bookmakers.split(",")) // 10 + (len(self.bookmakers.split(",")) % 10 > 0)) \
            if self.bookmakers else len(self.regions.split(","))

    def odds(self, frm: datetime, to: datetime, markets=FEATURED_MARKETS) -> list[dict]:
        self._check_quota(len(markets) * self._cost_units())
        p = {**self._base_params(frm, to), "markets": ",".join(markets), "oddsFormat": "decimal"}
        p.update({"bookmakers": self.bookmakers} if self.bookmakers else {"regions": self.regions})
        data = self._get(f"/sports/{SPORT}/odds", p)
        if not isinstance(data, list):
            raise ParserChanged("odds 回應不是 list")
        return data

    def event_odds(self, event_id: str, markets=H1_MARKETS) -> dict:
        self._check_quota(len(markets) * self._cost_units())
        p = {"dateFormat": "iso", "markets": ",".join(markets), "oddsFormat": "decimal"}
        p.update({"bookmakers": self.bookmakers} if self.bookmakers else {"regions": self.regions})
        data = self._get(f"/sports/{SPORT}/events/{event_id}/odds", p)
        if not isinstance(data, dict):
            raise ParserChanged("event odds 回應不是 object")
        return data


def fetch_raw(client: OddsApiClient, *, now: datetime | None = None, horizon_hours: int = 48,
              include_h1: bool = False) -> dict:
    now = now or now_utc()
    frm, to = now - timedelta(hours=1), now + timedelta(hours=horizon_hours)
    raw: dict[str, Any] = {"parser_version": PARSER_VERSION, "events_listing": client.events(frm, to),
                           "odds": [], "event_odds": {}, "window": [frm.isoformat(), to.isoformat()]}
    if not raw["events_listing"]:
        return raw                           # 沒有比賽：不花額度
    raw["odds"] = client.odds(frm, to)
    if include_h1:
        for ev in raw["odds"]:
            try:
                raw["event_odds"][ev["id"]] = client.event_odds(ev["id"])
            except QuotaLow:
                raw.setdefault("diagnostics", {})["h1_skipped_quota"] = True
                break
    raw["quota"] = vars(client.quota).copy()
    return raw


# ------------------------------------------------------------------ #
# Parser（純函式）                                                       #
# ------------------------------------------------------------------ #

@dataclass
class ParseResult:
    events: list[SourceEvent] = field(default_factory=list)
    n_listed: int = 0
    diagnostics: dict = field(default_factory=dict)

    @property
    def n_snapshots(self) -> int:
        return sum(len(e.snapshots) for e in self.events)

    @property
    def n_invalid(self) -> int:
        return sum(len(e.invalid) for e in self.events)


def _price(o: dict) -> float:
    p = o.get("price")
    if isinstance(p, (int, float)) and not isinstance(p, bool) and abs(p) >= 100:
        return decimal_from_american(p)      # 防呆：要求 decimal，但萬一回美式也能辨識
    return p


def _parse_market(ev: dict, bm: dict, m: dict) -> Any:
    key = m.get("key")
    market_type, period = MARKET_KEYS[key]
    home, away = ev["home_team"], ev["away_team"]
    prices: dict[str, Any] = {}
    lines: dict[str, Any] = {}
    outcomes = []
    for o in m.get("outcomes") or []:
        name = o.get("name")
        if market_type == TOTAL:
            side = {"Over": OVER, "Under": UNDER}.get(name)
        else:
            side = HOME if name == home else AWAY if name == away else DRAW if name == "Draw" else None
        if side is None:
            raise InvalidMarket("unexpected_outcome", f"{key}: {name!r}")
        if side in prices:
            raise InvalidMarket("duplicate_outcome", f"{key}: {side}")
        prices[side] = _price(o)
        lines[side] = o.get("point")
        outcomes.append({"side": side, "name": name, "price_raw": o.get("price"), "price_format": "decimal",
                         "point_raw": o.get("point")})
    kw: dict[str, Any] = {}
    if market_type == SPREAD:
        kw = {"home_line": lines.get(HOME), "away_line": lines.get(AWAY)}
    elif market_type == TOTAL:
        if lines.get(OVER) is not None and lines.get(UNDER) is not None and lines[OVER] != lines[UNDER]:
            raise InvalidMarket("asymmetric_total", f"{lines[OVER]} / {lines[UNDER]}")
        kw = {"total_line": lines.get(OVER) if lines.get(OVER) is not None else lines.get(UNDER)}
    outcome_set = THREE_WAY if DRAW in prices else TWO_WAY
    updated = parse_utc(m.get("last_update") or bm.get("last_update"))
    raw = {"parser": PARSER_VERSION, "market_key": key, "bookmaker_title": bm.get("title"),
           "bookmaker_last_update": bm.get("last_update"), "market_last_update": m.get("last_update"),
           "outcomes": outcomes}
    return make_snapshot(source=SOURCE, bookmaker=bm["key"], source_event_id=ev["id"], market_type=market_type,
                         period=period, status=OPEN, prices=prices, outcome_set=outcome_set,
                         source_market_id=key, source_updated_at=updated, raw=raw, **kw)


def parse_event(ev: dict) -> SourceEvent:
    for k in ("id", "home_team", "away_team", "commence_time"):
        if not ev.get(k):
            raise ParserChanged(f"event 缺少 {k}", detail={"keys": sorted(ev)})
    out = SourceEvent(source=SOURCE, source_event_id=ev["id"], home_name=ev["home_team"], away_name=ev["away_team"],
                      commence_utc=parse_utc(ev["commence_time"]), stage="unknown",
                      meta={"sport_key": ev.get("sport_key")})
    bms = ev.get("bookmakers")
    if bms is None:
        raise ParserChanged("event 缺少 bookmakers")
    for bm in bms:
        if not bm.get("key") or not isinstance(bm.get("markets"), list):
            raise ParserChanged("bookmaker 缺少 key / markets")
        for m in bm["markets"]:
            if m.get("key") not in MARKET_KEYS:
                continue
            try:
                out.snapshots.append(_parse_market(ev, bm, m))
            except InvalidMarket as e:
                out.invalid.append({"market": f"{bm['key']}:{m.get('key')}", "reason": e.reason, "detail": str(e)[:200]})
    return out


def parse_raw(raw: dict) -> ParseResult:
    if not isinstance(raw, dict) or not isinstance(raw.get("odds"), list):
        raise ParserChanged("raw 結構不符（缺 odds）")
    res = ParseResult(n_listed=len(raw.get("events_listing") or []))
    by_id: dict[str, SourceEvent] = {}
    for ev in raw["odds"]:
        se = parse_event(ev)
        by_id[se.source_event_id] = se
        res.events.append(se)
    for eid, payload in (raw.get("event_odds") or {}).items():
        extra = parse_event(payload)
        target = by_id.get(eid)
        if target is None:
            res.events.append(extra)
        else:
            target.snapshots.extend(extra.snapshots)
            target.invalid.extend(extra.invalid)
    if raw.get("diagnostics"):
        res.diagnostics.update(raw["diagnostics"])
    return res
