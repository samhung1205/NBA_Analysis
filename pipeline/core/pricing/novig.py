"""
去水（no-vig）——Phase D.2 唯一實作
------------------------------------------------------------
凍結定義（全專案只在這裡計算，Node API / 前端只讀結果）：

    raw_implied_prob   q_i = 1 / decimal_odds_i
    total_raw_implied  Σq
    market_overround   Σq − 1
    fair_no_vig_prob   p_i = q_i / Σq                    （proportional-v1：乘法 / 比例正規化）

兩向市場兩邊一起去水；三向市場（台彩上半場獨贏：主 / 和 / 客）三邊一起去水——**不**拆成兩組兩向。
只接受「同一個 snapshot、完整、open」的市場（由 engine 保證）；這裡只做算術與合理性檢查。

proportional-v1 是 production 唯一方法（凍結）；其他方法（Shin / power …）若日後實作只能作研究工具，
不得以 ROI 或真實盤口結果挑選、不得成為預設。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

NO_VIG_METHOD = "proportional-v1"

# 合理性界線（寬鬆；依實際盤口設定，不針對個別高水市場硬編規則）
#   實測（2026-10-04 fixture）：台彩兩向 16.3–17.3%、三向（上半場獨贏）27.7%；美國 bookmaker 兩向 4.3–5.3%。
#   overround < 0（同一 bookmaker 同一時刻的套利）或非有限值 → 資料錯誤，拒絕計算。
#   > REJECT → 幾乎必然是解析錯誤（例如把別的盤口的價格配在一起），拒絕計算。
#   > WARN / < VERY_LOW → 照算，但標 data-quality warning。
OVERROUND_WARN_HIGH = {2: 0.25, 3: 0.40}
OVERROUND_REJECT_HIGH = {2: 0.60, 3: 0.90}
OVERROUND_WARN_VERY_LOW = 0.005
FAIR_SUM_TOL = 1e-9


class NoVigError(ValueError):
    """無法去水（reason 是穩定代碼）。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class NoVigResult:
    raw_implied: tuple[float, ...]
    fair: tuple[float, ...]
    total_raw_implied: float
    overround: float
    fair_prob_sum: float
    warnings: tuple[str, ...]
    method: str = NO_VIG_METHOD


def raw_implied_prob(decimal_odds: float) -> float:
    d = float(decimal_odds)
    if not math.isfinite(d) or d <= 1.0:
        raise NoVigError("invalid_price", repr(decimal_odds))
    return 1.0 / d


def proportional_no_vig(decimal_odds: list[float] | tuple[float, ...]) -> NoVigResult:
    """同一市場的全部 outcome 一起去水（2 或 3 個）。"""
    n = len(decimal_odds)
    if n not in (2, 3):
        raise NoVigError("unsupported_outcome_count", str(n))
    q = tuple(raw_implied_prob(d) for d in decimal_odds)
    total = math.fsum(q)
    over = total - 1.0
    if not math.isfinite(over):
        raise NoVigError("overround_non_finite", repr(over))
    if over < 0:
        raise NoVigError("negative_overround", f"{over:.6f}")
    if over > OVERROUND_REJECT_HIGH[n]:
        raise NoVigError("implausible_overround", f"{over:.6f}")
    warnings = []
    if over > OVERROUND_WARN_HIGH[n]:
        warnings.append("overround_high")
    if over < OVERROUND_WARN_VERY_LOW:
        warnings.append("overround_very_low")
    fair = tuple(x / total for x in q)
    s = math.fsum(fair)
    if abs(s - 1.0) > FAIR_SUM_TOL:
        raise NoVigError("fair_sum_not_one", f"{s!r}")
    return NoVigResult(q, fair, total, over, s, tuple(warnings))
