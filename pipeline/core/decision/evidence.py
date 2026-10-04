"""
Production evidence panel（純函式；evidence 狀態**不會**改變任何 qualification / 額度 / 策略選擇）
------------------------------------------------------------
    historical model evidence     available（C.5C / C.5E walk-forward OOS；點預測 / 機率校準，不涉及盤口）
    historical betting evidence   unavailable（D.4：正式 DB 2024-25 / 2025-26 沒有任何觀測盤口 → evidence_class = none）
    prospective paper             D.4 paper ledger（execution-v1，台彩）：betting days / graded days / bets；
                                  ≥ 30 個有 graded bet 的 betting day 才輸出 day-block bootstrap 95% CI，否則 insufficient_sample
    actual user record            另外顯示（bankroll.actual_performance），永遠不混入 paper / 策略證據
"""
from __future__ import annotations

from typing import Any

from ..execution import metrics, settlement
from ..execution.engine import NO_BET, GameDecision, day_ledgers
from . import policy as P

HISTORICAL_MODEL = {"status": "available", "source": "C.5C / C.5E walk-forward out-of-sample (model accuracy & "
                    "calibration; not betting performance)"}
HISTORICAL_BETTING = {"status": "unavailable", "source": "D.4: no observed odds snapshots for 2024-25 / 2025-26 → "
                      "evidence_class = none (no ROI computed)"}


def paper_evidence(decisions: list[GameDecision] | None, *, strategy_id: str | None = None,
                   n_resamples: int = metrics.BOOTSTRAP_RESAMPLES) -> dict[str, Any]:
    if decisions is None:
        return {"status": "unavailable", "reason": "paper ledger not available (migration 0007 not applied)",
                "strategy_id": strategy_id}
    days = day_ledgers(decisions) if decisions else []
    wagers = [w for d in decisions for w in d.wagers]
    graded_days = {w.betting_day for w in wagers if w.settlement_status in settlement.GRADED}
    out = {"status": "accumulating", "strategy_id": strategy_id, "evidence_label": P.TAIWAN_LABEL,
           "n_decisions": len(decisions), "n_bet_decisions": sum(d.decision_status != NO_BET for d in decisions),
           "n_betting_days": len({d.betting_day for d in decisions}),
           "n_betting_days_with_bets": len({w.betting_day for w in wagers}),
           "n_graded_betting_days": len(graded_days), "n_bets": len(wagers),
           "n_graded_bets": sum(w.settlement_status in settlement.GRADED for w in wagers),
           "min_days_for_ci": metrics.MIN_BOOTSTRAP_DAYS}
    if len(graded_days) < metrics.MIN_BOOTSTRAP_DAYS:
        out["bootstrap"] = {"status": "insufficient_sample", "n_days": len(graded_days),
                            "min_days": metrics.MIN_BOOTSTRAP_DAYS}
    else:
        b = metrics.day_block_bootstrap(days, decisions, n_resamples=n_resamples)
        out["bootstrap"] = {k: b.get(k) for k in ("status", "n_days", "min_days", "yield_point", "yield_ci95",
                                                   "n_resamples", "seed")}
    out["statement"] = ("prospective paper tracking (Taiwan Sports Lottery, execution-v1) — descriptive only; "
                        "does not change strategy selection or stake")
    return out


def evidence_panel(paper: dict[str, Any]) -> dict[str, Any]:
    return {"badge": P.EVIDENCE_BADGE, "badge_text": "Prospective validation / 尚在前瞻驗證",
            "historical_model_evidence": HISTORICAL_MODEL, "historical_betting_evidence": HISTORICAL_BETTING,
            "prospective_paper": paper,
            "affects_strategy": False,
            "forbidden_claims": ["proven edge", "guaranteed value", "high-confidence winner", "必買", "穩贏", "強推"]}
