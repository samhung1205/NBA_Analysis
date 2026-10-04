"""D.1 The Odds API adapter：多 bookmaker 保留、fixture 正規化、額度標頭、缺盤、錯誤分類（純邏輯，不打網路）"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.odds import oddsapi
from core.odds.errors import AuthFailed, NotConfigured, ParserChanged, QuotaLow, RateLimited

FX = Path(__file__).parent / "fixtures" / "odds"
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def raw():
    return json.loads((FX / "oddsapi_raw_synthetic.json").read_text())


def test_multi_bookmaker_preserved_never_averaged():
    res = oddsapi.parse_raw(raw())
    e1 = next(e for e in res.events if e.home_name == "New York Knicks")
    books = {(s.bookmaker, s.market) for s in e1.snapshots}
    assert ("draftkings", "spread") in books and ("fanduel", "spread") in books
    dk = next(s for s in e1.snapshots if s.bookmaker == "draftkings" and s.market == "spread")
    fd = next(s for s in e1.snapshots if s.bookmaker == "fanduel" and s.market == "spread")
    assert (dk.line, dk.home_odds) == (-7.5, 1.91) and (fd.line, fd.home_odds, fd.away_odds) == (-7.0, 1.95, 1.87)
    assert ("fanduel", "total") not in books                    # 該家沒有大小分 → 不造資料
    assert len(e1.snapshots) == 5 + 3                           # 全場 5 + 上半場 3（只有 draftkings）


def test_fixture_normalization_and_outcome_mapping():
    res = oddsapi.parse_raw(raw())
    e1 = next(e for e in res.events if e.home_name == "New York Knicks")
    fd_ml = next(s for s in e1.snapshots if s.bookmaker == "fanduel" and s.market == "ml")
    assert (fd_ml.home_odds, fd_ml.away_odds) == (1.4, 3.0)    # outcome 順序相反也正確對應
    dk = {s.market: s for s in e1.snapshots if s.bookmaker == "draftkings"}
    assert dk["spread"].model_threshold == 7.5 and dk["spread"].away_line == 7.5
    assert dk["total"].line == 224.5 and dk["total"].model_target == "total"
    assert dk["h1_spread"].model_target == "h1_margin" and dk["h1_spread"].model_threshold == 4.0
    assert dk["h1_ml"].outcome_set == "two_way" and dk["h1_total"].line == 112.5
    assert dk["total"].source_updated_at.isoformat() == "2026-10-20T09:55:40+00:00"   # market 層 last_update 優先
    assert dk["spread"].raw["bookmaker_title"] == "DraftKings" and dk["spread"].source_event_id.startswith("e1")
    e2 = next(e for e in res.events if e.home_name == "Los Angeles Clippers")
    assert {s.market for s in e2.snapshots} == {"ml", "spread", "total"}


def test_missing_and_malformed_markets():
    r = raw()
    bm = r["odds"][1]["bookmakers"][0]
    bm["markets"][0]["outcomes"][0]["name"] = "LA Clippers"          # 名稱與 event.home_team 不一致 → 不猜
    bm["markets"][1]["outcomes"].pop()                               # 缺一邊
    bm["markets"][2]["outcomes"][1]["point"] = 220.0                 # 大小線不一致
    e2 = next(e for e in oddsapi.parse_raw(r).events if e.source_event_id.startswith("e2"))
    assert e2.snapshots == []
    assert sorted(i["reason"] for i in e2.invalid) == ["asymmetric_total", "missing_side", "unexpected_outcome"]
    r2 = raw()
    r2["odds"][0]["bookmakers"] = []
    r2["event_odds"] = {}
    assert oddsapi.parse_raw(r2).events[0].snapshots == []          # 沒有 bookmaker 開盤：不是錯誤
    r3 = raw()
    del r3["odds"][0]["bookmakers"]
    with pytest.raises(ParserChanged):
        oddsapi.parse_raw(r3)


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, json.dumps(body)

    def json(self):
        return self._body


def fake_get(routes, calls):
    def get(url, params=None, timeout=None):
        calls.append((url.replace(oddsapi.BASE_URL, ""), dict(params)))
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(url)
    return get


def test_quota_metadata_and_free_events_call_first():
    r, calls = raw(), []
    hdr = {"x-requests-remaining": "471", "x-requests-used": "29", "x-requests-last": "3"}
    c = oddsapi.OddsApiClient("k", http_get=fake_get({"/events": Resp(200, r["events_listing"], {**hdr, "x-requests-last": "0"}),
                                                      "/odds": Resp(200, r["odds"], hdr)}, calls))
    out = oddsapi.fetch_raw(c, now=NOW)
    assert [p for p, _ in calls] == ["/sports/basketball_nba/events", "/sports/basketball_nba/odds"]
    assert calls[1][1]["markets"] == "h2h,spreads,totals" and calls[1][1]["oddsFormat"] == "decimal"
    assert calls[1][1]["regions"] == "us" and calls[1][1]["apiKey"] == "k"
    assert out["quota"] == {"remaining": 471, "used": 29, "last": 3} and c.n_requests == 2


def test_no_events_spends_no_quota():
    calls = []
    c = oddsapi.OddsApiClient("k", http_get=fake_get({"/events": Resp(200, [], {"x-requests-remaining": "400"})}, calls))
    out = oddsapi.fetch_raw(c, now=NOW)
    assert out["odds"] == [] and len(calls) == 1


def test_quota_reserve_blocks_paid_call():
    calls = []
    c = oddsapi.OddsApiClient("k", quota_reserve=25, http_get=fake_get(
        {"/events": Resp(200, raw()["events_listing"], {"x-requests-remaining": "26"})}, calls))
    with pytest.raises(QuotaLow):
        oddsapi.fetch_raw(c, now=NOW)
    assert len(calls) == 1


@pytest.mark.parametrize("status,exc", [(401, AuthFailed), (429, RateLimited), (422, ParserChanged)])
def test_http_errors_classified(status, exc):
    c = oddsapi.OddsApiClient("k", http_get=fake_get({"/events": Resp(status, {"message": "x"})}, []))
    with pytest.raises(exc):
        oddsapi.fetch_raw(c, now=NOW)


def test_missing_key_is_not_configured():
    with pytest.raises(NotConfigured):
        oddsapi.OddsApiClient(None)
