"""
decision-v1（凍結；在任何投注 ROI 之前定義）
------------------------------------------------------------
D.5 只把「使用者真的已下注的 bets」納入**同一個** risk-v1 預算，不改 risk-v1 任何參數、不建立 risk-v1.1：

    risk-v1      ¼ Kelly → 單筆 ≤ 2% → 同場 ≤ 3% → 單日（Asia/Taipei betting day）≤ 8%       （core/sizing/policy.py）
    execution-v1 betting day = 開賽的 Asia/Taipei 日期；day_start_bankroll 當日凍結                 （core/execution/policy.py）
    decision-v1  （本檔）
      scope                 只有台彩（twsport:twsport）產生可操作額度；Odds API 只作 international diagnostic
                            （不 best-book / consensus / 平均 / 以國際盤替代沒有的台彩盤口）
      candidates            台彩-only 的 D.3 size_portfolio（as_of = 物化時點）中 eligible / exposure_scaled 的 outcome，
                            從 single_bet_capped_fraction 開始（不是 D.3 final：那是「沒有任何實際下注」的理論組合）
      actual exposure       有效（active、未 void / superseded）的實際下注 stake ÷ 當日 day_start_bankroll；
                            已結算的當日注單仍占用當日額度（與 execution-v1 一致）
      rescale               remaining_game = max(0, 3% − actual_game)、remaining_day = max(0, 8% − actual_day)；
                            同場 / 單日超出 → 等比例縮放（不依 EV 排名、不挑最佳）
      already recorded      同一 game × market 已有有效實際下注 → 新增額度 0（不 top-up；看到更好的賠率也不補單）
      no minimum EV         EV > 0 且通過 D.3 檢查即顯示；不設 3% / 5% / 10% 門檻
      evidence              尚無歷史投注證據；prospective paper 的結果不會改變任何選擇或額度

要改任何規則 → 新版本字串（decision-v2），不得冒充 decision-v1。
"""
from __future__ import annotations

from datetime import timedelta

from ..execution.policy import EXECUTION_V1, INTERNATIONAL_LABEL, PRIMARY_SCOPE, TWSPORT_LABEL
from ..sizing.policy import PRODUCTION_RISK_POLICY

DECISION_VERSION = "decision-v1"
BANKROLL_VERSION = "bankroll-v1"

RISK_POLICY = PRODUCTION_RISK_POLICY            # risk-v1（不修改）
EXECUTION_POLICY = EXECUTION_V1                 # execution-v1（betting day / day-start 語意）
PRIMARY = PRIMARY_SCOPE                         # twsport:twsport
TOP_UP_ALLOWED = False                          # decision-v1：已記錄的機會不補單
MIN_EV_THRESHOLD = None                         # 不設最低 EV 門檻（EV > 0 即可）
EXPOSURE_EPS = 1e-12                            # 浮點誤差：剩餘額度 < EPS 視為 0；actual > cap + 1e-9 視為超限
OVER_LIMIT_TOL = 1e-9
CURRENCY_STEP = 1.0                             # suggested_stake_amount 向下取整的單位（≤ max_additional）
MATERIALIZATION_STALE_AFTER = timedelta(minutes=10)

# ---- decision_status ---- #
QUALIFIED = "qualified"
ALREADY_RECORDED = "already_recorded"
NO_POSITIVE_EV = "no_positive_ev"
STALE_ODDS = "stale_odds"
NO_PREDICTION = "no_prediction"
NO_ODDS = "no_odds"
UNSUPPORTED = "unsupported"
RISK_CAP_REACHED = "risk_cap_reached"
ACTUAL_EXPOSURE_OVER_LIMIT = "actual_exposure_over_limit"
BANKROLL_UNAVAILABLE = "bankroll_unavailable"
DATA_INCOMPLETE = "data_incomplete"
DECISION_STATUSES = (QUALIFIED, ALREADY_RECORDED, NO_POSITIVE_EV, STALE_ODDS, NO_PREDICTION, NO_ODDS, UNSUPPORTED,
                     RISK_CAP_REACHED, ACTUAL_EXPOSURE_OVER_LIMIT, BANKROLL_UNAVAILABLE, DATA_INCOMPLETE)

# ---- status_group（UI 層級；文字 + icon 也會顯示，不只靠顏色） ---- #
ACTIONABLE = "actionable"        # Qualified opportunity
REVIEW = "review"                # Qualified，但有資料品質警示（仍在前瞻驗證期）
RECORDED = "recorded"            # Actual bet logged
BLOCKED = "blocked"              # 無台彩盤口 / 過舊 / 無預測 / bankroll / 額度用完 …
INACTIVE = "inactive"            # 無正 EV / 不支援的玩法
DIAGNOSTIC = "diagnostic"        # 國際盤（不是台彩證據、沒有額度）

# ---- actual bet ---- #
ORIGINS = ("platform_opportunity", "paper_decision", "manual_unlinked")
COMPLIANCE = ("compliant", "manual_unlinked", "user_override", "outside_model", "missing_context")
ACTIVE = "active"

# ---- 台彩盤口狀態（為什麼沒有 actionable） ---- #
TW_AVAILABLE = "available"
TW_STALE = "stale"
TW_MARKET_CLOSED = "market_closed"
TW_NOT_PUBLISHED = "not_yet_published"
TW_INGESTION_UNAVAILABLE = "ingestion_unavailable"     # 自動擷取被擋 / 失敗 → 需要 HAR 匯入
TW_NOT_CONFIGURED = "not_configured"
TW_OUTSIDE_WINDOW = "outside_window"
TW_GAME_STARTED = "game_started"

TAIWAN_LABEL = TWSPORT_LABEL
INTERNATIONAL = INTERNATIONAL_LABEL
EVIDENCE_BADGE = "prospective_validation"       # decision-v1 一律顯示「尚在前瞻驗證」（不會自動升級）


def to_dict() -> dict:
    return {"decision_version": DECISION_VERSION, "bankroll_version": BANKROLL_VERSION,
            "risk_policy": RISK_POLICY.to_dict(), "execution_policy_version": EXECUTION_POLICY.version,
            "scope": f"{PRIMARY.source}:{PRIMARY.bookmaker}", "top_up_allowed": TOP_UP_ALLOWED,
            "min_ev_threshold": MIN_EV_THRESHOLD}
