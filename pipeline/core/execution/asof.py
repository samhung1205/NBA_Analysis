"""
Exact as-of reconstruction（execution-v1）
------------------------------------------------------------
對每場比賽 G：T = scheduled_tipoff − 60 分鐘。只用 T 時點真正已知的資料重建定價與 sizing：

    odds        fetched_at ≤ T、且屬於單一 strategy scope（source + bookmaker）
                每個 series 取 T 時點最新一列（D.2 latest_snapshots_as_of；非 open → 該市場在 T 不可下注）
    freshness   報價在 T 的「最後確認時間」必須是 T 以前的輪詢（見 confirmed_last_seen_as_of）——
                odds_snapshots.last_seen_at 只保存最後一次確認；若它 > T，代表 T 之後的輪詢又確認了同一內容，
                那是 hindsight，不能拿來證明 T 時點報價是新鮮的。
    prediction  available_at = max(created_at, prediction_as_of) ≤ T 的最新有效預測（D.2 select_prediction）
    artifact    該 prediction 列記錄的 artifact_version；manifest trained_at ≤ T、bundle sha256 驗證通過（不可變）
    pricing     D.2 price_game_as_of（純函式；不讀 market_pricing_snapshots）
    sizing      D.3 size_pricings（純函式；不讀 bet_sizing_snapshots）

market_pricing_snapshots / bet_sizing_snapshots 只是 cache / 稽核，永遠不拿來當「當時已知」的結果。
T 時沒有有效 quote / prediction → no_bet；不會用 T 之後才出現的 quote / prediction 回填。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from ..pricing import engine as pricing_engine
from ..pricing.alignment import latest_snapshots_as_of, odds_input_from_row, price_game_as_of, select_prediction
from ..sizing import engine as sizing_engine
from ..sizing.policy import RiskPolicy
from ..timeutil import ensure_utc, parse_utc
from .policy import ExecutionPolicy, StrategyScope

# ---- no-bet reasons（每場都有一筆 decision；no_bet 也保存） ---- #
NO_ODDS = "no_odds"
MARKET_NOT_OPEN = "market_not_open"
STALE_ODDS = "stale_odds"
NO_PREDICTION = "no_prediction"
ARTIFACT_UNAVAILABLE = "artifact_unavailable"
UNSUPPORTED_MARKET = "unsupported_market"
INVALID_MARKET_DATA = "invalid_market_data"
NO_POSITIVE_EV = "no_positive_ev"
MUTUALLY_EXCLUSIVE = "mutually_exclusive_positive_kelly"
PORTFOLIO_CAP = "portfolio_cap"
DECISION_WINDOW_MISSED = "decision_window_missed"
DATA_UNAVAILABLE = "data_unavailable"
NO_BET_REASONS = (NO_ODDS, MARKET_NOT_OPEN, STALE_ODDS, NO_PREDICTION, ARTIFACT_UNAVAILABLE, UNSUPPORTED_MARKET,
                  INVALID_MARKET_DATA, NO_POSITIVE_EV, MUTUALLY_EXCLUSIVE, PORTFOLIO_CAP, DECISION_WINDOW_MISSED,
                  DATA_UNAVAILABLE)

CONFIRMING_RUN_OUTCOMES = ("success", "partial")


def decision_time(scheduled_tipoff: datetime, policy: ExecutionPolicy) -> datetime:
    return ensure_utc(parse_utc(scheduled_tipoff)) - policy.decision_offset


def betting_day(scheduled_tipoff: datetime) -> date:
    return sizing_engine.betting_day(ensure_utc(parse_utc(scheduled_tipoff)))


# ------------------------------------------------------------------ #
# 報價在 T 的最後確認時間                                                 #
# ------------------------------------------------------------------ #

def _json_obj(v: Any) -> Any:
    if isinstance(v, (dict, list)) or v is None:
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def run_covered_game(run: dict, game_id: int) -> bool:
    """odds_fetch_runs.diagnostics.events 是否記錄這場比賽被比對成功且帶有市場（events 最多存 60 筆；找不到 = 不確定 → False）。"""
    diag = _json_obj(run.get("diagnostics")) or {}
    for e in diag.get("events") or ():
        if e.get("game_id") == game_id and e.get("status") == "matched" and (e.get("n_snapshots") or 0) > 0 \
                and not e.get("skipped_in_play"):
            return True
    return False


def confirmed_last_seen_as_of(row: dict, as_of: datetime, runs: Iterable[dict] = ()) -> datetime:
    """這個 snapshot 在 as_of 時點「已知」的最後一次輪詢確認時間（≤ as_of）。

    last_seen_at ≤ as_of → 就是它（所有確認都發生在 T 以前）。
    last_seen_at > as_of → T 之後的輪詢又確認了同一內容；T 時點真正知道的是「T 以前最後一次涵蓋這場比賽的成功輪詢」：
        同來源、outcome ∈ success / partial、fetched_at ∈ [snapshot.fetched_at, as_of]、diagnostics 記錄這場比賽 → 取最大值；
        找不到（沒有 run 紀錄、run 不含這場）→ 保守退回 fetched_at（只會低估新鮮度，不會高估）。
    已知限制：run 的涵蓋判斷是「事件層級」；Odds API 單一 bookmaker 在某次輪詢暫時下架、之後原價恢復，無法從 run 紀錄分辨。"""
    as_of = ensure_utc(as_of)
    fetched = ensure_utc(parse_utc(row["fetched_at"]))
    seen = parse_utc(row.get("last_seen_at"))
    seen = max(ensure_utc(seen), fetched) if seen else fetched
    if seen <= as_of:
        return seen
    best = fetched
    for r in runs:
        t = parse_utc(r.get("fetched_at"))
        if t is None or r.get("source") != row.get("source") or r.get("outcome") not in CONFIRMING_RUN_OUTCOMES:
            continue
        t = ensure_utc(t)
        if fetched <= t <= as_of and t > best and run_covered_game(r, int(row["game_id"])):
            best = t
    return best


def rows_as_of(odds_rows: Iterable[dict], game_id: int, scope: StrategyScope, as_of: datetime,
               runs: Iterable[dict] = ()) -> list[dict]:
    """單一 scope、單一比賽、fetched_at ≤ T 的列；last_seen_at 換成 T 時點已知的確認時間（複本，不改輸入）。"""
    as_of = ensure_utc(as_of)
    runs = list(runs)
    out = []
    for r in odds_rows:
        if int(r["game_id"]) != int(game_id) or not scope.matches(r):
            continue
        if ensure_utc(parse_utc(r["fetched_at"])) > as_of:
            continue                                                   # T 之後才觀測到的報價：不存在
        out.append({**r, "last_seen_at": confirmed_last_seen_as_of(r, as_of, runs)})
    return out


def quote_is_stale(source: str | None, fetched_at: datetime, confirmed_at: datetime, as_of: datetime,
                   risk: RiskPolicy) -> bool:
    """與 D.3 sizing.engine._freshness 相同的 risk-v1 規則（last_seen_age > 2 × 來源輪詢間隔；未知來源 = stale）。"""
    max_age = risk.max_quote_age(source)
    if max_age is None:
        return True
    seen = min(max(ensure_utc(confirmed_at), ensure_utc(fetched_at)), ensure_utc(as_of))
    return (ensure_utc(as_of) - seen) > max_age


# ------------------------------------------------------------------ #
# Artifact：T 時點必須已存在、且內容不可變                                   #
# ------------------------------------------------------------------ #

class ArtifactRegistry(Protocol):
    def trained_at(self, version: str) -> datetime | None:
        """artifact 版本的建立時間（manifest trained_at_utc）；None = 不存在。"""


class LocalArtifactRegistry:
    """production：pipeline/artifacts/production/versions/<version>/manifest.json（sha256 由 load_version 驗證）。"""

    def __init__(self, root: str | Path | None = None):
        from ..production import artifact as art_mod

        self.root = art_mod.artifact_root(root)

    def trained_at(self, version: str) -> datetime | None:
        p = Path(self.root) / "versions" / str(version) / "manifest.json"
        if not version or not p.is_file():
            return None
        try:
            return parse_utc(json.loads(p.read_text()).get("trained_at_utc"))
        except (OSError, ValueError, TypeError):
            return None


class StaticArtifactRegistry:
    """fixture / 測試：{version: trained_at}。"""

    def __init__(self, versions: dict[str, datetime]):
        self.versions = {k: ensure_utc(v) for k, v in versions.items()}

    def trained_at(self, version: str) -> datetime | None:
        return self.versions.get(version)


# ------------------------------------------------------------------ #
# 單場重建                                                              #
# ------------------------------------------------------------------ #

@dataclass
class Reconstruction:
    game_id: int
    scheduled_tipoff: datetime
    decision_time: datetime
    betting_day: date
    scope: StrategyScope
    odds_snapshot_ids: list[int] = field(default_factory=list)
    prediction: pricing_engine.PredictionInput | None = None
    pricings: list[pricing_engine.MarketPricing] = field(default_factory=list)
    pre_reason: str | None = None                  # sizing 之前就確定的 no-bet 原因（報價 / 預測 / artifact 可用性）
    blockers: dict[str, Any] = field(default_factory=dict)
    artifact_error: str | None = None

    @property
    def artifact_version(self) -> str | None:
        return self.prediction.artifact_version if self.prediction else None

    def pricing_fingerprint(self) -> str:
        payload = json.dumps([mp.to_dict() for mp in self.pricings], sort_keys=True, default=str,
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def reconstruct_game(game: dict, as_of: datetime, *, scope: StrategyScope, odds_rows: Iterable[dict],
                     pred_rows: Iterable[dict], runs: Iterable[dict], risk: RiskPolicy, policy: ExecutionPolicy,
                     model_for: Callable[[pricing_engine.PredictionInput], pricing_engine.ModelProbabilities],
                     artifacts: ArtifactRegistry) -> Reconstruction:
    as_of = ensure_utc(as_of)
    gid, tip = int(game["id"]), ensure_utc(parse_utc(game["date_utc"]))
    rec = Reconstruction(gid, tip, as_of, betting_day(tip), scope)
    rows = rows_as_of(odds_rows, gid, scope, as_of, runs)
    latest = latest_snapshots_as_of(rows, as_of)
    rec.odds_snapshot_ids = sorted(int(r["id"]) for r in latest)
    pred_rows = list(pred_rows)
    pred = select_prediction(pred_rows, gid, as_of)
    rec.prediction = pred

    # ---- artifact：存在、T 以前建立、可載入（sha256） ---- #
    model_ok = pred is not None
    if pred is not None:
        made = artifacts.trained_at(pred.artifact_version)
        if made is None:
            rec.artifact_error, model_ok = "artifact_missing", False
        elif ensure_utc(made) > as_of:
            rec.artifact_error, model_ok = "artifact_created_after_decision_time", False
    if model_ok:
        try:
            rec.pricings = price_game_as_of(rows, pred_rows, gid, as_of, model_for)
        except pricing_engine.ModelProbabilityError as e:
            rec.artifact_error, model_ok = f"artifact_load_failed:{str(e)[:120]}", False
    if not model_ok:                                         # 沒有可用模型 → 仍記錄 T 時點的盤口（market_only）
        rec.pricings = price_game_as_of(rows, [], gid, as_of, model_for)

    # ---- 可用性 gate（順序固定：報價存在 → open → 結算支援 → 新鮮 → 預測 → artifact） ---- #
    by_snap = {int(r["id"]): r for r in latest}
    n_open = n_invalid = n_closed = n_stale = n_supported = n_fresh_supported = 0
    for mp in rec.pricings:
        if mp.status == pricing_engine.REJECTED and (mp.status_reason or "").startswith("market_not_open"):
            n_closed += 1
            continue
        if mp.status == pricing_engine.REJECTED:
            n_invalid += 1
            continue
        n_open += 1
        r = by_snap[int(mp.odds_snapshot_id)]
        stale = quote_is_stale(mp.source, r["fetched_at"], r["last_seen_at"], as_of, risk)
        n_stale += stale
        if mp.status not in (pricing_engine.UNSUPPORTED_SETTLEMENT, pricing_engine.UNSUPPORTED_MARKET):
            n_supported += 1
            n_fresh_supported += not stale
    rec.blockers = {"n_snapshots": len(latest), "n_open": n_open, "n_closed": n_closed, "n_invalid": n_invalid,
                    "n_stale": n_stale, "n_supported": n_supported, "n_fresh_supported": n_fresh_supported,
                    "prediction_id": pred.prediction_id if pred else None, "artifact_error": rec.artifact_error}
    if not latest:
        rec.pre_reason = NO_ODDS
    elif n_open == 0:
        rec.pre_reason = MARKET_NOT_OPEN if n_closed else INVALID_MARKET_DATA
    elif n_supported == 0:
        rec.pre_reason = UNSUPPORTED_MARKET
    elif n_fresh_supported == 0:
        rec.pre_reason = STALE_ODDS
    elif pred is None:
        rec.pre_reason = NO_PREDICTION
    elif not model_ok:
        rec.pre_reason = ARTIFACT_UNAVAILABLE
    return rec
