"""
Canonical market model（Phase D.1）
------------------------------------------------------------
所有來源（台灣運彩 / The Odds API）都先轉成 MarketSnapshot：某一刻、某家 bookmaker、
某一場比賽、某一個市場的「完整報價」（兩向或三向）。下游（DB / D.2 定價）只看這個型別。

市場身分 = (market_type, period)，不靠字串猜：
    market_type ∈ {moneyline, spread, total}
    period      ∈ {full_game, h1}
legacy_market()（'ml' / 'spread' / 'total' / 'h1_ml' / 'h1_spread' / 'h1_total'）只是
odds_snapshots.market 的既有顯示代碼（前端 / bets 沿用），由這裡單向導出。

**正負號慣例（全專案唯一定義處）**

    模型：margin = home_score − away_score（C.5E 介面回答 P(target > threshold)）

    讓分（spread）—— bookmaker 顯示線是「該隊得分 + 顯示線」後比大小：
        主隊顯示線 h（例 −5.5）：主隊過盤 ⇔ margin + h > 0 ⇔ margin > −h      → threshold = −h = +5.5，comparator gt
        客隊顯示線 a（例 +5.5）：客隊過盤 ⇔ −margin + a > 0 ⇔ margin < a       → threshold =  a = +5.5，comparator lt
        兩向報價必須對稱（a = −h），否則整個 market 拒收（不猜）。
    大小分（total）：大 ⇔ total > line（gt）；小 ⇔ total < line（lt）；threshold = line
    獨贏（moneyline）：主 ⇔ margin > 0；客 ⇔ margin < 0；和（只有三向的上半場）⇔ margin = 0；threshold = 0

    整數線的 push（= threshold）不屬於任一邊，由 C.5E 的 probability_push 表達；這裡不處理結算規則。

賠率一律 decimal（> 1）。原始格式（台彩分數 up/down、美式）保留在 raw，另存轉換後的 decimal。
這裡**不做** implied probability / 去水 / edge。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any

NORMALIZER_VERSION = "odds-norm-v1"

MONEYLINE, SPREAD, TOTAL = "moneyline", "spread", "total"
FULL_GAME, H1 = "full_game", "h1"
MARKET_TYPES = (MONEYLINE, SPREAD, TOTAL)
PERIODS = (FULL_GAME, H1)

TWO_WAY, THREE_WAY = "two_way", "three_way"
OPEN, SUSPENDED, CLOSED, UNKNOWN = "open", "suspended", "closed", "unknown"
STATUSES = (OPEN, SUSPENDED, CLOSED, UNKNOWN)

HOME, AWAY, DRAW, OVER, UNDER = "home", "away", "draw", "over", "under"

_LEGACY = {
    (MONEYLINE, FULL_GAME): "ml", (SPREAD, FULL_GAME): "spread", (TOTAL, FULL_GAME): "total",
    (MONEYLINE, H1): "h1_ml", (SPREAD, H1): "h1_spread", (TOTAL, H1): "h1_total",
}
_CANONICAL_KEY = {
    (MONEYLINE, FULL_GAME): "moneyline", (SPREAD, FULL_GAME): "spread", (TOTAL, FULL_GAME): "total",
    (MONEYLINE, H1): "h1_moneyline", (SPREAD, H1): "h1_spread", (TOTAL, H1): "h1_total",
}
_TARGET = {
    (MONEYLINE, FULL_GAME): "margin", (SPREAD, FULL_GAME): "margin", (TOTAL, FULL_GAME): "total",
    (MONEYLINE, H1): "h1_margin", (SPREAD, H1): "h1_margin", (TOTAL, H1): "h1_total",
}

# 合理範圍（超出 = 解析錯誤或非主盤，拒收）
SPREAD_MAX_ABS = {FULL_GAME: 40.0, H1: 25.0}
TOTAL_RANGE = {FULL_GAME: (150.0, 320.0), H1: (70.0, 170.0)}
DECIMAL_MIN, DECIMAL_MAX = 1.001, 1000.0


class InvalidMarket(ValueError):
    """market 不符合 canonical 規則（缺邊、線不合理、賠率非法…）。reason 是穩定代碼。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def legacy_market(market_type: str, period: str) -> str:
    return _LEGACY[(market_type, period)]


def canonical_market_key(market_type: str, period: str) -> str:
    return _CANONICAL_KEY[(market_type, period)]


def model_target(market_type: str, period: str) -> str:
    return _TARGET[(market_type, period)]


# ------------------------------------------------------------------ #
# 正負號轉換（唯一實作）                                                 #
# ------------------------------------------------------------------ #

def home_spread_threshold(display_home_line: float) -> float:
    """主隊顯示線 → 模型門檻：主隊過盤 ⇔ margin > threshold。例：−5.5 → +5.5。"""
    return 0.0 - float(display_home_line)


def away_spread_threshold(display_away_line: float) -> float:
    """客隊顯示線 → 模型門檻：客隊過盤 ⇔ margin < threshold。例：+5.5 → +5.5；−3.5 → −3.5。"""
    return float(display_away_line)


def away_line_from_home(display_home_line: float) -> float:
    """對稱讓分盤：客隊顯示線 = −主隊顯示線（只用於來源只給一條線時）。"""
    return 0.0 - float(display_home_line)


# ------------------------------------------------------------------ #
# 賠率                                                                  #
# ------------------------------------------------------------------ #

def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def decimal_from_fraction(up: Any, down: Any) -> float:
    """台彩 currentpriceup / currentpricedown（分數賠率）→ decimal = 1 + up/down。"""
    try:
        u, d = int(str(up).strip()), int(str(down).strip())
    except (TypeError, ValueError):
        raise InvalidMarket("price_unparseable", f"{up}/{down}") from None
    if d <= 0 or u <= 0:
        raise InvalidMarket("price_non_positive", f"{up}/{down}")
    return round(float(1 + Fraction(u, d)), 4)


def decimal_from_american(american: Any) -> float:
    a = _finite(american)
    if a is None or -100 < a < 100:
        raise InvalidMarket("price_unparseable", f"american {american}")
    return round(1 + (a / 100.0 if a > 0 else 100.0 / -a), 4)


def validate_decimal(price: Any) -> float:
    p = _finite(price)
    if p is None:
        raise InvalidMarket("price_nan", repr(price))
    if not (DECIMAL_MIN <= p <= DECIMAL_MAX):
        raise InvalidMarket("price_out_of_range", repr(price))
    return round(p, 4)


def _validate_line(market_type: str, period: str, line: Any) -> float:
    v = _finite(line)
    if v is None:
        raise InvalidMarket("line_nan", repr(line))
    if abs(v * 4 - round(v * 4)) > 1e-9:
        raise InvalidMarket("line_not_quarter_multiple", repr(line))
    if market_type == SPREAD and abs(v) > SPREAD_MAX_ABS[period]:
        raise InvalidMarket("line_out_of_range", f"spread {v}")
    if market_type == TOTAL:
        lo, hi = TOTAL_RANGE[period]
        if not lo <= v <= hi:
            raise InvalidMarket("line_out_of_range", f"total {v}")
    return v


# ------------------------------------------------------------------ #
# MarketSnapshot                                                       #
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class NormalizedQuote:
    """一個 outcome 的 canonical quote（MarketSnapshot.quotes() 產生；v_odds_quotes 是同一份資料的 SQL 版）。"""
    side: str
    price: float | None
    display_line: float | None       # bookmaker 顯示的線（讓分：該隊自己的線；大小：總分線；獨贏 None）
    model_target: str
    model_threshold: float
    comparator: str                  # gt / lt / eq：side 成立 ⇔ target <comparator> threshold


@dataclass(frozen=True)
class MarketSnapshot:
    source: str
    bookmaker: str
    source_event_id: str
    market_type: str
    period: str
    outcome_set: str
    status: str
    line: float | None               # spread：主隊顯示線；total：總分線；moneyline：None
    away_line: float | None          # spread：客隊顯示線（= −line，已驗證對稱）
    home_odds: float | None = None
    away_odds: float | None = None
    draw_odds: float | None = None
    over_odds: float | None = None
    under_odds: float | None = None
    source_market_id: str | None = None
    source_updated_at: datetime | None = None
    raw: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def market(self) -> str:
        return legacy_market(self.market_type, self.period)

    @property
    def canonical_key(self) -> str:
        return canonical_market_key(self.market_type, self.period)

    @property
    def model_target(self) -> str:
        return model_target(self.market_type, self.period)

    @property
    def model_threshold(self) -> float:
        if self.market_type == MONEYLINE:
            return 0.0
        if self.market_type == SPREAD:
            return home_spread_threshold(self.line)
        return float(self.line)

    def quotes(self) -> list[NormalizedQuote]:
        t, thr = self.model_target, self.model_threshold
        out: list[NormalizedQuote] = []
        if self.market_type == TOTAL:
            out.append(NormalizedQuote(OVER, self.over_odds, self.line, t, thr, "gt"))
            out.append(NormalizedQuote(UNDER, self.under_odds, self.line, t, thr, "lt"))
            return out
        home_line = self.line if self.market_type == SPREAD else None
        away_line = self.away_line if self.market_type == SPREAD else None
        away_thr = away_spread_threshold(away_line) if self.market_type == SPREAD else 0.0
        out.append(NormalizedQuote(HOME, self.home_odds, home_line, t, thr, "gt"))
        out.append(NormalizedQuote(AWAY, self.away_odds, away_line, t, away_thr, "lt"))
        if self.outcome_set == THREE_WAY:
            out.append(NormalizedQuote(DRAW, self.draw_odds, None, t, 0.0, "eq"))
        return out

    def content_key(self) -> dict[str, Any]:
        """去重身分（不含 fetched_at / source_updated_at / raw）：同一 series 內容相同 = 沒有實質變化。"""
        r = lambda x: None if x is None else round(float(x), 4)  # noqa: E731
        return {
            "market_type": self.market_type, "period": self.period, "outcome_set": self.outcome_set,
            "status": self.status, "line": r(self.line), "away_line": r(self.away_line),
            "home": r(self.home_odds), "away": r(self.away_odds), "draw": r(self.draw_odds),
            "over": r(self.over_odds), "under": r(self.under_odds),
        }


def content_hash(game_id: int, snap: MarketSnapshot) -> str:
    payload = {"game_id": int(game_id), "source": snap.source, "bookmaker": snap.bookmaker,
               "market": snap.market, **snap.content_key()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_snapshot(*, source: str, bookmaker: str, source_event_id: str, market_type: str, period: str,
                  status: str, prices: dict[str, Any], home_line: Any = None, away_line: Any = None,
                  total_line: Any = None, outcome_set: str = TWO_WAY, source_market_id: str | None = None,
                  source_updated_at: datetime | None = None, raw: dict | None = None) -> MarketSnapshot:
    """驗證並建立 MarketSnapshot。prices 已是 decimal（各 adapter 自行把原始格式轉好）。
    open 狀態：所有該有的邊都必須有合法賠率，否則 InvalidMarket（不寫入半個市場）。
    非 open（suspended / closed / unknown）：賠率非法者存 None，狀態照實保存，絕不當成 active quote。"""
    if market_type not in MARKET_TYPES or period not in PERIODS:
        raise InvalidMarket("unknown_market", f"{market_type}/{period}")
    if status not in STATUSES:
        raise InvalidMarket("unknown_status", status)
    if outcome_set == THREE_WAY and market_type != MONEYLINE:
        raise InvalidMarket("three_way_non_moneyline", market_type)

    sides = {MONEYLINE: [HOME, AWAY] + ([DRAW] if outcome_set == THREE_WAY else []),
             SPREAD: [HOME, AWAY], TOTAL: [OVER, UNDER]}[market_type]
    extra = set(k for k, v in prices.items() if v is not None) - set(sides)
    if extra:
        raise InvalidMarket("unexpected_outcome", ",".join(sorted(extra)))
    clean: dict[str, float | None] = {}
    for s in sides:
        if s not in prices or prices[s] is None:
            if status == OPEN:
                raise InvalidMarket("missing_side", s)
            clean[s] = None
            continue
        try:
            clean[s] = validate_decimal(prices[s])
        except InvalidMarket:
            if status == OPEN:
                raise
            clean[s] = None

    line = away = None
    if market_type == SPREAD:
        if home_line is None and away_line is None:
            raise InvalidMarket("missing_line", "spread")
        h = _validate_line(SPREAD, period, home_line if home_line is not None else away_line_from_home(away_line))
        a = _validate_line(SPREAD, period, away_line if away_line is not None else away_line_from_home(h))
        if abs(h + a) > 1e-9:
            raise InvalidMarket("asymmetric_spread", f"home {h} / away {a}")
        line, away = h, a
    elif market_type == TOTAL:
        if total_line is None:
            raise InvalidMarket("missing_line", "total")
        line = _validate_line(TOTAL, period, total_line)

    return MarketSnapshot(
        source=source, bookmaker=bookmaker, source_event_id=str(source_event_id), market_type=market_type,
        period=period, outcome_set=outcome_set, status=status, line=line, away_line=away,
        home_odds=clean.get(HOME), away_odds=clean.get(AWAY), draw_odds=clean.get(DRAW),
        over_odds=clean.get(OVER), under_odds=clean.get(UNDER), source_market_id=source_market_id,
        source_updated_at=source_updated_at, raw=raw or {},
    )


# ------------------------------------------------------------------ #
# 來源事件（adapter 輸出、matching 輸入）                                 #
# ------------------------------------------------------------------ #

@dataclass
class SourceEvent:
    source: str
    source_event_id: str
    home_name: str
    away_name: str
    commence_utc: datetime
    stage: str = "unknown"                 # 來源宣稱：preseason / regular / cup / playoffs / unknown
    snapshots: list[MarketSnapshot] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)     # [{market, reason, detail}]
    meta: dict = field(default_factory=dict)
