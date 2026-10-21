"""來源檢查不碰 DB、不重試被擋來源；裁判 NBA/WNBA 與未公布/改版不能混淆。"""
import json
from datetime import datetime, timezone

import pytest
import requests

import run_source_check as check
from core.sources.referees import parse_assignments

NOW = datetime(2026, 10, 15, 14, tzinfo=timezone.utc)
HEADER = "<tr><th>Game</th><th>Crew Chief</th><th>Referee</th><th>Umpire</th><th>Alternate</th></tr>"
ROW = "<tr><td>Boston @ New York</td><td><a>Alpha (#1)</a></td><td>Beta</td><td>Gamma</td><td></td></tr>"


def page(rows=ROW):
    return ('<h1>NBA Referee Assignments</h1><div class="entry-meta">October 15, 2026</div>'
            f'<div class="nba-refs-content"><table>{HEADER}{rows}</table></div>'
            '<h1>WNBA Referee Assignments</h1><div class="entry-meta">October 16, 2026</div>'
            f'<div class="wnba-refs-content"><table>{HEADER}{ROW}</table></div>')


def test_referees_excludes_wnba_and_preserves_observation_time():
    result = parse_assignments(page(), observed_at=NOW)
    assert result["report_date_et"] == "2026-10-15"
    assert result["observed_at"] == NOW.isoformat()
    assert len(result["assignments"]) == 1
    assert result["assignments"][0]["crew_chief"] == "Alpha (#1)"


def test_empty_nba_table_is_valid_but_not_wnba_assignments():
    assert parse_assignments(page(""), observed_at=NOW)["assignments"] == []


@pytest.mark.parametrize("html", ["<html>challenge</html>", page().replace("Crew Chief", "Changed"),
                                    page().replace("October 15, 2026", "unknown"), page(ROW.replace("Beta", ""))])
def test_invalid_or_changed_page_is_not_no_games(html):
    with pytest.raises(ValueError):
        parse_assignments(html, observed_at=NOW)


class Response:
    status_code = 200
    text = page()

    def __init__(self, data=None):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def mock_get(url, **_):
    if "schedule" in url:
        return Response({"leagueSchedule": {"gameDates": [{"games": [{"arenaName": "Arena"}]}]}})
    if "boxscore" in url:
        return Response({"game": {"gameId": "0022400001", "homeTeam": {"players": [{}]},
                                  "awayTeam": {"players": [{}]}, "officials": [{}, {}, {}]}})
    if "playbyplay" in url:
        return Response({"game": {"gameId": "0022400001", "actions": [{"actionType": "substitution"}]}})
    return Response()


def test_four_read_only_sources_no_stats_or_lottery(monkeypatch):
    monkeypatch.setattr(check, "now_utc", lambda: NOW)
    urls = []

    def get(url, **kw):
        urls.append(url)
        return mock_get(url, **kw)

    results = check.check_sources(get=get)
    assert len(urls) == 4 and all("stats.nba.com" not in u and "sportslottery" not in u for u in urls)
    assert all(r["status"] == "ready" for r in results)
    assert results[2]["detail"]["validated_five_player_lineups"] is False


def test_failed_requests_do_not_retry_or_expose_response():
    urls = []

    def get(url, **_):
        urls.append(url)
        response = requests.Response()
        response.status_code = 403
        raise requests.HTTPError("SECRET_RESPONSE_COOKIE", response=response)

    result = check.check_sources(get=get)
    assert len(urls) == 4 and all(r["http_status"] == 403 for r in result)
    assert all(r["status"] == "unavailable" for r in result)
    assert "SECRET" not in json.dumps(result)


def test_referee_empty_and_stale_classifications(monkeypatch):
    monkeypatch.setattr(check, "now_utc", lambda: NOW)
    monkeypatch.setattr(Response, "text", page(""))
    assert check.check_sources(get=mock_get)[-1]["status"] == "waiting"
    monkeypatch.setattr(Response, "text", page().replace("October 15, 2026", "October 14, 2026"))
    assert check.check_sources(get=mock_get)[-1]["status"] == "stale"


def test_empty_pbp_not_ready():
    def get(url, **kw):
        r = mock_get(url, **kw)
        if "playbyplay" in url:
            r.data["game"]["actions"] = []
        return r

    assert check.check_sources(get=get)[2]["status"] == "unavailable"


def test_invalid_game_id_never_sends_request():
    with pytest.raises(ValueError):
        check.check_sources("../../secret", get=lambda *a, **k: pytest.fail("unexpected request"))
