"""
Kelly 數學（Phase D.3 凍結）——單一 outcome、可含 push（退還本金）
------------------------------------------------------------
輸入只有：P(win)、P(push)、P(loss)、實際提供的十進位賠率。**不**使用 edge_vs_fair / fair_no_vig_prob。

    b = decimal_odds − 1                                     （淨賠率）
    EV = P(win)·b − P(loss)                                  （= D.2 ev_per_unit；push 的報酬為 0）

下注 bankroll 比例 f 後的期望對數成長：

    G(f) = P(win)·log(1 + b·f) + P(loss)·log(1 − f) + P(push)·log(1)

    G'(f) = P(win)·b / (1 + b·f) − P(loss) / (1 − f) = 0
      ⇒ P(win)·b·(1 − f) = P(loss)·(1 + b·f)
      ⇒ f* = (P(win)·b − P(loss)) / (b·(P(win) + P(loss)))  = EV / (b·(1 − P(push)))

    G''(f) = −P(win)·b² / (1 + b·f)² − P(loss) / (1 − f)² < 0 → 唯一最大值（凹函數）。
    P(push) = 0 時退化為標準 Kelly：f* = (P(win)·b − P(loss)) / b。
    G'(0) = EV → EV ≤ 0 時在 [0, 1) 上 G 遞減，最佳 f = 0（不放空、不反向下注）。
    P(loss) = 0 且 P(win) > 0 → G 單調遞增，f* = 1（公式同樣給 1）；之後由 fractional Kelly 與上限處理。
    P(loss) > 0 → f* < P(win)·b / (b·(P(win)+P(loss))) ≤ 1。

三向市場（台彩上半場主 / 和 / 客）的一個 outcome：P(win) = 該 outcome、P(loss) = 其他兩個 outcome 合計、P(push) = 0
——就是一般單一 outcome Kelly；不拆成兩向、不對多個互斥 outcome 同時下注（見 engine 的互斥檢查）。
"""
from __future__ import annotations

import math

KELLY_MATH_VERSION = "kelly-push-v1"
PROB_SUM_TOL = 1e-9
PROB_RANGE_TOL = 1e-12


class KellyInputError(ValueError):
    """機率 / 賠率不合法（reason 是穩定代碼）。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _finite(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError) as e:
        raise KellyInputError("non_numeric", repr(x)) from e
    if not math.isfinite(v):
        raise KellyInputError("non_finite", repr(x))
    return v


def validate(p_win, p_push, p_loss, decimal_odds) -> tuple[float, float, float, float]:
    """→ (p_win, p_push, p_loss, b)；不合法 → KellyInputError。"""
    d = _finite(decimal_odds)
    if d <= 1.0:
        raise KellyInputError("invalid_decimal_odds", repr(decimal_odds))
    ps = tuple(_finite(p) for p in (p_win, p_push, p_loss))
    if any(p < -PROB_RANGE_TOL or p > 1.0 + PROB_RANGE_TOL for p in ps):
        raise KellyInputError("probability_out_of_range", repr(ps))
    if abs(math.fsum(ps) - 1.0) > PROB_SUM_TOL:
        raise KellyInputError("probabilities_do_not_sum_to_one", repr(math.fsum(ps)))
    pw, pp, pl = (min(max(p, 0.0), 1.0) for p in ps)
    return pw, pp, pl, d - 1.0


def expected_value(p_win, p_push, p_loss, decimal_odds) -> float:
    """每投注 1 單位的 EV（push 退本金）= P(win)·b − P(loss)。與 D.2 engine.expected_value 同一定義。"""
    pw, _, pl, b = validate(p_win, p_push, p_loss, decimal_odds)
    return pw * b - pl


def full_kelly_fraction(p_win, p_push, p_loss, decimal_odds) -> float:
    """push-aware full Kelly（bankroll 比例）。EV ≤ 0 → 0。"""
    pw, _, pl, b = validate(p_win, p_push, p_loss, decimal_odds)
    numerator = pw * b - pl                      # = EV
    if numerator <= 0.0:
        return 0.0
    f = numerator / (b * (pw + pl))              # numerator > 0 ⇒ pw > 0 ⇒ 分母 > 0
    return min(f, 1.0)                           # 數學上 ≤ 1；只防浮點誤差


def expected_log_growth(f: float, p_win, p_push, p_loss, decimal_odds) -> float:
    """G(f)（測試用：數值最大化需與解析解一致）。"""
    pw, _, pl, b = validate(p_win, p_push, p_loss, decimal_odds)
    if not 0.0 <= f < 1.0 and not (f == 1.0 and pl == 0.0):
        raise ValueError("f 必須在 [0, 1)")
    g = pw * math.log1p(b * f) if pw > 0 else 0.0
    if pl > 0:
        g += pl * math.log1p(-f)
    return g
