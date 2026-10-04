"""Phase D.3：bet_sizing_snapshots 持久化（暫存 schema；真實合成 artifact → D.2 定價 → D.3 sizing）

- 決定性 / 冪等（同一定價輸入 × 同一 policy × 同一組合 → 不新增）
- risk-v1 列不改寫；policy 改版或組合改變 → 新列、舊列保留
- dry-run / bankroll 不寫入；不碰 bets
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.odds.canonical import FULL_GAME, MONEYLINE, OPEN, SPREAD, TOTAL, make_snapshot
from core.pricing.job import pricing_job
from core.production import probability
from core.sizing import engine, job as sizing, policy
from core.sizing.policy import RISK_V1, RiskPolicy
from test_pricing_artifact import S, TIP, game, insert_pred, league, pred_row, setup, write_odds  # noqa: F401


def sizing_rows(db, gid=None):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM bet_sizing_snapshots" + (" WHERE game_id = %s" if gid else "") + " ORDER BY id",
                    (gid,) if gid else ())
        return cur.fetchall()


def generous_ml(setup):
    """以真實分佈的 P(主勝) 造一個主隊正 EV、overround ≥ 0 的獨贏盤。"""
    q = probability.line_probability_from_prediction_row(pred_row(setup["gp"]), "margin", 0.0,
                                                         art=setup["art_a"])["probability_above"]
    oh = round(1.15 / q, 2)
    oa = round(0.97 / (1 - 1 / oh), 2)
    assert 1 / oh + 1 / oa >= 1 and q * oh > 1
    return oh, oa


def prepare(db, game, setup, t1):
    pid = insert_pred(db, game, setup, t1 - timedelta(hours=1))
    oh, oa = generous_ml(setup)
    write_odds(db, game, S(MONEYLINE, prices={"home": oh, "away": oa}), t1)
    write_odds(db, game, S(SPREAD, home_line=-4.0, prices={"home": 1.91, "away": 1.91}), t1)
    write_odds(db, game, make_snapshot(source="oddsapi", bookmaker="draftkings", source_event_id="X",
                                       market_type=TOTAL, period=FULL_GAME, status=OPEN, total_line=221.5,
                                       prices={"over": 1.91, "under": 1.91}), t1)
    return pid


def test_sizing_job_persists_deterministically_and_idempotently(db, game, setup):
    t1 = TIP - timedelta(hours=10)
    pid = prepare(db, game, setup, t1)
    now = t1 + timedelta(minutes=5)
    assert pricing_job(now, root=setup["root"], db=db).rows_inserted == 6
    rep = sizing.sizing_job(now, db=db)
    assert rep.candidates == 6 and rep.rows_inserted == 6 and rep.pricing_missing == []
    rows = sizing_rows(db, game)
    assert {r["risk_policy_version"] for r in rows} == {"risk-v1"} and {r["prediction_id"] for r in rows} == {pid}
    assert all(r["betting_day"].isoformat() == "2026-10-22" for r in rows)              # 台灣開賽日期
    assert len({r["portfolio_key"] for r in rows}) == 1 and all(r["analysis_as_of"] == now for r in rows)
    home_ml = next(r for r in rows if r["market"] == "ml" and r["side"] == "home")
    assert home_ml["qualification_status"] in ("eligible", "exposure_scaled") and home_ml["actionable"] is True
    assert home_ml["final_stake_fraction"] > 0 and home_ml["final_stake_fraction"] <= 0.02
    assert sum(r["final_stake_fraction"] for r in rows) <= 0.03 + 1e-12
    assert [r for r in rows if r["actionable"]] == [r for r in rows if r["final_stake_fraction"] > 0]
    # 重跑（同一組合、T 前進）→ 不新增、不改寫
    before = [dict(r) for r in rows]
    rep2 = sizing.sizing_job(now + timedelta(minutes=5), db=db)
    assert rep2.rows_inserted == 0 and rep2.rows_existing == 6
    assert [dict(r) for r in sizing_rows(db, game)] == before
    # 存的值 = 以相同輸入重算的值（決定性）
    redo = {(r.market_pricing_snapshot_id, r.side): r for r in
            engine.size_portfolio(sizing.select_candidates(*_load(db, now), now).candidates, as_of=now)}
    for r in rows:
        x = redo[(r["market_pricing_snapshot_id"], r["side"])]
        assert r["qualification_status"] == x.qualification_status and r["portfolio_key"] == x.portfolio_key
        for k in ("full_kelly_fraction", "fractional_kelly_fraction", "single_bet_capped_fraction",
                  "final_stake_fraction", "game_scale_factor", "daily_scale_factor"):
            assert r[k] == pytest.approx(getattr(x, k), rel=1e-12, abs=1e-15)


def _load(db, now):
    with db.cursor() as cur:
        return sizing.load_inputs(cur, now, timedelta(hours=48))


def test_policy_version_change_creates_new_rows_and_keeps_risk_v1(db, game, setup, monkeypatch):
    t1 = TIP - timedelta(hours=10)
    prepare(db, game, setup, t1)
    now = t1 + timedelta(minutes=5)
    pricing_job(now, root=setup["root"], db=db)
    sizing.sizing_job(now, db=db)
    v1 = [dict(r) for r in sizing_rows(db, game)]
    other = RiskPolicy("risk-test", 0.5, 0.01, 0.02, 0.05, 2.0, dict(RISK_V1.source_poll_interval_min))
    with pytest.raises(ValueError):                                     # 未登記的 policy 不能寫入
        sizing.sizing_job(now, db=db, policy=other)
    monkeypatch.setitem(policy.REGISTERED_POLICIES, "risk-test", other)
    rep = sizing.sizing_job(now, db=db, policy=other)
    assert rep.rows_inserted == 6
    rows = sizing_rows(db, game)
    assert len(rows) == 12 and [dict(r) for r in rows if r["risk_policy_version"] == "risk-v1"] == v1
    new = [r for r in rows if r["risk_policy_version"] == "risk-test"]
    assert all(r["single_bet_capped_fraction"] is None or r["single_bet_capped_fraction"] <= 0.01 for r in new)


def test_portfolio_change_creates_new_rows_old_rows_immutable(db, game, setup):
    t1 = TIP - timedelta(hours=10)
    prepare(db, game, setup, t1)
    pricing_job(t1 + timedelta(minutes=5), root=setup["root"], db=db)
    sizing.sizing_job(t1 + timedelta(minutes=5), db=db)
    first = [dict(r) for r in sizing_rows(db, game)]
    t2 = t1 + timedelta(minutes=30)                                     # 讓分線移動 → 新 snapshot → 新定價 → 組合改變
    write_odds(db, game, S(SPREAD, home_line=-4.5, prices={"home": 1.91, "away": 1.91}), t2)
    pricing_job(t2 + timedelta(minutes=1), root=setup["root"], db=db)
    rep = sizing.sizing_job(t2 + timedelta(minutes=1), db=db)
    assert rep.rows_inserted == 6
    rows = sizing_rows(db, game)
    assert [dict(r) for r in rows[:6]] == first                          # 舊列不改寫
    assert len({r["portfolio_key"] for r in rows}) == 2
    assert {r["line"] for r in rows[6:] if r["market"] == "spread"} == {-4.5}


def test_dry_run_and_bankroll_never_write_and_bets_untouched(db, game, setup):
    t1 = TIP - timedelta(hours=10)
    prepare(db, game, setup, t1)
    pricing_job(t1 + timedelta(minutes=5), root=setup["root"], db=db)
    rep = sizing.sizing_job(t1 + timedelta(minutes=5), db=db, dry_run=True, bankroll=10000)
    assert rep.rows_inserted == 0 and sizing_rows(db) == []
    acts = [r for r in rep.results if r["actionable"]]
    assert acts and all(r["stake_amount"] == pytest.approx(10000 * r["final_stake_fraction"]) for r in acts)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM bets")
        assert cur.fetchone()["n"] == 0


def test_stale_quote_persisted_as_not_actionable(db, game, setup):
    t1 = TIP - timedelta(hours=10)
    prepare(db, game, setup, t1)
    pricing_job(t1 + timedelta(minutes=5), root=setup["root"], db=db)
    now = t1 + timedelta(minutes=90)                                    # 台彩 90 分鐘沒有再被輪詢確認 → stale
    sizing.sizing_job(now, db=db)
    rows = sizing_rows(db, game)
    tw = [r for r in rows if r["source"] == "twsport"]
    assert all(not r["actionable"] and r["final_stake_fraction"] == 0 for r in tw)
    assert any(r["qualification_status"] == "stale_quote" for r in tw)
    assert all(r["last_seen_age_seconds"] == 5400 and r["max_quote_age_seconds"] == 3600 for r in tw)
