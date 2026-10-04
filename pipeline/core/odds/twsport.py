"""
台灣運彩（Taiwan Sports Lottery）adapter（Phase D.1）
------------------------------------------------------------
2026-10-04 實測（見 docs/phase-d1-report.md §5）：
  - www.sportslottery.com.tw/sportsbook 的盤口是 iframe 內的 ORAKO sportsbook
    （www-talo-ssb-pr.sportslottery.com.tw），資料來自網站自己使用的同源 JSON：
        POST /services/content/get  {"contentId": {"type": T, "id": ID}, "clientContext": {...}}
    T = boNavigationList（運動 / 國家 / 聯賽樹）、eventGroup（聯賽的賽事列表 + 主盤）、event（單場全部玩法）。
  - 一般 HTTP client（curl / requests）會收到 Cloudflare managed challenge（403 "Just a moment..."）；
    真實瀏覽器正常載入頁面即可。→ 用 Playwright 開真實 Chrome 載入網站，再於頁面內以 fetch 呼叫同一個 JSON
    （與網站自己的呼叫完全相同）。**challenge 若沒有自動通過就回報 blocked，不做任何繞過**
    （不偽造 UA、不用 stealth 外掛、不換 proxy、不解 CAPTCHA）。
  - 每個請求之間有禮貌性間隔；每次輪詢 ≈ 4 + NBA 場次 個請求。

JSON 語意（以 Betradar UOF 參照碼辨識玩法，不靠 nth-child / 中文字串猜）：
  event.participantname_home / participantname_away（名稱 "客 @ 主"；tsstart 帶 +08:00）
  market.externalreference：219 全場獨贏（outcome 4 主 / 5 客）、223/hcp=X 全場讓分（1714 主 / 1715 客，
    X = **主隊**讓分顯示線）、225/total=X 全場大小（12 大 / 13 小）、60 上半場獨贏**三向**（1 主 / 2 和 / 3 客）、
    66/hcp=X 上半場讓分、68/total=X 上半場大小。
  price = 分數賠率 currentpriceup / currentpricedown → decimal = 1 + up/down（13/25 → 1.52）。
  狀態：event/market istradable、market idfobolifestate（O = 開盤）、selection idfoselectionsuspensiontype。
  交叉驗證：ref 與名稱（[上半場]）一致、讓分線與選項名稱的正負號一致、大小與「大/小」一致、主客與 hadvalue 一致
  （大小分的 hadvalue 是 A=大 / H=小，不可信 → 只用參照碼 + 名稱）。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..timeutil import ensure_utc
from .canonical import (
    AWAY, CLOSED, DRAW, FULL_GAME, H1, HOME, MONEYLINE, OPEN, OVER, SPREAD, SUSPENDED, THREE_WAY, TOTAL,
    TWO_WAY, UNDER, UNKNOWN, InvalidMarket, SourceEvent, decimal_from_fraction, make_snapshot,
)
from .errors import Blocked, NetworkFailure, ParserChanged, RateLimited

log = logging.getLogger(__name__)

SOURCE = "twsport"
BOOKMAKER = "twsport"
PARSER_VERSION = "twsport-parser-v1"

SSB_ORIGIN = "https://www-talo-ssb-pr.sportslottery.com.tw"
BASKETBALL_NAV_ID = "34765.1"          # 籃球（可由 TWSPORT_BASKETBALL_NAV_ID 覆寫）
NAV_PREFIX = "1355/"
COUNTRY_NAME = "美國"
# 台彩聯賽名稱 → 來源宣稱的賽事階段（名稱去除 \r 後完全相等）
NBA_TOURNAMENTS = {"美國職籃": "regular", "NBA盃": "cup", "美國職籃熱身賽": "preseason"}
REQUIRED_TOURNAMENT = "美國職籃"       # 休賽期也存在（0 場）；找不到 = 網站結構變了

# ref 基底 → (market_type, period, outcome_set, {outcome ref: side})
MARKET_SPECS: dict[str, tuple[str, str, str, dict[str, str]]] = {
    "219": (MONEYLINE, FULL_GAME, TWO_WAY, {"4": HOME, "5": AWAY}),
    "223": (SPREAD, FULL_GAME, TWO_WAY, {"1714": HOME, "1715": AWAY}),
    "225": (TOTAL, FULL_GAME, TWO_WAY, {"12": OVER, "13": UNDER}),
    "60": (MONEYLINE, H1, THREE_WAY, {"1": HOME, "2": DRAW, "3": AWAY}),
    "66": (SPREAD, H1, TWO_WAY, {"1714": HOME, "1715": AWAY}),
    "68": (TOTAL, H1, TWO_WAY, {"12": OVER, "13": UNDER}),
}
HAD = {"H": HOME, "A": AWAY, "D": DRAW}
_TRAILING_NUM = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*$")


CHALLENGE_TITLES = ("just a moment", "請稍候", "请稍候", "attention required")


def _is_challenge_title(title: str | None) -> bool:
    t = (title or "").lower()
    return any(k in t for k in CHALLENGE_TITLES)


def clean(s: Any) -> str:
    return str(s or "").replace("\r", "").replace("\n", "").strip()


# ------------------------------------------------------------------ #
# Playwright client（只讀）                                             #
# ------------------------------------------------------------------ #

_FETCH_JS = """async ([type, id]) => {
  const r = await fetch('/services/content/get', {method: 'POST',
    headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
    body: JSON.stringify({contentId: {type, id}, clientContext: {language: 'ZH', ipAddress: '0.0.0.0'}})});
  return {status: r.status, text: await r.text()};
}"""


class TwsportClient:
    """真實瀏覽器（預設本機 Google Chrome，channel='chrome'）載入台彩 sportsbook，於頁面內呼叫同源 JSON。"""

    def __init__(self, *, origin: str = SSB_ORIGIN, channel: str | None = "chrome", headless: bool = True,
                 delay_s: tuple[float, float] = (1.0, 2.0), timeout_s: float = 30.0, challenge_wait_s: float = 25.0):
        self.origin, self.channel, self.headless = origin, channel, headless
        self.delay_s, self.timeout_ms, self.challenge_wait_s = delay_s, int(timeout_s * 1000), challenge_wait_s
        self.n_requests = 0
        self._pw = self._browser = self.page = None

    def __enter__(self) -> "TwsportClient":
        try:
            from playwright.sync_api import Error as PwError, sync_playwright
        except ImportError as e:  # pragma: no cover
            raise NetworkFailure("playwright 未安裝（pip install -r requirements.txt）") from e
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(channel=self.channel or None, headless=self.headless)
            ctx = self._browser.new_context(locale="zh-TW", timezone_id="Asia/Taipei")
            self.page = ctx.new_page()
            self.page.set_default_timeout(self.timeout_ms)
            self.n_requests += 1
            self.page.goto(f"{self.origin}/zh/", wait_until="domcontentloaded")
            self._wait_challenge()
        except PwError as e:
            self.close()
            raise NetworkFailure(f"開啟台彩網站失敗：{e}"[:300]) from e
        except Exception:
            self.close()
            raise
        return self

    def _wait_challenge(self) -> None:
        """Cloudflare managed challenge：真實瀏覽器通常數秒內自動通過；沒通過就停（不繞過）。"""
        deadline = time.monotonic() + self.challenge_wait_s
        while _is_challenge_title(self.page.title()):
            if time.monotonic() > deadline:
                raise Blocked("Cloudflare challenge 未自動通過（自動化瀏覽器被擋；不繞過。可改用 --from-file 匯入一般瀏覽器匯出的 HAR）")
            self.page.wait_for_timeout(1000)

    def get(self, ctype: str, cid: str) -> dict:
        time.sleep(random.uniform(*self.delay_s))
        self.n_requests += 1
        try:
            r = self.page.evaluate(_FETCH_JS, [ctype, cid])
        except Exception as e:  # noqa: BLE001
            raise NetworkFailure(f"{ctype} {cid}: {type(e).__name__}: {e}"[:300]) from e
        status, text = r.get("status"), r.get("text") or ""
        if status == 403 or _is_challenge_title(text[:500]):
            raise Blocked(f"{ctype} {cid}: HTTP {status}（Cloudflare 對自動化瀏覽器回 challenge / 403；不繞過）")
        if status == 429:
            raise RateLimited(f"{ctype} {cid}: HTTP 429")
        if status != 200:
            raise NetworkFailure(f"{ctype} {cid}: HTTP {status}")
        try:
            return json.loads(text)
        except ValueError as e:
            raise ParserChanged(f"{ctype} {cid}: 回應不是 JSON", detail={"head": text[:200]}) from e

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._browser = self._pw = self.page = None

    def __exit__(self, *exc) -> None:
        self.close()


def _nodes(nav: dict, where: str) -> list[dict]:
    data = nav.get("data") if isinstance(nav, dict) else None
    if isinstance(data, list):        # boNavigationPath 形式
        data = data[-1] if data else None
    if not isinstance(data, dict) or not isinstance(data.get("bonavigationnodes"), list):
        raise ParserChanged(f"{where}：boNavigationList 結構不符（缺 data.bonavigationnodes）")
    return data["bonavigationnodes"]


def fetch_raw(client, *, basketball_nav_id: str = BASKETBALL_NAV_ID, fetch_event_details: bool = True) -> dict:
    """抓取所有 NBA 聯賽的賽事與玩法，回傳可序列化的 raw（parser / fixture 的輸入）。"""
    sport = client.get("boNavigationList", NAV_PREFIX + basketball_nav_id)
    country = next((n for n in _nodes(sport, "籃球") if clean(n.get("name")) == COUNTRY_NAME), None)
    if country is None:
        raise ParserChanged(f"籃球分類下找不到國家節點「{COUNTRY_NAME}」")
    country_nav = client.get("boNavigationList", NAV_PREFIX + country["idfwbonavigation"])
    tours = {clean(n.get("name")): n for n in _nodes(country_nav, COUNTRY_NAME)}
    if REQUIRED_TOURNAMENT not in tours:
        raise ParserChanged(f"找不到聯賽節點「{REQUIRED_TOURNAMENT}」", detail={"tournaments": sorted(tours)})
    raw: dict[str, Any] = {"parser_version": PARSER_VERSION, "tournaments": [], "event_details": {},
                           "other_tournaments": sorted(n for n in tours if n not in NBA_TOURNAMENTS)}
    for name, stage in NBA_TOURNAMENTS.items():
        node = tours.get(name)
        if node is None:
            continue
        entry = {"name": name, "stage": stage, "nav_id": node.get("idfwbonavigation"),
                 "numevents": int(node.get("numevents") or 0), "event_groups": []}
        raw["tournaments"].append(entry)
        if entry["numevents"] == 0:
            continue
        tnav = client.get("boNavigationList", NAV_PREFIX + node["idfwbonavigation"])
        data = tnav.get("data") if isinstance(tnav, dict) else None
        groups = [m for m in (data or {}).get("marketgroups") or [] if m.get("idfwmarketgrouptype") == "MATCHESCOUPON"]
        if not groups:
            raise ParserChanged(f"聯賽「{name}」有 {entry['numevents']} 場，但沒有 MATCHESCOUPON market group")
        for g in groups:
            entry["event_groups"].append(client.get("eventGroup", g["idfwmarketgroup"]))
    if fetch_event_details:
        for t in raw["tournaments"]:
            for eg in t["event_groups"]:
                for ev in ((eg.get("data") or {}).get("events") or []):
                    eid = ev.get("idfoevent")
                    if not eid or eid in raw["event_details"]:
                        continue
                    try:
                        raw["event_details"][eid] = client.get("event", eid)
                    except (NetworkFailure, RateLimited) as e:     # 單場失敗：退回 coupon 主盤（只有全場）
                        raw["event_details"][eid] = {"error": str(e)[:200]}
    return raw


# ------------------------------------------------------------------ #
# Parser（純函式，fixture 可測）                                         #
# ------------------------------------------------------------------ #

@dataclass
class ParseResult:
    events: list[SourceEvent] = field(default_factory=list)
    n_nba_events_listed: int = 0          # 聯賽節點宣稱的場次（numevents）
    diagnostics: dict = field(default_factory=dict)

    @property
    def n_snapshots(self) -> int:
        return sum(len(e.snapshots) for e in self.events)

    @property
    def n_invalid(self) -> int:
        return sum(len(e.invalid) for e in self.events)


def _status(event: dict, market: dict, selections: list[dict]) -> str:
    if event.get("istradable") is False:
        return SUSPENDED
    life = clean(market.get("idfobolifestate")).upper()
    if life in ("C", "R", "V", "X"):
        return CLOSED
    if life == "S" or market.get("istradable") is False:
        return SUSPENDED
    if life != "O":
        return UNKNOWN
    for s in selections:
        if clean(s.get("idfoselectionsuspensiontype")) or clean(s.get("idfobolifestate")).upper() not in ("", "N", "O"):
            return SUSPENDED
    return OPEN


def _ref(market: dict) -> tuple[str, dict[str, str]]:
    ref = clean(market.get("externalreference"))
    base, _, rest = ref.partition("/")
    params = dict(p.split("=", 1) for p in rest.split("|") if "=" in p) if rest else {}
    return base, params


def _parse_market(ev: dict, m: dict, spec, params: dict, main_rule: str) -> Any:
    market_type, period, outcome_set, side_by_ref = spec
    name = clean(m.get("name"))
    is_h1_name = "上半場" in name
    if (period == H1) != is_h1_name or "節" in name:
        raise InvalidMarket("name_ref_mismatch", f"{name} / {m.get('externalreference')}")
    sels = m.get("selections") or []
    prices: dict[str, float | None] = {}
    outcomes = []
    status = _status(ev, m, sels)
    for s in sels:
        sref, sname = clean(s.get("externalreference")), clean(s.get("name"))
        side = side_by_ref.get(sref)
        if side is None:
            raise InvalidMarket("unexpected_outcome", f"{sname} ref {sref}")
        if side in prices:
            raise InvalidMarket("duplicate_outcome", side)
        if market_type == TOTAL:
            if not sname.startswith({OVER: "大", UNDER: "小"}[side]):
                raise InvalidMarket("side_mismatch", f"{sname} ≠ {side}")
        elif HAD.get(clean(s.get("hadvalue")).upper()) != side:
            raise InvalidMarket("side_mismatch", f"{sname} hadvalue {s.get('hadvalue')} ≠ {side}")
        up, down = s.get("currentpriceup"), s.get("currentpricedown")
        try:
            dec = decimal_from_fraction(up, down)
        except InvalidMarket:
            if status == OPEN:
                raise
            dec = None
        prices[side] = dec
        outcomes.append({"side": side, "name": sname, "ref": sref, "had": s.get("hadvalue"),
                         "handicap_raw": s.get("currenthandicap"), "price_raw": f"{up}/{down}",
                         "price_format": "fractional", "decimal": dec, "selection_id": s.get("idfoselection")})
    kw: dict[str, Any] = {}
    if market_type == SPREAD:
        hcp = params.get("hcp")
        if hcp is None:
            raise InvalidMarket("missing_line", "hcp")
        home_line = float(hcp)
        if m.get("line") not in (None, "") and abs(float(m["line"]) - home_line) > 1e-9:
            raise InvalidMarket("line_mismatch", f"market.line {m.get('line')} ≠ hcp {hcp}")
        for o in outcomes:      # 名稱尾端的帶號數字必須與 主 = hcp / 客 = −hcp 一致
            mt = _TRAILING_NUM.search(o["name"])
            want = home_line if o["side"] == HOME else -home_line
            if mt and abs(float(mt.group(1)) - want) > 1e-9:
                raise InvalidMarket("line_sign_mismatch", f"{o['name']} ≠ {want:+g}")
        kw["home_line"] = home_line
    elif market_type == TOTAL:
        tot = params.get("total")
        if tot is None:
            raise InvalidMarket("missing_line", "total")
        if m.get("line") not in (None, "") and abs(float(m["line"]) - float(tot)) > 1e-9:
            raise InvalidMarket("line_mismatch", f"market.line {m.get('line')} ≠ total {tot}")
        kw["total_line"] = float(tot)
    raw = {"parser": PARSER_VERSION, "market_ref": clean(m.get("externalreference")), "market_name": name,
           "market_id": m.get("idfomarket"), "market_type_id": m.get("idfomarkettype"),
           "ismainline": m.get("ismainline"), "main_rule": main_rule, "lifestate": m.get("idfobolifestate"),
           "istradable": m.get("istradable"), "tsbetstart": m.get("tsbetstart"), "tsbetend": m.get("tsbetend"),
           "outcomes": outcomes}
    return make_snapshot(source=SOURCE, bookmaker=BOOKMAKER, source_event_id=clean(ev.get("idfoevent")),
                         market_type=market_type, period=period, status=status, prices=prices,
                         outcome_set=outcome_set, source_market_id=clean(m.get("idfomarket")) or None,
                         source_updated_at=None, raw=raw, **kw)


def parse_event(ev: dict, stage: str, tournament: str) -> SourceEvent:
    eid = clean(ev.get("idfoevent"))
    home, away = clean(ev.get("participantname_home")), clean(ev.get("participantname_away"))
    if not eid or not home or not away or not ev.get("tsstart"):
        raise ParserChanged("event 缺少 idfoevent / participantname_home / participantname_away / tsstart",
                            detail={"keys": sorted(ev)[:40]})
    start = datetime.fromisoformat(clean(ev["tsstart"]).replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ParserChanged(f"tsstart 沒有時區：{ev['tsstart']}")
    out = SourceEvent(source=SOURCE, source_event_id=eid, home_name=home, away_name=away,
                      commence_utc=ensure_utc(start), stage=stage,
                      meta={"tournament": tournament, "name": clean(ev.get("name")), "venue": ev.get("venue")})
    groups: dict[tuple, list[tuple[dict, dict]]] = {}
    for m in ev.get("markets") or []:
        base, params = _ref(m)
        spec = MARKET_SPECS.get(base)
        if spec is None:
            continue                                   # 其他玩法（單節、勝分差、球隊大小…）不在 D.1 範圍
        if clean(m.get("externalprovider")) not in ("", "BETRADARUOF"):
            out.invalid.append({"market": base, "reason": "unknown_provider", "detail": m.get("externalprovider")})
            continue
        groups.setdefault(spec[:2], []).append((m, params))
    for key, cands in groups.items():
        spec = MARKET_SPECS[next(b for b, s in MARKET_SPECS.items() if s[:2] == key)]
        mains = [c for c in cands if c[0].get("ismainline") is True]
        if len(mains) == 1:
            chosen, rule = mains[0], "ismainline"
        elif not mains and len(cands) == 1:
            chosen, rule = cands[0], "single_line"
        else:
            out.invalid.append({"market": "/".join(key), "reason": "ambiguous_main_line",
                                "detail": [clean(c[0].get("externalreference")) for c in cands]})
            continue
        try:
            out.snapshots.append(_parse_market(ev, chosen[0], spec, chosen[1], rule))
        except InvalidMarket as e:
            out.invalid.append({"market": "/".join(key), "reason": e.reason, "detail": str(e)[:200]})
    if not groups:
        out.invalid.append({"market": "*", "reason": "no_recognized_markets",
                            "detail": [clean(m.get("externalreference")) for m in ev.get("markets") or []][:20]})
    return out


def parse_raw(raw: dict) -> ParseResult:
    if not isinstance(raw, dict) or not isinstance(raw.get("tournaments"), list):
        raise ParserChanged("raw 結構不符（缺 tournaments）")
    res = ParseResult()
    seen: set[str] = set()
    details = raw.get("event_details") or {}
    for t in raw["tournaments"]:
        res.n_nba_events_listed += int(t.get("numevents") or 0)
        for eg in t.get("event_groups") or []:
            data = eg.get("data") if isinstance(eg, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ParserChanged(f"eventGroup 結構不符（聯賽 {t.get('name')}）")
            for ev in data["events"]:
                eid = clean(ev.get("idfoevent"))
                if eid in seen:
                    continue
                seen.add(eid)
                det = details.get(eid)
                full = det.get("data") if isinstance(det, dict) and isinstance(det.get("data"), dict) else None
                src = ev
                if full is not None and clean(full.get("idfoevent")) == eid:
                    src = full
                else:
                    res.diagnostics.setdefault("event_detail_missing", []).append(eid)
                res.events.append(parse_event(src, t.get("stage") or "unknown", t.get("name") or ""))
    if raw.get("other_tournaments"):
        res.diagnostics["other_tournaments"] = raw["other_tournaments"]
    return res


# ------------------------------------------------------------------ #
# 手動擷取匯入（合規備援）：一般瀏覽器 DevTools → Network → Save all as HAR    #
# ------------------------------------------------------------------ #

def raw_from_har(har: dict) -> tuple[dict, datetime]:
    """把使用者以一般瀏覽器瀏覽台彩 NBA 頁面時匯出的 HAR 轉成 parser 的 raw。
    回傳 (raw, fetched_at)：fetched_at = 所用回應中**最晚**的 startedDateTime（保守：不宣稱更早看到）。"""
    import base64

    entries = (har.get("log") or {}).get("entries") if isinstance(har, dict) else None
    if not isinstance(entries, list):
        raise ParserChanged("HAR 結構不符（缺 log.entries）")
    got: dict[tuple[str, str], tuple[datetime, dict]] = {}
    for e in entries:
        req, resp = e.get("request") or {}, e.get("response") or {}
        if not str(req.get("url", "")).endswith("/services/content/get") or resp.get("status") != 200:
            continue
        try:
            cid = json.loads((req.get("postData") or {}).get("text") or "{}")["contentId"]
            content = resp.get("content") or {}
            text = content.get("text") or ""
            if content.get("encoding") == "base64":
                text = base64.b64decode(text).decode("utf-8")
            body = json.loads(text)
        except (KeyError, ValueError, TypeError):
            continue
        t = ensure_utc(datetime.fromisoformat(str(e["startedDateTime"]).replace("Z", "+00:00")))
        key = (cid.get("type"), cid.get("id"))
        if key not in got or t > got[key][0]:
            got[key] = (t, body)

    raw: dict[str, Any] = {"parser_version": PARSER_VERSION, "tournaments": [], "event_details": {},
                           "capture": "har"}
    used: list[datetime] = []
    by_tour: dict[str, dict] = {}
    for (ctype, cid), (t, body) in got.items():
        if ctype == "eventGroup":
            events = ((body.get("data") or {}).get("events") or [])
            names = {clean(ev.get("tournamentname")) or clean((ev.get("markets") or [{}])[0].get("tournamentname"))
                     for ev in events}
            names = {n for n in names if n}
            nba = [n for n in names if n in NBA_TOURNAMENTS]
            if not nba:
                continue
            name = nba[0]
            tour = by_tour.setdefault(name, {"name": name, "stage": NBA_TOURNAMENTS[name], "numevents": 0,
                                             "event_groups": []})
            tour["event_groups"].append(body)
            tour["numevents"] += len(events)
            used.append(t)
    for (ctype, cid), (t, body) in got.items():
        if ctype == "event" and any(clean(ev.get("idfoevent")) == cid for tour in by_tour.values()
                                    for eg in tour["event_groups"] for ev in (eg.get("data") or {}).get("events") or []):
            raw["event_details"][cid] = body
            used.append(t)
    raw["tournaments"] = list(by_tour.values())
    if not used:
        raise ParserChanged("HAR 內沒有任何 NBA 聯賽的 eventGroup 回應（請在 NBA 聯賽頁面匯出）")
    return raw, max(used)
