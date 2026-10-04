"""
Phase D.2 — market pricing engine（去水 / 模型機率 / edge / EV 的唯一實作）
------------------------------------------------------------
    price_market(odds, prediction, model, as_of=...) -> MarketPricing

輸入：
    odds        OddsInput：一個 odds_snapshots 列（= 某一刻、某 bookmaker、某市場的完整報價，D.1 canonical）
    prediction  PredictionInput 或 None：analysis_as_of 時點以前最新且有效的一筆 predictions（見 alignment.py）
    model       ModelProbabilities：模型機率來源。production = RowModelProbabilities，
                **一律**透過 C.5E core/production/probability.line_probability_from_prediction_row()
                （用該 prediction 列記錄的 artifact_version；不在這裡另外算 normal CDF）

凍結定義（API 欄位同名；不得混用）：
    decimal_odds        bookmaker 實際提供的十進位賠率
    raw_implied_prob    1 / decimal_odds（含水）
    market_overround    Σ raw_implied_prob − 1（同一 snapshot 的全部 outcome）
    fair_no_vig_prob    raw_implied_prob / Σ raw_implied_prob（no_vig_method = proportional-v1）
    model_prob          C.5E 分佈對「同一 outcome / 同一條線」嚴格勝出的機率 P(win)
    push_prob           P(結果 = 線)，退還本金（只有整數讓分 / 大小分線）；其他市場 = 0
    loss_prob           1 − model_prob − push_prob
    edge_vs_fair        model_prob − fair_no_vig_prob（= 規格中的 model_edge；唯一叫 edge 的量）
    ev_per_unit         model_prob × (decimal_odds − 1) − loss_prob
                        （≡ model_prob × decimal_odds + push_prob − 1；用實際賠率，不是去水後的公允賠率）
    expected_return     1 + ev_per_unit
    ev_percent          100 × ev_per_unit
不叫 edge 的東西：盤口線與點預測的差（line gap）、model_prob − raw_implied_prob、EV。

市場 → 模型機率（comparator 由 D.1 canonical 決定，這裡不再做任何正負號運算）：
    gt（主隊 / 大）  P(win) = P(target > threshold)、P(push) = P(target = threshold)、P(loss) = P(target < threshold)
    lt（客隊 / 小）  P(win) = P(target < threshold)、push 同上、P(loss) = P(target > threshold)
    eq（三向的和）   P(win) = P(target = 0)、push = 0（和局是可下注的 outcome，不是 push）、P(loss) = 其餘
    三向的主 / 客     P(target = 0) 歸入 P(loss)（和局開出 = 主 / 客都輸，不退款），push = 0

結算規則（settlement_rule）：
    全場獨贏兩向        moneyline_ot_included：NBA 有延長賽，分差不可能 0 → 沒有 push（模型 push 必須為 0）
    全場獨贏三向        unsupported_market：常規時間 1X2，模型分差含延長賽，target 不同
    上半場獨贏三向      three_way_draw_outcome：主 / 和 / 客三邊一起去水；和局是 outcome
    上半場獨贏兩向      unsupported_settlement：平手的結算（push / 輸 / 和局退款…）依 bookmaker 而定，
                        沒有可靠 metadata → 只算 raw / 去水，不算 model_prob / edge / EV
    讓分 / 大小 半分線   half_line_no_push
    讓分 / 大小 整數線   integer_line_push_refund：push 退還本金（EV 公式含 push_prob，不當輸、不做條件化）
    四分之一線          unsupported_settlement：亞洲盤半注拆分結算，本階段不支援

只對「完整、open」的單一 snapshot 計算（缺邊 / suspended / closed / 非法價格 → rejected）；
不同時間的 snapshot 永遠不會被拼成同一個市場（一個 OddsInput = 一列）。
各 bookmaker、各條線各自獨立定價：不平均、不 consensus、不挑最佳盤、不推薦、不算下注金額。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..odds.canonical import DRAW, FULL_GAME, MONEYLINE, OPEN, THREE_WAY, MarketSnapshot
from ..production import spec
from ..timeutil import ensure_utc, parse_utc
from . import novig

PRICING_VERSION = "pricing-v1"
NO_VIG_METHOD = novig.NO_VIG_METHOD

PRICED = "priced"
MARKET_ONLY = "market_only"                       # 沒有有效預測：只有 raw / 去水
UNSUPPORTED_SETTLEMENT = "unsupported_settlement"
UNSUPPORTED_MARKET = "unsupported_market"
REJECTED = "rejected"
STATUSES = (PRICED, MARKET_ONLY, UNSUPPORTED_SETTLEMENT, UNSUPPORTED_MARKET, REJECTED)

ML_CONSISTENCY_WARN = 0.05                        # C.5E §9b：分差導出 vs 邏輯迴歸勝率差 > 5 pp → 標示檢查
PROB_SUM_TOL = 1e-9


class FutureDataError(ValueError):
    """輸入晚於 analysis_as_of（future leakage）。"""


class ModelProbabilityError(RuntimeError):
    """模型機率無法取得（artifact 載入失敗等）；呼叫端不得把這種市場當成 market_only 永久保存。"""


# ------------------------------------------------------------------ #
# 輸入                                                                  #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class OddsInput:
    snapshot_id: int | None
    game_id: int
    fetched_at: datetime
    snapshot: MarketSnapshot | None               # None = 列本身不合法（invalid_reason）
    last_seen_at: datetime | None = None
    invalid_reason: str | None = None
    source: str | None = None                     # snapshot 為 None 時仍需要的身分欄位
    bookmaker: str | None = None
    market: str | None = None


@dataclass(frozen=True)
class PredictionInput:
    prediction_id: int | None
    game_id: int
    model_version: str
    created_at: datetime                          # DB 寫入時間（實際可得時間）
    prediction_as_of: datetime | None             # features_json.prediction_as_of_utc（輸入資料的時點）
    artifact_version: str | None
    profile: str | None
    prediction_kind: str | None
    home_win_prob: float | None                   # 邏輯迴歸勝率（只作一致性監控）
    row: dict = field(compare=False, hash=False, repr=False, default_factory=dict)

    @property
    def available_at(self) -> datetime:
        """這筆預測最早可被使用的時間 = max(created_at, prediction_as_of)。"""
        if self.prediction_as_of is None:
            return self.created_at
        return max(self.created_at, self.prediction_as_of)


def _features(row: dict) -> dict:
    fj = row.get("features_json")
    if isinstance(fj, str):
        try:
            fj = json.loads(fj)
        except ValueError:
            fj = None
    return fj if isinstance(fj, dict) else {}


def _finite(x: Any) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def prediction_invalid_reason(row: dict) -> str | None:
    """None = 可用於定價（C.5E 介面需要的欄位齊全）。"""
    if row.get("model_version") != spec.MODEL_VERSION:
        return "model_version_not_priceable"
    fj = _features(row)
    if not fj.get("artifact_version"):
        return "missing_artifact_version"
    if fj.get("profile") not in spec.PROFILES:
        return "missing_profile"
    for k in ("pred_margin", "pred_total", "pred_home_h1", "pred_away_h1"):
        if not _finite(row.get(k)):
            return f"missing_{k}"
    return None


def prediction_from_row(row: dict) -> PredictionInput:
    fj = _features(row)
    r = dict(row)
    r["features_json"] = fj
    hw = row.get("home_win_prob")
    return PredictionInput(
        prediction_id=row.get("id"), game_id=int(row["game_id"]), model_version=row.get("model_version"),
        created_at=ensure_utc(parse_utc(row["created_at"])),
        prediction_as_of=parse_utc(fj.get("prediction_as_of_utc")),
        artifact_version=fj.get("artifact_version"), profile=fj.get("profile"),
        prediction_kind=fj.get("prediction_kind"),
        home_win_prob=float(hw) if _finite(hw) else None, row=r)


# ------------------------------------------------------------------ #
# 模型機率來源                                                           #
# ------------------------------------------------------------------ #

class ModelProbabilities(Protocol):
    def line(self, target: str, threshold: float) -> dict[str, Any]:
        """回傳 C.5E predict_line_probability 的輸出（probability_above / _push / _below、版本欄位…）。"""


class RowModelProbabilities:
    """production：stored prediction 列 + 該列記錄的 artifact 版本 → C.5E line_probability_from_prediction_row。"""

    def __init__(self, prediction: PredictionInput, art=None, *, root=None):
        from ..production import artifact as art_mod

        self.prediction = prediction
        try:
            self.art = art if art is not None else art_mod.load_version(prediction.artifact_version, root)
        except Exception as e:  # noqa: BLE001
            raise ModelProbabilityError(f"artifact {prediction.artifact_version} 無法載入：{e}") from e
        self.root = root
        self._cache: dict[tuple[str, float], dict[str, Any]] = {}

    def line(self, target: str, threshold: float) -> dict[str, Any]:
        from ..production import probability

        key = (target, float(threshold))
        if key not in self._cache:
            try:
                self._cache[key] = probability.line_probability_from_prediction_row(
                    self.prediction.row, target, float(threshold), root=self.root, art=self.art)
            except Exception as e:  # noqa: BLE001
                raise ModelProbabilityError(f"{target} @ {threshold}：{e}") from e
        return self._cache[key]


class ArtifactCache:
    """同一次工作內每個 artifact 版本只載入一次。"""

    def __init__(self, root=None):
        self.root = root
        self._arts: dict[str, Any] = {}

    def provider(self, prediction: PredictionInput) -> RowModelProbabilities:
        v = prediction.artifact_version
        if v not in self._arts:
            self._arts[v] = RowModelProbabilities(prediction, root=self.root).art
        return RowModelProbabilities(prediction, self._arts[v], root=self.root)


# ------------------------------------------------------------------ #
# 輸出                                                                  #
# ------------------------------------------------------------------ #

@dataclass
class OutcomePricing:
    side: str
    display_line: float | None
    model_target: str
    model_threshold: float
    comparator: str
    decimal_odds: float | None
    raw_implied_prob: float | None = None
    fair_no_vig_prob: float | None = None
    model_prob: float | None = None
    push_prob: float | None = None
    loss_prob: float | None = None
    edge_vs_fair: float | None = None
    ev_per_unit: float | None = None
    expected_return: float | None = None
    ev_percent: float | None = None


@dataclass
class MarketPricing:
    pricing_version: str
    no_vig_method: str
    status: str
    status_reason: str | None
    settlement_rule: str | None
    odds_snapshot_id: int | None
    game_id: int
    source: str | None
    bookmaker: str | None
    market: str | None
    market_type: str | None
    period: str | None
    outcome_set: str | None
    line: float | None
    away_line: float | None
    odds_fetched_at: datetime
    odds_last_seen_at: datetime | None
    analysis_as_of: datetime                      # = max(odds_fetched_at, prediction.available_at)：此分析最早可成立的時間
    prediction_id: int | None = None
    prediction_created_at: datetime | None = None
    prediction_kind: str | None = None
    prediction_profile: str | None = None
    model_version: str | None = None
    artifact_version: str | None = None
    distribution_version: str | None = None
    total_raw_implied: float | None = None
    market_overround: float | None = None
    fair_prob_sum: float | None = None
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    outcomes: list[OutcomePricing] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("odds_fetched_at", "odds_last_seen_at", "analysis_as_of", "prediction_created_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    def to_rows(self) -> list[dict[str, Any]]:
        """market_pricing_snapshots 的列（每個 outcome 一列；市場層欄位重複保存）。"""
        base = {k: getattr(self, k) for k in (
            "pricing_version", "no_vig_method", "status", "status_reason", "settlement_rule", "odds_snapshot_id",
            "game_id", "source", "bookmaker", "market", "market_type", "period", "outcome_set", "line", "away_line",
            "odds_fetched_at", "analysis_as_of", "prediction_id", "prediction_created_at", "prediction_kind",
            "prediction_profile", "model_version", "artifact_version", "distribution_version", "total_raw_implied",
            "market_overround", "fair_prob_sum")}
        base["warnings"] = list(self.warnings)
        base["diagnostics"] = self.diagnostics
        return [{**base, **asdict(o)} for o in self.outcomes]


# ------------------------------------------------------------------ #
# 結算 / 機率 / EV（純算術）                                              #
# ------------------------------------------------------------------ #

def _frac(x: float) -> float:
    return abs(float(x)) % 1.0


def settlement_rule(snap: MarketSnapshot) -> tuple[str, str | None]:
    """→ (rule, 不支援時的狀態)。狀態 None = 可完整定價。"""
    if snap.market_type == MONEYLINE:
        if snap.period == FULL_GAME:
            if snap.outcome_set == THREE_WAY:
                return "full_game_three_way_regulation_not_modelled", UNSUPPORTED_MARKET
            return "moneyline_ot_included", None
        if snap.outcome_set == THREE_WAY:
            return "three_way_draw_outcome", None
        return "h1_two_way_tie_settlement_unknown", UNSUPPORTED_SETTLEMENT
    f = _frac(snap.model_threshold)
    if math.isclose(f, 0.5):
        return "half_line_no_push", None
    if math.isclose(f, 0.0) or math.isclose(f, 1.0):
        return "integer_line_push_refund", None
    return "quarter_line_split_settlement", UNSUPPORTED_SETTLEMENT


def win_push_loss(comparator: str, lp: dict[str, Any]) -> tuple[float, float, float]:
    """C.5E 線機率 → 該 outcome 的 (P(win), P(push), P(loss))。"""
    a, p, b = float(lp["probability_above"]), float(lp["probability_push"]), float(lp["probability_below"])
    if comparator == "gt":
        return a, p, b
    if comparator == "lt":
        return b, p, a
    if comparator == "eq":                      # 三向的和局：P(=threshold) 是「贏」，沒有 push
        return p, 0.0, a + b
    raise ValueError(f"未知 comparator {comparator}")


def expected_value(p_win: float, p_push: float, p_loss: float, decimal_odds: float) -> float:
    """每投注 1 單位的 EV（push 退還本金）：P(win)·(odds − 1) − P(loss)。odds 必須是實際提供的賠率。"""
    if abs(p_win + p_push + p_loss - 1.0) > PROB_SUM_TOL:
        raise ValueError(f"機率總和 {p_win + p_push + p_loss!r} ≠ 1")
    return p_win * (float(decimal_odds) - 1.0) - p_loss


def edge_vs_fair(model_prob: float, fair_no_vig_prob: float) -> float:
    return model_prob - fair_no_vig_prob


def _expected_sides(snap: MarketSnapshot) -> int:
    return 3 if snap.outcome_set == THREE_WAY else 2


# ------------------------------------------------------------------ #
# price_market                                                         #
# ------------------------------------------------------------------ #

def price_market(odds: OddsInput, prediction: PredictionInput | None, model: ModelProbabilities | None, *,
                 as_of: datetime) -> MarketPricing:
    as_of = ensure_utc(as_of)
    fetched = ensure_utc(odds.fetched_at)
    if fetched > as_of:
        raise FutureDataError(f"odds snapshot fetched_at {fetched.isoformat()} > analysis_as_of {as_of.isoformat()}")
    if prediction is not None:
        if prediction.available_at > as_of:
            raise FutureDataError(f"prediction available_at {prediction.available_at.isoformat()} > "
                                  f"analysis_as_of {as_of.isoformat()}")
        if prediction.game_id != odds.game_id:
            raise ValueError(f"prediction game {prediction.game_id} ≠ odds game {odds.game_id}")
        if model is None:
            raise ValueError("有 prediction 時必須提供 model 機率來源")

    snap = odds.snapshot
    effective = fetched if prediction is None else max(fetched, prediction.available_at)
    mp = MarketPricing(
        pricing_version=PRICING_VERSION, no_vig_method=NO_VIG_METHOD, status=REJECTED, status_reason=None,
        settlement_rule=None, odds_snapshot_id=odds.snapshot_id, game_id=odds.game_id,
        source=snap.source if snap else odds.source, bookmaker=snap.bookmaker if snap else odds.bookmaker,
        market=snap.market if snap else odds.market, market_type=snap.market_type if snap else None,
        period=snap.period if snap else None, outcome_set=snap.outcome_set if snap else None,
        line=snap.line if snap else None, away_line=snap.away_line if snap else None,
        odds_fetched_at=fetched, odds_last_seen_at=ensure_utc(odds.last_seen_at) if odds.last_seen_at else None,
        analysis_as_of=effective)
    if prediction is not None:
        mp.prediction_id = prediction.prediction_id
        mp.prediction_created_at = prediction.created_at
        mp.prediction_kind = prediction.prediction_kind
        mp.prediction_profile = prediction.profile
        mp.model_version = prediction.model_version
        mp.artifact_version = prediction.artifact_version

    if snap is None:
        mp.status_reason = odds.invalid_reason or "invalid_snapshot"
        return mp

    quotes = snap.quotes()
    mp.outcomes = [OutcomePricing(q.side, q.display_line, q.model_target, q.model_threshold, q.comparator, q.price)
                   for q in quotes]

    # ---- 完整性：同一 snapshot、open、每一邊都有合法價格 ---- #
    if snap.status != OPEN:
        mp.status_reason = f"market_not_open:{snap.status}"
        return mp
    if len(quotes) != _expected_sides(snap) or (snap.outcome_set == THREE_WAY) != any(q.side == DRAW for q in quotes):
        mp.status_reason = "outcome_set_incomplete"
        return mp
    for o in mp.outcomes:
        if o.decimal_odds is not None:
            try:
                o.raw_implied_prob = novig.raw_implied_prob(o.decimal_odds)
            except novig.NoVigError:
                o.decimal_odds = None
    if any(o.decimal_odds is None for o in mp.outcomes):
        mp.status_reason = "incomplete_market"
        return mp

    # ---- 去水（全部 outcome 一起） ---- #
    try:
        nv = novig.proportional_no_vig([o.decimal_odds for o in mp.outcomes])
    except novig.NoVigError as e:
        mp.status_reason = e.reason
        return mp
    mp.total_raw_implied, mp.market_overround, mp.fair_prob_sum = nv.total_raw_implied, nv.overround, nv.fair_prob_sum
    mp.warnings.extend(nv.warnings)
    for o, q, f in zip(mp.outcomes, nv.raw_implied, nv.fair):
        o.raw_implied_prob, o.fair_no_vig_prob = q, f

    rule, unsupported = settlement_rule(snap)
    mp.settlement_rule = rule
    if unsupported is not None:
        mp.status, mp.status_reason = unsupported, rule
        return mp
    if prediction is None:
        mp.status, mp.status_reason = MARKET_ONLY, "no_valid_prediction_at_as_of"
        return mp

    # ---- 模型機率（C.5E）→ edge / EV ---- #
    lps: dict[float, dict[str, Any]] = {}
    for o in mp.outcomes:
        lp = lps.get(o.model_threshold)
        if lp is None:
            lp = lps[o.model_threshold] = model.line(o.model_target, o.model_threshold)
        if lp.get("artifact_version") != prediction.artifact_version:
            raise ModelProbabilityError(f"機率來源 artifact {lp.get('artifact_version')} ≠ prediction "
                                        f"{prediction.artifact_version}")
        p_win, p_push, p_loss = win_push_loss(o.comparator, lp)
        if rule == "three_way_draw_outcome" and o.comparator != "eq":
            p_push, p_loss = 0.0, p_loss + p_push      # 三向：結果 = 0 是「和局」outcome 中獎，主 / 客都輸（不是 push）
        if rule in ("moneyline_ot_included", "half_line_no_push", "three_way_draw_outcome") and p_push != 0.0:
            raise ModelProbabilityError(f"{rule} 的模型 push 機率必須為 0（得到 {p_push!r}）")
        o.model_prob, o.push_prob, o.loss_prob = p_win, p_push, p_loss
        o.edge_vs_fair = edge_vs_fair(p_win, o.fair_no_vig_prob)
        o.ev_per_unit = expected_value(p_win, p_push, p_loss, o.decimal_odds)
        o.expected_return = 1.0 + o.ev_per_unit
        o.ev_percent = 100.0 * o.ev_per_unit
        mp.distribution_version = lp.get("distribution_version")
        mp.diagnostics.setdefault("point_prediction", lp.get("point_prediction"))
        mp.diagnostics.setdefault("distribution", {"name": lp.get("distribution"), "center": lp.get("center"),
                                                   "scale": lp.get("scale")})
        flags = (lp.get("data_quality") or {}).get("flags")
        if flags:
            mp.diagnostics["data_quality_flags"] = list(flags)

    if snap.market_type == MONEYLINE and snap.period == FULL_GAME:
        home = next(o for o in mp.outcomes if o.side == "home")
        mp.diagnostics["ml_consistency"] = c = {
            "margin_derived_home_win_prob": home.model_prob, "logistic_home_win_prob": prediction.home_win_prob,
            "abs_diff": None, "threshold": ML_CONSISTENCY_WARN, "warning": False}
        if prediction.home_win_prob is not None:
            c["abs_diff"] = abs(home.model_prob - prediction.home_win_prob)
            if c["abs_diff"] > ML_CONSISTENCY_WARN:
                c["warning"] = True
                mp.warnings.append("ml_logistic_margin_divergence")
    mp.status = PRICED
    return mp

