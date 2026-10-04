"""
Evidence classification / historical evidence guard
------------------------------------------------------------
三種證據絕不混用：
    A. historical model evaluation   C.5C / C.5E walk-forward OOS（點預測 / 機率校準；不涉及盤口）
    B. historical betting performance  只能來自「真實觀測」的 odds snapshot + 真實賽果 + T 時點合法的預測 / artifact
    C. prospective paper tracking     2026-27 起 execution-v1 在 T-60 即時記錄的 paper ledger

odds 列的 provenance：
    observed            D.1 寫入（content_hash、normalizer_version）且 fetch_run_id 指向同來源、outcome ∈ success/partial、
                        fetched_at 相同的 odds_fetch_runs 列（即時輪詢或 HAR 匯入：fetched_at = 實際觀測時間）
    seed_fixture        沒有 content_hash / normalizer_version（seed、舊格式列）或 raw_json 標記 seed
    synthetic_fixture   raw_json 標記 fixture / synthetic（D.4 validation slate）
    unverified          有 D.1 欄位但找不到對應的成功抓取紀錄（不可當歷史證據）

run 的 evidence_class：
    historical_observed   全部使用的 odds 列都是 observed（seed / synthetic / unverified 在載入時就被排除並計數）
    fixture_validation    validation_only（fixture / synthetic）——只驗證引擎，不是績效
    prospective_paper     paper ledger
    none                  期間內沒有任何可用的真實盤口 → historical_evidence_available = false（不輸出 ROI = 0）
evidence_label（strategy scope 決定）：twsport → taiwan_sports_lottery_strategy；其他 → international_market_diagnostic。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..production import spec
from ..timeutil import ensure_utc, parse_utc
from .policy import StrategyScope

OBSERVED = "observed"
SEED_FIXTURE = "seed_fixture"
SYNTHETIC_FIXTURE = "synthetic_fixture"
UNVERIFIED = "unverified"

HISTORICAL_OBSERVED = "historical_observed"
FIXTURE_VALIDATION = "fixture_validation"
PROSPECTIVE_PAPER = "prospective_paper"
NO_EVIDENCE = "none"

OK_RUN_OUTCOMES = ("success", "partial")
FIXTURE_MARKERS = ("fixture", "synthetic", "d4_fixture")


class EvidenceError(RuntimeError):
    """把非真實觀測資料當成歷史績效。"""


def _raw(row: dict) -> dict:
    v = row.get("raw_json")
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = None
    return v if isinstance(v, dict) else {}


def classify_odds_row(row: dict, runs_by_id: dict[int, dict]) -> tuple[str, str | None]:
    raw = _raw(row)
    if any(raw.get(m) for m in FIXTURE_MARKERS):
        return SYNTHETIC_FIXTURE, "fixture_marker"
    if raw.get("seed") or not row.get("content_hash") or not row.get("normalizer_version"):
        return SEED_FIXTURE, "legacy_or_seed_row"
    rid = row.get("fetch_run_id")
    run = runs_by_id.get(int(rid)) if rid is not None else None
    if run is None:
        return UNVERIFIED, "no_fetch_run"
    if run.get("source") != row.get("source"):
        return UNVERIFIED, "fetch_run_source_mismatch"
    if run.get("outcome") not in OK_RUN_OUTCOMES:
        return UNVERIFIED, f"fetch_run_outcome:{run.get('outcome')}"
    rf, of = parse_utc(run.get("fetched_at")), parse_utc(row.get("fetched_at"))
    if rf is None or of is None or ensure_utc(rf) != ensure_utc(of):
        return UNVERIFIED, "fetch_time_mismatch"
    seen = parse_utc(row.get("last_seen_at"))
    if seen is not None and ensure_utc(seen) < ensure_utc(of):
        return UNVERIFIED, "last_seen_before_fetched"
    return OBSERVED, None


def partition_odds_rows(rows: Iterable[dict], runs: Iterable[dict]) -> tuple[list[dict], dict[str, int]]:
    """→（observed 列, 被排除列的分類計數）。歷史引擎只用 observed 列。"""
    runs_by_id = {int(r["id"]): r for r in runs if r.get("id") is not None}
    keep, rejected = [], {}
    for r in rows:
        c, why = classify_odds_row(r, runs_by_id)
        if c == OBSERVED:
            keep.append(r)
        else:
            k = f"{c}:{why}"
            rejected[k] = rejected.get(k, 0) + 1
    return keep, rejected


def prediction_is_production(row: dict) -> bool:
    fj = row.get("features_json")
    if isinstance(fj, str):
        try:
            fj = json.loads(fj)
        except ValueError:
            fj = None
    return row.get("model_version") == spec.MODEL_VERSION and isinstance(fj, dict) and bool(fj.get("artifact_version")) \
        and not fj.get("seed") and not any(fj.get(m) for m in FIXTURE_MARKERS)


def evidence_summary(*, scope: StrategyScope, validation_only: bool, prospective: bool = False,
                     n_observed_rows: int = 0, rejected_rows: dict[str, int] | None = None,
                     used_snapshot_ids: Iterable[int] = (), observed_ids: Iterable[int] = ()) -> dict[str, Any]:
    """run 的 evidence 標記。historical_observed 只有在「全部使用的 snapshot 都是 observed」時成立。"""
    used, obs = set(used_snapshot_ids), set(observed_ids)
    if validation_only:
        cls = FIXTURE_VALIDATION
    elif prospective:
        cls = PROSPECTIVE_PAPER
    elif n_observed_rows == 0:
        cls = NO_EVIDENCE
    else:
        if not used <= obs:
            raise EvidenceError(f"使用了非 observed 的 snapshot：{sorted(used - obs)[:10]}")
        cls = HISTORICAL_OBSERVED
    return {"evidence_class": cls, "evidence_label": scope.evidence_label, "strategy_scope": scope.kind,
            "source": scope.source, "bookmaker": scope.bookmaker, "validation_only": bool(validation_only),
            "historical_evidence_available": cls == HISTORICAL_OBSERVED,
            "is_taiwan_sports_lottery_evidence": scope.source == "twsport" and cls in (HISTORICAL_OBSERVED,
                                                                                         PROSPECTIVE_PAPER),
            "n_observed_odds_rows": n_observed_rows, "rejected_odds_rows": dict(rejected_rows or {}),
            "statement": _statement(cls, scope)}


def _statement(cls: str, scope: StrategyScope) -> str:
    if cls == FIXTURE_VALIDATION:
        return "fixture / synthetic engine validation only — NOT historical betting performance"
    if cls == NO_EVIDENCE:
        return "no observed odds snapshots in the requested period — no historical betting evidence (no ROI computed)"
    what = "Taiwan Sports Lottery strategy" if scope.source == "twsport" else \
        f"international market diagnostic ({scope.source}:{scope.bookmaker}) — not Taiwan Sports Lottery evidence"
    if cls == PROSPECTIVE_PAPER:
        return f"prospective paper tracking, {what}"
    return f"historical reconstruction from observed snapshots, {what}"
