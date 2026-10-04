"""D.1 台灣運彩 parser：真實回應 fixture（2026-10-04，一般瀏覽器擷取）、缺盤、改版偵測、HAR 匯入（純邏輯）"""
import base64
import copy
import json
from pathlib import Path

import pytest

from core.odds import twsport
from core.odds.canonical import CLOSED, OPEN, SUSPENDED, THREE_WAY
from core.odds.errors import Blocked, ParserChanged

FX = Path(__file__).parent / "fixtures" / "odds"


def raw_fixture():
    return json.loads((FX / "twsport_raw_preseason_20261004.json").read_text(encoding="utf-8"))


def detail_fixture():
    return json.loads((FX / "twsport_event_detail_h1_20261004.json").read_text(encoding="utf-8"))["data"]


def by_market(ev):
    return {s.market: s for s in ev.snapshots}


def test_preseason_fixture_full_game_markets():
    res = twsport.parse_raw(raw_fixture())
    assert res.n_nba_events_listed == 2 and len(res.events) == 2 and res.n_invalid == 0
    gsw_lac, uta_den = sorted(res.events, key=lambda e: e.source_event_id, reverse=True)
    assert (gsw_lac.home_name, gsw_lac.away_name, gsw_lac.stage) == ("洛杉磯快艇", "金州勇士", "preseason")
    assert gsw_lac.commence_utc.isoformat() == "2026-10-04T23:00:00+00:00"      # 07:00 +08:00
    m = by_market(gsw_lac)
    # 網站顯示：客 金州勇士 1.52 / 主 洛杉磯快艇 1.94
    assert (m["ml"].home_odds, m["ml"].away_odds) == (1.94, 1.52)
    # 洛杉磯快艇 +2.5（主隊受讓）/ 金州勇士 −2.5
    sp = m["spread"]
    assert (sp.line, sp.away_line, sp.home_odds, sp.away_odds) == (2.5, -2.5, 1.75, 1.68)
    assert sp.model_threshold == -2.5          # 主隊過盤 ⇔ margin > −2.5
    assert (m["total"].line, m["total"].over_odds, m["total"].under_odds) == (223.5, 1.7, 1.73)
    # 丹佛金塊 −3.5（主隊讓分）→ threshold +3.5
    d = by_market(uta_den)["spread"]
    assert (d.line, d.away_line, d.model_threshold, d.home_odds, d.away_odds) == (-3.5, 3.5, 3.5, 1.7, 1.73)
    for s in gsw_lac.snapshots + uta_den.snapshots:
        assert s.status == OPEN and s.bookmaker == "twsport" and s.source == "twsport"
        assert s.raw["market_ref"] and s.raw["outcomes"] and s.raw["outcomes"][0]["price_format"] == "fractional"
    assert sp.raw["outcomes"][0]["price_raw"] in ("17/25", "3/4")


def test_event_detail_h1_markets_and_main_line_selection():
    ev = twsport.parse_event(detail_fixture(), "regular", "test")
    m = by_market(ev)
    assert set(m) == {"ml", "spread", "total", "h1_ml", "h1_spread", "h1_total"}   # 單雙 / 球隊大小 忽略
    assert m["spread"].line == 1.5 and m["spread"].raw["main_rule"] == "ismainline"   # 1.5 / 2.5 / 3.5 取主盤
    h1 = m["h1_ml"]
    assert h1.outcome_set == THREE_WAY and (h1.home_odds, h1.away_odds, h1.draw_odds) == (1.7, 1.7, 10.0)
    assert m["h1_spread"].line == 0.5 and m["h1_spread"].model_target == "h1_margin"
    assert m["h1_spread"].raw["main_rule"] == "single_line"
    assert m["h1_total"].line == 80.5 and m["h1_total"].model_target == "h1_total"
    assert ev.home_name == "金州瓦爾基里" and ev.away_name == "拉斯維加斯王牌"          # \r 已去除


def test_missing_market_is_not_fabricated():
    d = detail_fixture()
    d["markets"] = [m for m in d["markets"] if not m["externalreference"].startswith(("60", "66", "68"))]
    m = by_market(twsport.parse_event(d, "regular", "t"))
    assert set(m) == {"ml", "spread", "total"}


def test_ambiguous_main_line_is_skipped_not_guessed():
    d = detail_fixture()
    for mk in d["markets"]:
        if mk["externalreference"].startswith("223"):
            mk["ismainline"] = False
    ev = twsport.parse_event(d, "regular", "t")
    assert "spread" not in by_market(ev)
    assert any(i["reason"] == "ambiguous_main_line" for i in ev.invalid)


def test_suspended_and_closed_state_preserved():
    d = detail_fixture()
    for mk in d["markets"]:
        if mk["externalreference"] == "219":
            mk["istradable"] = False
        if mk["externalreference"].startswith("225"):
            mk["idfobolifestate"] = "C"
        if mk["externalreference"].startswith("68"):
            mk["selections"][0]["idfoselectionsuspensiontype"] = "SUSP"
    m = by_market(twsport.parse_event(d, "regular", "t"))
    assert m["ml"].status == SUSPENDED and m["total"].status == CLOSED and m["h1_total"].status == SUSPENDED
    assert m["spread"].status == OPEN
    d2 = detail_fixture()
    d2["istradable"] = False
    assert all(s.status == SUSPENDED for s in twsport.parse_event(d2, "regular", "t").snapshots)


@pytest.mark.parametrize("mutate,reason", [
    (lambda m: m["selections"][0].update(name="拉斯維加斯王牌 +1.5"), "line_sign_mismatch"),
    (lambda m: m["selections"][0].update(hadvalue="H"), "side_mismatch"),
    (lambda m: m.update(line="2.5"), "line_mismatch"),
    (lambda m: m["selections"][0].update(currentpriceup="0"), "price_non_positive"),
    (lambda m: m["selections"][0].update(externalreference="9999"), "unexpected_outcome"),
])
def test_cross_checks_reject_inconsistent_spread(mutate, reason):
    d = detail_fixture()
    main = next(mk for mk in d["markets"] if mk["externalreference"] == "223/hcp=1.5")
    mutate(main)
    ev = twsport.parse_event(d, "regular", "t")
    assert "spread" not in by_market(ev)
    assert [i["reason"] for i in ev.invalid] == [reason]


def test_total_side_mapped_by_ref_and_name_not_hadvalue():
    """台彩大小分的 hadvalue 是 A=大、H=小；只能靠 ref 12/13 與「大/小」。"""
    d = detail_fixture()
    tot = next(mk for mk in d["markets"] if mk["externalreference"].startswith("225"))
    assert [s["hadvalue"] for s in tot["selections"]] == ["A", "H"]
    tot["selections"][0]["name"] = "小 161.5"
    ev = twsport.parse_event(d, "regular", "t")
    assert "total" not in by_market(ev) and ev.invalid[0]["reason"] == "side_mismatch"


def test_name_ref_mismatch_detected():
    d = detail_fixture()
    next(mk for mk in d["markets"] if mk["externalreference"] == "60")["name"] = "不讓分"
    ev = twsport.parse_event(d, "regular", "t")
    assert "h1_ml" not in by_market(ev) and ev.invalid[0]["reason"] == "name_ref_mismatch"


# ---- 改版偵測 ---------------------------------------------------------------- #

def test_structure_change_raises_parser_changed():
    raw = raw_fixture()
    raw["tournaments"][2]["event_groups"][0]["data"].pop("events")
    with pytest.raises(ParserChanged):
        twsport.parse_raw(raw)
    with pytest.raises(ParserChanged):
        twsport.parse_raw({"unexpected": True})
    ev = raw_fixture()["tournaments"][2]["event_groups"][0]["data"]["events"][0]
    ev.pop("participantname_home")
    with pytest.raises(ParserChanged):
        twsport.parse_event(ev, "preseason", "t")


def test_events_with_no_recognized_markets_are_reported():
    raw = raw_fixture()
    for ev in raw["tournaments"][2]["event_groups"][0]["data"]["events"]:
        for mk in ev["markets"]:
            mk["externalreference"] = "X" + mk["externalreference"]     # 參照碼體系改版
    res = twsport.parse_raw(raw)
    assert res.n_snapshots == 0 and res.n_invalid == 2
    assert {i["reason"] for e in res.events for i in e.invalid} == {"no_recognized_markets"}


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls, self.n_requests = responses, [], 0

    def get(self, ctype, cid):
        self.calls.append((ctype, cid))
        r = self.responses.get((ctype, cid))
        if isinstance(r, Exception):
            raise r
        return r


def _nav(nodes):
    return {"data": {"bonavigationnodes": nodes}}


def test_fetch_raw_navigation_and_change_detection():
    sport = _nav([{"idfwbonavigation": "34800.1", "name": "美國"}])
    country = _nav([{"idfwbonavigation": "34801.1", "name": "美國職籃\r", "numevents": "0"},
                    {"idfwbonavigation": "37272.1", "name": "美國職籃熱身賽\r", "numevents": "2"},
                    {"idfwbonavigation": "36772.1", "name": "美國女子職籃\r", "numevents": "2"}])
    tour = {"data": {"marketgroups": [{"idfwmarketgroup": "63733.1", "idfwmarketgrouptype": "MATCHESCOUPON"}]}}
    eg = raw_fixture()["tournaments"][2]["event_groups"][0]
    c = FakeClient({("boNavigationList", "1355/34765.1"): sport, ("boNavigationList", "1355/34800.1"): country,
                    ("boNavigationList", "1355/37272.1"): tour, ("eventGroup", "63733.1"): eg,
                    ("event", "3495765.1"): Blocked("x"), ("event", "3495764.1"): {"data": eg["data"]["events"][1]}})
    with pytest.raises(Blocked):               # blocked 不吞掉
        twsport.fetch_raw(c)
    c.responses[("event", "3495765.1")] = {"data": eg["data"]["events"][0]}
    raw = twsport.fetch_raw(c)
    assert [t["name"] for t in raw["tournaments"]] == ["美國職籃", "美國職籃熱身賽"]
    assert ("boNavigationList", "1355/34801.1") not in c.calls          # 0 場不打
    assert "美國女子職籃" in raw["other_tournaments"]
    assert len(twsport.parse_raw(raw).events) == 2
    # 改版：找不到「美國職籃」節點 / 有場次卻沒有 coupon
    c.responses[("boNavigationList", "1355/34800.1")] = _nav([{"idfwbonavigation": "1", "name": "美國女子職籃"}])
    with pytest.raises(ParserChanged):
        twsport.fetch_raw(c)
    c.responses[("boNavigationList", "1355/34800.1")] = country
    c.responses[("boNavigationList", "1355/37272.1")] = {"data": {"marketgroups": []}}
    with pytest.raises(ParserChanged):
        twsport.fetch_raw(c)
    c.responses[("boNavigationList", "1355/34765.1")] = {"data": {}}
    with pytest.raises(ParserChanged):
        twsport.fetch_raw(c)


def test_duplicate_events_across_groups_deduped():
    raw = raw_fixture()
    t = raw["tournaments"][2]
    t["event_groups"].append(copy.deepcopy(t["event_groups"][0]))
    assert len(twsport.parse_raw(raw).events) == 2


def test_har_import_uses_latest_response_time():
    eg = raw_fixture()["tournaments"][2]["event_groups"][0]
    det = {"data": eg["data"]["events"][0]}

    def entry(ctype, cid, t, body, b64=False):
        text = json.dumps(body, ensure_ascii=False)
        content = {"text": base64.b64encode(text.encode()).decode(), "encoding": "base64"} if b64 else {"text": text}
        return {"startedDateTime": t,
                "request": {"url": "https://www-talo-ssb-pr.sportslottery.com.tw/services/content/get",
                            "postData": {"text": json.dumps({"contentId": {"type": ctype, "id": cid}})}},
                "response": {"status": 200, "content": content}}
    har = {"log": {"entries": [entry("eventGroup", "63733.1", "2026-10-04T04:40:00.000Z", eg),
                               entry("event", "3495765.1", "2026-10-04T04:41:30.000Z", det, b64=True),
                               entry("event", "999.1", "2026-10-04T05:00:00.000Z", {"data": {}})]}}
    raw, fetched_at = twsport.raw_from_har(har)
    assert fetched_at.isoformat() == "2026-10-04T04:41:30+00:00"        # 無關的回應不影響時間
    res = twsport.parse_raw(raw)
    assert len(res.events) == 2 and res.n_snapshots == 6
    with pytest.raises(ParserChanged):
        twsport.raw_from_har({"log": {"entries": []}})


def test_challenge_title_detection():
    assert twsport._is_challenge_title("Just a moment...")
    assert twsport._is_challenge_title("請稍候...")
    assert not twsport._is_challenge_title("ORAKO Sportsbook WebApp k8s")
