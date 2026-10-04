"""
盤口來源錯誤分類（Phase D.1）
------------------------------------------------------------
每個例外帶一個 outcome 代碼，寫入 odds_fetch_runs.outcome 與 data_sources.last_outcome，
讓「今天沒有盤」「parser 壞了」「額度不足」「被擋」在系統狀態頁上可以分辨。
"""
from __future__ import annotations

# outcome → data_sources.last_status（系統狀態頁既有的 ok / warn / error 語意）
OUTCOME_STATUS = {
    "success": "ok",
    "no_nba_markets": "ok",          # 來源正常、只是目前沒有 NBA 盤（休賽 / 尚未開盤）
    "partial": "warn",               # 有寫入，但部分事件/市場無法對應或解析
    "quota_low": "warn",             # 額度低於保留門檻，主動略過
    "rate_limited": "warn",
    "not_configured": "warn",        # 缺 API key 等設定
    "parser_changed": "error",       # 預期有 NBA 事件/市場，但結構對不上
    "auth_failed": "error",
    "network_failure": "error",
    "blocked": "error",              # 例如 Cloudflare challenge 未自動通過（不繞過）
    "error": "error",
}


class OddsSourceError(RuntimeError):
    outcome = "error"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class ParserChanged(OddsSourceError):
    outcome = "parser_changed"


class Blocked(OddsSourceError):
    outcome = "blocked"


class RateLimited(OddsSourceError):
    outcome = "rate_limited"


class AuthFailed(OddsSourceError):
    outcome = "auth_failed"


class NetworkFailure(OddsSourceError):
    outcome = "network_failure"


class QuotaLow(OddsSourceError):
    outcome = "quota_low"


class NotConfigured(OddsSourceError):
    outcome = "not_configured"
