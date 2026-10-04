"""
凍結的 production risk policy（risk-v1）
------------------------------------------------------------
這些是保守的 operational risk limits，**不是**從歷史 ROI 最佳化出來的；自 2026-10-04 起凍結，
D.4 ROI backtest 不得回頭調整（要改 → 新版本字串，例如 risk-v2，舊 sizing 列保留）。

    full Kelly → × 0.25（1/4 Kelly）→ 單筆 ≤ bankroll 2% → 同一場合計 ≤ 3% → 同一 betting day 合計 ≤ 8%

所有數值都是 bankroll 比例（0.0125 = bankroll 的 1.25%）；核心不假設 bankroll 金額。

報價新鮮度（stale quote guard）也屬於 risk-v1：
    last_seen_age = analysis_as_of − 該報價最後一次被輪詢確認的時間（odds_snapshots.last_seen_at；舊列為 fetched_at）
    last_seen_age > STALE_AFTER_POLL_INTERVALS × 來源輪詢間隔 → stale_quote（不 actionable）
    依據：D.1 的輪詢間隔（core/odds/ingest.SOURCES：台彩 30 分、The Odds API 6 小時）與系統狀態頁既有的
    「超過預期間隔 2 倍 = stale」規則。代表「最近的輪詢沒有再看到這個報價」（輪詢失敗或盤口已撤），
    不是「價格已變」——輪詢之間的變動本來就看不到（D.1 §9）。寬鬆的 safety guard，不是針對個別來源調出來的門檻。
    未知來源（沒有已宣告的輪詢間隔）→ 無法定義 → stale_quote（poll_interval_unknown），不偷偷假設。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Mapping

SIZING_VERSION = "sizing-v1"          # qualification / portfolio 演算法版本（與 risk policy 參數分開）


@dataclass(frozen=True)
class RiskPolicy:
    version: str
    kelly_multiplier: float
    max_bet_fraction: float
    max_game_fraction: float
    max_day_fraction: float
    stale_after_poll_intervals: float
    source_poll_interval_min: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        for k in ("kelly_multiplier", "max_bet_fraction", "max_game_fraction", "max_day_fraction",
                  "stale_after_poll_intervals"):
            v = getattr(self, k)
            if not (isinstance(v, (int, float)) and v > 0):
                raise ValueError(f"{k} 必須 > 0（得到 {v!r}）")
        if self.kelly_multiplier > 1:
            raise ValueError("kelly_multiplier 不得 > 1（不允許超過 full Kelly）")
        if not (self.max_bet_fraction <= self.max_game_fraction <= self.max_day_fraction <= 1):
            raise ValueError("必須 max_bet ≤ max_game ≤ max_day ≤ 1")
        object.__setattr__(self, "source_poll_interval_min", MappingProxyType(dict(self.source_poll_interval_min)))

    def max_quote_age(self, source: str | None) -> timedelta | None:
        """該來源可接受的最大 last_seen_age；None = 未知來源（不可定義）。"""
        m = self.source_poll_interval_min.get(source or "")
        return None if m is None else timedelta(minutes=m * self.stale_after_poll_intervals)

    def poll_interval(self, source: str | None) -> timedelta | None:
        m = self.source_poll_interval_min.get(source or "")
        return None if m is None else timedelta(minutes=m)

    def to_dict(self) -> dict:
        return {"risk_policy_version": self.version, "kelly_multiplier": self.kelly_multiplier,
                "max_bet_fraction": self.max_bet_fraction, "max_game_fraction": self.max_game_fraction,
                "max_day_fraction": self.max_day_fraction,
                "stale_after_poll_intervals": self.stale_after_poll_intervals,
                "source_poll_interval_min": dict(self.source_poll_interval_min)}


KELLY_MULTIPLIER = 0.25
MAX_BET_BANKROLL_FRACTION = 0.02
MAX_GAME_BANKROLL_FRACTION = 0.03
MAX_DAY_BANKROLL_FRACTION = 0.08

RISK_V1 = RiskPolicy(
    version="risk-v1",
    kelly_multiplier=KELLY_MULTIPLIER,
    max_bet_fraction=MAX_BET_BANKROLL_FRACTION,
    max_game_fraction=MAX_GAME_BANKROLL_FRACTION,
    max_day_fraction=MAX_DAY_BANKROLL_FRACTION,
    stale_after_poll_intervals=2.0,
    source_poll_interval_min={"twsport": 30, "oddsapi": 6 * 60},   # = D.1 core/odds/ingest.SOURCES（凍結當時的值）
)

PRODUCTION_RISK_POLICY = RISK_V1
REGISTERED_POLICIES: dict[str, RiskPolicy] = {RISK_V1.version: RISK_V1}


def assert_registered(policy: RiskPolicy) -> None:
    """寫入 DB 前：版本字串必須對應到登記的參數（不允許用 risk-v1 的名字寫入別的參數）。"""
    reg = REGISTERED_POLICIES.get(policy.version)
    if reg is None or reg.to_dict() != policy.to_dict():
        raise ValueError(f"risk policy {policy.version} 未登記或參數與登記版本不同；只能 dry-run")
