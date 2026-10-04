"""
凍結的 execution policy（execution-v1）與 strategy scope
------------------------------------------------------------
execution-v1 自 2026-10-04 起凍結，**在看到任何 ROI 之前**定義；不得依 ROI 回頭調整。
要測其他時點 / 規則 → 新版本字串（execution-v2…），不得冒充 execution-v1。

    decision_time      = scheduled_tipoff − 60 分鐘（C.5C / C.5D：T-60 是 production final 預測時點；T-60 → T-15 無可測得增益）
    analysis_as_of     = decision_time（不是 job 實際執行時間；之後才出現的資料一律不用）
    betting_day        = scheduled_tipoff 的 Asia/Taipei 日曆日（= D.3 sizing.engine.betting_day）
    stake base         = day_start_bankroll（同一 betting day 內固定；當日已結算的比賽不增加當日額度）
    sequential order   = decision_time 遞增；同一 decision_time 的比賽是同一個決策批次（一起 sizing，與 game_id 無關）
    daily budget       = risk-v1 max_day_fraction − 當日已 committed 的 stake 比例（earlier commitments 占用額度）
    eligibility        = D.3 risk-v1：actionable 且 final_stake_fraction > 0（EV > 0；無最低 EV 門檻；不用 edge）
    unresolved stakes  = 前一日尚未結算（pending / ungradable）的 stake 不計入下一日 day_start（不猜結果）

只有 risk-v1（D.3 凍結）決定單筆 2% / 同場 3% / 單日 8%；這裡只加「同日跨決策時點」的 committed exposure 包裝，
不修改 risk-v1 任何參數。

Strategy scope（不混用 bookmaker）：
    primary     source=twsport, bookmaker=twsport  → taiwan_sports_lottery_strategy（本專案嚴格 ROI 目標）
    diagnostic  source=oddsapi, bookmaker=<單一 key> → international_market_diagnostic（不是台彩證據）
不做 best-book、line shopping、consensus、bookmaker averaging。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..sizing.policy import PRODUCTION_RISK_POLICY, SIZING_VERSION, RiskPolicy
from ..sizing import kelly
from ..pricing import engine as pricing_engine


@dataclass(frozen=True)
class ExecutionPolicy:
    version: str
    decision_offset: timedelta                 # decision_time = scheduled_tipoff − decision_offset
    max_evaluation_lag: timedelta              # prospective：job 晚於 T 超過此值 → no_bet（decision_window_missed）
    reschedule_tolerance: timedelta            # 決策後開賽時間變動超過此值 → ungradable（bookmaker 改期規則未驗證）
    result_known_after_tipoff: timedelta       # 歷史重建：開賽後多久視為結果已知（決定前一日 stake 是否已回到 bankroll）
    starting_bankroll_units: float
    betting_day_timezone: str
    risk_policy_version: str

    def to_dict(self) -> dict:
        return {"execution_policy_version": self.version,
                "decision_offset_minutes": self.decision_offset.total_seconds() / 60,
                "max_evaluation_lag_minutes": self.max_evaluation_lag.total_seconds() / 60,
                "reschedule_tolerance_minutes": self.reschedule_tolerance.total_seconds() / 60,
                "result_known_after_tipoff_minutes": self.result_known_after_tipoff.total_seconds() / 60,
                "starting_bankroll_units": self.starting_bankroll_units,
                "betting_day_timezone": self.betting_day_timezone,
                "risk_policy_version": self.risk_policy_version}


GAME_T_MINUS_MINUTES = 60

EXECUTION_V1 = ExecutionPolicy(
    version="execution-v1",
    decision_offset=timedelta(minutes=GAME_T_MINUS_MINUTES),
    max_evaluation_lag=timedelta(minutes=15),          # 排程每 5 分鐘 → 正常延遲 ≤ 5 分；3 次機會
    reschedule_tolerance=timedelta(minutes=60),
    result_known_after_tipoff=timedelta(hours=4),      # NBA 比賽約 2.5 小時 + 結算延遲；只用於歷史重建
    starting_bankroll_units=1.0,
    betting_day_timezone="Asia/Taipei",
    risk_policy_version=PRODUCTION_RISK_POLICY.version,
)
REGISTERED_EXECUTION_POLICIES: dict[str, ExecutionPolicy] = {EXECUTION_V1.version: EXECUTION_V1}
SETTLEMENT_VERSION = "settle-v1"


def assert_registered(policy: ExecutionPolicy, risk: RiskPolicy) -> None:
    """寫入 ledger 前：版本字串必須對應登記的參數；risk policy 必須是 execution policy 綁定的版本。"""
    reg = REGISTERED_EXECUTION_POLICIES.get(policy.version)
    if reg is None or reg.to_dict() != policy.to_dict():
        raise ValueError(f"execution policy {policy.version} 未登記或參數與登記版本不同；只能 dry-run")
    if risk.version != policy.risk_policy_version:
        raise ValueError(f"{policy.version} 綁定 {policy.risk_policy_version}，不可搭配 {risk.version}")
    from ..sizing.policy import assert_registered as assert_risk
    assert_risk(risk)


# ------------------------------------------------------------------ #
# Strategy scope                                                       #
# ------------------------------------------------------------------ #

PRIMARY = "primary"
DIAGNOSTIC = "diagnostic"
TWSPORT_LABEL = "taiwan_sports_lottery_strategy"
INTERNATIONAL_LABEL = "international_market_diagnostic"
KNOWN_SOURCES = ("twsport", "oddsapi")


@dataclass(frozen=True)
class StrategyScope:
    source: str
    bookmaker: str

    def __post_init__(self):
        if self.source not in KNOWN_SOURCES:
            raise ValueError(f"未知來源 {self.source}")
        if not self.bookmaker or self.bookmaker in ("*", "all", "best", "consensus"):
            raise ValueError("strategy scope 必須是單一 bookmaker（不做 best-book / consensus / 混合）")
        if self.source == "twsport" and self.bookmaker != "twsport":
            raise ValueError("twsport 的 bookmaker 只能是 twsport")
        if self.source == "oddsapi" and self.bookmaker == "twsport":
            raise ValueError("oddsapi 不可冒充 twsport")

    @property
    def kind(self) -> str:
        return PRIMARY if (self.source, self.bookmaker) == ("twsport", "twsport") else DIAGNOSTIC

    @property
    def evidence_label(self) -> str:
        """台彩策略證據只能來自 source=twsport；其他來源一律是國際盤診斷（永不標成台彩）。"""
        return TWSPORT_LABEL if self.source == "twsport" else INTERNATIONAL_LABEL

    def matches(self, row: dict) -> bool:
        return row.get("source") == self.source and (row.get("bookmaker") or row.get("source")) == self.bookmaker

    def to_dict(self) -> dict:
        return {"source": self.source, "bookmaker": self.bookmaker, "strategy_scope": self.kind,
                "evidence_label": self.evidence_label}


PRIMARY_SCOPE = StrategyScope("twsport", "twsport")


def strategy_id(policy: ExecutionPolicy, risk: RiskPolicy, scope: StrategyScope) -> str:
    """策略身分：execution / risk / sizing / pricing / kelly 版本 + 單一 source:bookmaker。任一改變 → 不同策略。"""
    return (f"{policy.version}/{risk.version}/{SIZING_VERSION}/{pricing_engine.PRICING_VERSION}/"
            f"{kelly.KELLY_MATH_VERSION}/{scope.source}:{scope.bookmaker}")


def parse_scope(text: str) -> StrategyScope:
    """'twsport' / 'oddsapi:pinnacle' → StrategyScope。"""
    src, _, book = text.partition(":")
    if src == "twsport":
        return StrategyScope("twsport", book or "twsport")
    if not book:
        raise ValueError("oddsapi 診斷必須指定單一 bookmaker，例如 oddsapi:pinnacle")
    return StrategyScope(src, book)
