"""
Deterministic synthetic validation slate（execution-v1 引擎驗證；validation_only — 不是 ROI 證據）
------------------------------------------------------------
2 個 betting day、9 場比賽，涵蓋：win / loss / push / H1 和局中獎 / no bet / 正 EV sizing / game cap / 當日 8% 上限
（後面的批次被縮放、最後一場 portfolio_cap）/ stale 報價 / 缺預測（T 之後才出現的預測不回填）/
T 之後才出現的盤口不回填 / T 之後的較佳賠率不使用。

模型機率由 FixtureModel 直接給定（合成；不經 artifact），價格 / 結算 / sizing / controller 全部走 production 程式碼。
手算的 bankroll 路徑（Fraction 精確值）在 EXPECTED，測試逐項核對：

  Day 1（台灣 2026-10-22，day_start = 1）
    批次 T=22:00Z：G101 ML 主 2.00（p .60 → ¼K .05 → 2%）+ 讓分 −4 主 2.00（p .44/.20/.36 → full .1 → 2%）→ 同場 4% → ×0.75 → 各 1.5%
                   G102 大分 220.5 2.00（p .60 → 2%）+ 上半場三向「和」12.0（p .10 → full 1/55 → ¼ 1/220）
                   批次合計 0.03 + 0.02 + 1/220 = 0.0545… ≤ 8% → 全額；committed = 0.06 − 0.02 + 1/220
    批次 T=00:30Z：G103 ML 2% + 讓分 −5.5 2% → 同場 3%；剩餘 0.08 − committed = 0.03 − 1/220 = 7/275
                   → 執行縮放 (7/275)/0.03 = 28/33 → 各 7/550；當日 committed = 8%
    T=01:00Z：G104 正 EV 但剩餘 0 → no_bet portfolio_cap
    T=01:30Z：G105 沒有 T 以前的預測（T+5 分才寫入）→ no_bet no_prediction
    結算：G101 110–106（分差 4）→ ML 贏 +0.015、讓分 −4 push 0；G102 108–102 總分 210 → 大分輸 −0.02；
          上半場 50–50 → 和局中獎 +11/220 = +0.05；G103 100–108 → 兩筆輸 −14/550
    day_profit = 0.015 + 0 − 0.02 + 0.05 − 14/550 = 43/2200；day_end = 2243/2200
  Day 2（台灣 2026-10-23，day_start = 2243/2200）
    G106 報價最後確認於 T−3h（台彩上限 60 分）→ no_bet stale_odds
    G107 ML 主 2.00 p .52 → EV .04 → ¼K = 1% → stake = 2243/220000；112–100 贏 → +stake
    G108 兩邊 EV < 0 → no_bet no_positive_ev
    G109 盤口 T+10 分才出現 → no_bet no_odds
    day_end = 2243/2200 × 1.01
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Callable

from ..odds.canonical import FULL_GAME, H1, MONEYLINE, OPEN, SPREAD, THREE_WAY, TOTAL, make_snapshot
from ..production import spec
from .asof import StaticArtifactRegistry

UTC = timezone.utc
FIXTURE_ARTIFACT = "ml-v2.0+d4fixture"
ARTIFACT_TRAINED_AT = datetime(2026, 10, 1, tzinfo=UTC)
FIXTURE_TAG = "d4_fixture"


class FixtureModel:
    """合成模型機率：{(target, threshold): (P(above), P(push), P(below))}，介面同 C.5E line probability。"""

    def __init__(self, table: dict[tuple[str, float], tuple[float, float, float]], artifact_version: str):
        self.table, self.artifact_version = table, artifact_version

    def line(self, target: str, threshold: float) -> dict[str, Any]:
        a, p, b = self.table[(target, float(threshold))]
        return {"probability_above": a, "probability_push": p, "probability_below": b,
                "artifact_version": self.artifact_version, "distribution_version": spec.DISTRIBUTION_VERSION,
                "point_prediction": None, "distribution": "fixture", "center": None, "scale": None,
                "data_quality": {"flags": []}}


def _game(gid: int, tip: datetime, h: tuple[int, int, int, int], a: tuple[int, int, int, int],
          status: str = "final") -> dict:
    return {"id": gid, "date_utc": tip, "status": status, "season": "2026-27",
            "home_pts": sum(h), "away_pts": sum(a), "home_h1": h[0] + h[1], "away_h1": a[0] + a[1],
            **{f"home_q{i + 1}": v for i, v in enumerate(h)}, **{f"away_q{i + 1}": v for i, v in enumerate(a)},
            "home_ot": None, "away_ot": None}


def odds_row(rid: int, gid: int, snap, fetched: datetime, last_seen: datetime | None = None, *,
             tag: str | None = FIXTURE_TAG, fetch_run_id: int | None = None, content_hash: str | None = "fixture",
             normalizer_version: str | None = "odds-norm-v1") -> dict:
    """MarketSnapshot（D.1 canonical 驗證過）→ odds_snapshots 形狀的列。"""
    raw = {tag: True} if tag else {}
    return {"id": rid, "game_id": gid, "fetched_at": fetched, "last_seen_at": last_seen or fetched,
            "source": snap.source, "bookmaker": snap.bookmaker, "market": snap.market, "market_type": snap.market_type,
            "period": snap.period, "outcome_set": snap.outcome_set, "line": snap.line, "away_line": snap.away_line,
            "home_odds": snap.home_odds, "away_odds": snap.away_odds, "draw_odds": snap.draw_odds,
            "over_odds": snap.over_odds, "under_odds": snap.under_odds, "market_status": snap.status,
            "model_target": snap.model_target, "model_threshold": snap.model_threshold,
            "source_event_id": snap.source_event_id, "content_hash": content_hash, "fetch_run_id": fetch_run_id,
            "normalizer_version": normalizer_version, "raw_json": raw}


def tw(mt: str, period: str = FULL_GAME, outcome_set: str = "two_way", **kw):
    return make_snapshot(source="twsport", bookmaker="twsport", source_event_id="FX", market_type=mt, period=period,
                         status=OPEN, outcome_set=outcome_set, **kw)


def pred_row(pid: int, gid: int, created: datetime, *, artifact: str = FIXTURE_ARTIFACT, tag: bool = True) -> dict:
    fj = {"artifact_version": artifact, "profile": "final", "prediction_kind": "final",
          "prediction_as_of_utc": created.isoformat()}
    if tag:
        fj[FIXTURE_TAG] = True
    return {"id": pid, "game_id": gid, "model_version": spec.MODEL_VERSION, "created_at": created,
            "home_win_prob": 0.6, "pred_margin": 4.0, "pred_total": 220.0, "pred_home_h1": 55.0, "pred_away_h1": 53.0,
            "features_json": fj}


@dataclass
class FixtureSlate:
    games: list[dict]
    odds_rows: list[dict]
    pred_rows: list[dict]
    runs: list[dict]
    model_for: Callable
    artifacts: StaticArtifactRegistry
    tables: dict[int, dict]


ML = ("margin", 0.0)
D1_T1 = datetime(2026, 10, 21, 22, 0, tzinfo=UTC)      # 台灣 10/22 06:00
D2_T1 = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)      # 台灣 10/23 06:00


def build_fixture() -> FixtureSlate:
    H = timedelta(hours=1)
    m = timedelta(minutes=1)
    games, rows, preds, tables = [], [], [], {}

    def add(gid, tip, h, a, markets, table, *, pred_at=None, fetched=None, seen=None):
        t = tip - H
        games.append(_game(gid, tip, h, a))
        tables[gid] = table
        for i, snap in enumerate(markets):
            rows.append(odds_row(gid * 100 + i, gid, snap, fetched or t - 20 * m, seen or t - 3 * m))
        if pred_at is not False:
            preds.append(pred_row(gid, gid, pred_at or t - 15 * m))

    ml_pos = tw(MONEYLINE, prices={"home": 2.0, "away": 1.8})
    # ---------------- Day 1 ---------------- #
    add(101, D1_T1 + H, (28, 27, 25, 30), (25, 25, 28, 28),
        [ml_pos, tw(SPREAD, home_line=-4.0, prices={"home": 2.0, "away": 1.8})],
        {ML: (0.60, 0.0, 0.40), ("margin", 4.0): (0.44, 0.20, 0.36)})
    rows.append(odds_row(10199, 101, tw(MONEYLINE, prices={"home": 2.5, "away": 1.6}),
                         D1_T1 + 10 * m, D1_T1 + 30 * m))                        # T 之後較佳賠率：不得使用
    add(102, D1_T1 + H, (25, 25, 30, 28), (26, 24, 27, 25),
        [tw(TOTAL, total_line=220.5, prices={"over": 2.0, "under": 1.8}),
         tw(MONEYLINE, H1, outcome_set=THREE_WAY, prices={"home": 1.9, "draw": 12.0, "away": 2.1})],
        {("total", 220.5): (0.60, 0.0, 0.40), ("h1_margin", 0.0): (0.45, 0.10, 0.45)})
    t3 = datetime(2026, 10, 22, 1, 30, tzinfo=UTC)
    add(103, t3, (25, 25, 25, 25), (27, 27, 27, 27),
        [ml_pos, tw(SPREAD, home_line=-5.5, prices={"home": 2.0, "away": 1.8})],
        {ML: (0.60, 0.0, 0.40), ("margin", 5.5): (0.60, 0.0, 0.40)})
    add(104, datetime(2026, 10, 22, 2, 0, tzinfo=UTC), (25, 25, 25, 24), (24, 24, 24, 23), [ml_pos],
        {ML: (0.60, 0.0, 0.40)})
    t5 = datetime(2026, 10, 22, 2, 30, tzinfo=UTC)
    add(105, t5, (25, 25, 25, 25), (20, 20, 20, 20), [ml_pos], {ML: (0.60, 0.0, 0.40)},
        pred_at=t5 - H + 5 * m)                                                   # T+5 分才有預測：不回填
    # ---------------- Day 2 ---------------- #
    add(106, D2_T1 + H, (30, 30, 20, 20), (20, 20, 20, 20), [ml_pos], {ML: (0.60, 0.0, 0.40)},
        fetched=D2_T1 - 3 * H - 30 * m, seen=D2_T1 - 3 * H)                       # 最後確認 T−3h → stale
    t7 = D2_T1 + H + 30 * m
    add(107, t7, (28, 28, 28, 28), (25, 25, 25, 25),
        [tw(MONEYLINE, prices={"home": 2.0, "away": 1.8}),
         tw(SPREAD, home_line=-2.5, prices={"home": 1.8, "away": 1.8})],
        {ML: (0.52, 0.0, 0.48), ("margin", 2.5): (0.50, 0.0, 0.50)})
    t8 = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)
    add(108, t8, (25, 25, 25, 25), (24, 24, 24, 24), [tw(MONEYLINE, prices={"home": 1.5, "away": 2.5})],
        {ML: (0.62, 0.0, 0.38)})
    t9 = datetime(2026, 10, 23, 0, 30, tzinfo=UTC)
    add(109, t9, (25, 25, 25, 25), (26, 26, 26, 26), [ml_pos], {ML: (0.60, 0.0, 0.40)},
        fetched=t9 - H + 10 * m, seen=t9 - H + 20 * m)                            # 盤口 T+10 分才出現：不回填

    def model_for(pred):
        return FixtureModel(tables[pred.game_id], pred.artifact_version)

    return FixtureSlate(games, rows, preds, [], model_for, StaticArtifactRegistry({FIXTURE_ARTIFACT: ARTIFACT_TRAINED_AT}),
                        tables)


F = Fraction
D1_START = F(1)
D1_PROFIT = F(15, 1000) + 0 - F(2, 100) + F(11, 220) - F(14, 550)
D1_END = D1_START + D1_PROFIT
D2_STAKE = D1_END * F(1, 100)
D2_END = D1_END + D2_STAKE

EXPECTED = {
    "decisions": {101: ("bet", None), 102: ("bet", None), 103: ("bet", None), 104: ("no_bet", "portfolio_cap"),
                  105: ("no_bet", "no_prediction"), 106: ("no_bet", "stale_odds"), 107: ("bet", None),
                  108: ("no_bet", "no_positive_ev"), 109: ("no_bet", "no_odds")},
    "wagers": {   # (game, market, side) → (stake_fraction, settlement_status, profit)
        (101, "ml", "home"): (F(15, 1000), "settled_win", F(15, 1000)),
        (101, "spread", "home"): (F(15, 1000), "settled_push", F(0)),
        (102, "total", "over"): (F(2, 100), "settled_loss", -F(2, 100)),
        (102, "h1_ml", "draw"): (F(1, 220), "settled_draw_win", F(11, 220)),
        (103, "ml", "home"): (F(7, 550), "settled_loss", -F(7, 550)),
        (103, "spread", "home"): (F(7, 550), "settled_loss", -F(7, 550)),
        (107, "ml", "home"): (F(1, 100), "settled_win", D2_STAKE),
    },
    "day_start": {"2026-10-22": D1_START, "2026-10-23": D1_END},
    "day_profit": {"2026-10-22": D1_PROFIT, "2026-10-23": D2_STAKE},
    "day_end": {"2026-10-22": D1_END, "2026-10-23": D2_END},
    "execution_scale_103": F(28, 33),
}
assert D1_PROFIT == F(43, 2200) and D1_END == F(2243, 2200)


def run_fixture_validation(*, starting_bankroll: float = 1.0) -> dict[str, Any]:
    """引擎驗證報告（validation_only = true；evidence_class = fixture_validation）。"""
    from . import evidence, metrics
    from .engine import simulate
    from .policy import PRIMARY_SCOPE

    fx = build_fixture()
    sim = simulate(fx.games, fx.odds_rows, fx.pred_rows, fx.runs, scope=PRIMARY_SCOPE, model_for=fx.model_for,
                   artifacts=fx.artifacts)
    return {"evidence": evidence.evidence_summary(scope=PRIMARY_SCOPE, validation_only=True),
            "strategy_id": sim.strategy_id, "starting_bankroll_display": starting_bankroll,
            "metrics": metrics.strategy_metrics(sim.decisions, sim.days, sim.starting_bankroll),
            "days": [d.to_dict() for d in sim.days],
            "decisions": [d.to_dict() for d in sim.decisions]}
