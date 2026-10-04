"""
環境設定
------------------------------------------------------------
規格書 §0：階段一與階段二共用同一組環境變數（見 repo 根目錄 .env.example）。
本機開發時依序找 pipeline/.env → 根目錄 .env → 根目錄 .dev.vars（Node 慣例），
第一個存在的檔案生效；真實環境變數（Docker/Railway 注入）一律優先，不會被覆蓋。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PIPELINE_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PIPELINE_DIR.parent

for _candidate in (_PIPELINE_DIR / ".env", _REPO_ROOT / ".env", _REPO_ROOT / ".dev.vars"):
    if _candidate.exists():
        load_dotenv(_candidate, override=False)
        break


def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    v = os.environ.get(key)
    return float(v) if v else default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    return default if v in (None, "") else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str
    odds_api_key: str | None = field(default=None)
    balldontlie_api_key: str | None = field(default=None)
    nba_api_min_delay: float = field(default=1.5)
    nba_api_max_delay: float = field(default=4.0)
    twsport_base_url: str | None = field(default=None)
    alert_webhook_url: str | None = field(default=None)
    model_version: str = field(default="elo-v1.0")
    kelly_fraction: float = field(default=0.25)
    # ---- Phase D.1 盤口 ---- #
    twsport_enabled: bool = field(default=True)
    twsport_headless: bool = field(default=True)
    twsport_browser_channel: str | None = field(default="chrome")
    odds_api_regions: str = field(default="us")
    odds_api_bookmakers: str | None = field(default=None)
    odds_api_include_h1: bool = field(default=False)
    odds_api_quota_reserve: int = field(default=25)
    odds_api_quota_warn: int = field(default=100)
    odds_api_cron_hours: str = field(default="0,6,12,18")


def load_settings() -> Settings:
    database_url = _env("DATABASE_URL") or _env("DATABASE_URL_DIRECT")
    if not database_url:
        raise RuntimeError(
            "缺少 DATABASE_URL：請在 pipeline/.env 或根目錄 .env / .dev.vars 設定，"
            "詳見 .env.example"
        )
    return Settings(
        database_url=database_url,
        odds_api_key=_env("ODDS_API_KEY") or None,
        balldontlie_api_key=_env("BALLDONTLIE_API_KEY") or None,
        nba_api_min_delay=_env_float("NBA_API_MIN_DELAY", 1.5),
        nba_api_max_delay=_env_float("NBA_API_MAX_DELAY", 4.0),
        twsport_base_url=_env("TWSPORT_BASE_URL") or None,
        alert_webhook_url=_env("ALERT_WEBHOOK_URL") or None,
        model_version=_env("MODEL_VERSION") or "elo-v1.0",
        kelly_fraction=_env_float("KELLY_FRACTION", 0.25),
        twsport_enabled=_env_bool("TWSPORT_ENABLED", True),
        twsport_headless=_env_bool("TWSPORT_HEADLESS", True),
        twsport_browser_channel=(_env("TWSPORT_BROWSER_CHANNEL", "chrome") or None),
        odds_api_regions=_env("ODDS_API_REGIONS") or "us",
        odds_api_bookmakers=_env("ODDS_API_BOOKMAKERS") or None,
        odds_api_include_h1=_env_bool("ODDS_API_INCLUDE_H1", False),
        odds_api_quota_reserve=int(_env_float("ODDS_API_QUOTA_RESERVE", 25)),
        odds_api_quota_warn=int(_env_float("ODDS_API_QUOTA_WARN", 100)),
        odds_api_cron_hours=_env("ODDS_API_CRON_HOURS") or "0,6,12,18",
    )


settings = load_settings()
