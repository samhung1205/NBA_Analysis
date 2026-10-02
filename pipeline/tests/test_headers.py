"""NBA Stats request headers：沿用 nba_api 內建預設，不再自訂 UA"""
import inspect

from core import config
from core.sources import nba_cdn, nba_stats


def test_nba_stats_no_longer_overrides_headers():
    code = "\n".join(l for l in inspect.getsource(nba_stats).splitlines() if not l.lstrip().startswith("#"))
    assert "_headers" not in code                     # 舊的 _headers()（Chrome 120 UA 覆寫）已移除
    assert "headers=" not in code                     # 任何 endpoint 呼叫都不再傳 headers=
    assert "Chrome/120" not in code
    assert not hasattr(config.settings, "nba_api_user_agent")


def test_stats_endpoints_called_without_custom_headers(monkeypatch):
    import nba_api.stats.endpoints as eps

    seen = {}

    class Fake:
        def __init__(self, **kw):
            seen.update(kw)

        def get_dict(self):
            return {"scoreboard": {"games": []}}

    monkeypatch.setattr(eps.scoreboardv3, "ScoreboardV3", Fake)
    monkeypatch.setattr(nba_stats, "_sleep_polite", lambda: None)
    from datetime import date
    nba_stats.fetch_games_for_et_dates([date(2026, 1, 15)])
    assert "headers" not in seen                      # → nba_api 使用 NBAStatsHTTP.headers 完整預設
    assert seen["timeout"] == nba_stats.REQUEST_TIMEOUT   # timeout 仍保留


def test_polite_pacing_and_retry_still_configured():
    assert nba_stats.REQUEST_TIMEOUT >= 10
    assert config.settings.nba_api_min_delay > 0
    assert nba_stats.fetch_box_advanced.retry.stop.max_attempt_number >= 2     # tenacity retry 仍在


def test_cdn_headers_are_nba_api_defaults_minus_host():
    from nba_api.stats.library.http import NBAStatsHTTP

    h = nba_cdn._headers()
    assert "Host" not in h and "host" not in h
    expected = {k: v for k, v in NBAStatsHTTP.headers.items() if k != "Host"}
    assert h == expected                              # 不改 UA、不加料
    assert "Chrome/120" not in h["User-Agent"]
