"""Phase D.5（暫存 schema）：migration 0008 / bets 不可變與更正 / ledger / day-start 凍結 / 併發 / 物化 job 端到端。

Node API 的寫入語句（risk_state_claims 認領 + INSERT … VALUES）在這裡以 psycopg 原樣執行，驗證 DB 層的保證；
Node 端的完整流程另由 scripts/smoke-test.mjs（本機 D1）驗證。
"""
from __future__ import annotations

import threading
from datetime import timedelta

import psycopg
import pytest

from core.decision import job as dj
from core.decision import policy as P
from core.pricing.job import pricing_job
from core.sizing import job as sizing
from test_pricing_artifact import TIP, game, league, setup  # noqa: F401
from test_sizing_db import prepare

T1 = TIP - timedelta(hours=10)
NOW = T1 + timedelta(minutes=5)


def user(db, email="d5@example.com"):
    with db.cursor() as cur:
        cur.execute("""INSERT INTO users (email, password_hash) VALUES (%s, 'x')
                       ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id""", (email,))
        return cur.fetchone()["id"]


def account(db, uid, amount=10000.0, at=None):
    at = at or T1 - timedelta(days=2)
    with db.cursor() as cur:
        cur.execute("""INSERT INTO bankroll_accounts (user_id, currency, created_at, risk_state_version)
                       VALUES (%s, 'TWD', %s, 1) RETURNING id""", (uid, at))
        aid = cur.fetchone()["id"]
        cur.execute("INSERT INTO risk_state_claims (account_id, version, kind) VALUES (%s, 1, 'ledger_entry')", (aid,))
        cur.execute("""INSERT INTO bankroll_ledger (account_id, entry_type, amount, recorded_by, recorded_at)
                       VALUES (%s, 'initial_funding', %s, 'user', %s)""", (aid, amount, at))
        return aid


def record_bet(cur, uid, gid, *, stake, odds=1.9, market="ml", side="home", aid=None, expected=None, req="r1",
               opp=None, recorded=None, compliance="manual_unlinked", origin="manual_unlinked", day="2026-10-22",
               ref_odds=None):
    """與 Node POST /api/bets 相同的語句（claim → version → bet → event），在呼叫端的交易內執行。"""
    recorded = recorded or NOW
    if aid is not None:
        cur.execute("INSERT INTO risk_state_claims (account_id, version, kind, request_id) VALUES (%s, %s, 'bet_recorded', %s)",
                    (aid, expected + 1, req))
        cur.execute("UPDATE bankroll_accounts SET risk_state_version = %s WHERE id = %s AND risk_state_version = %s",
                    (expected + 1, aid, expected))
    o = opp or {}
    cur.execute("""INSERT INTO bets (user_id, game_id, market, selection, line, odds, stake, result, placed_at, account_id,
                     betting_day, source, bookmaker, market_type, period, outcome_set, model_target, model_threshold,
                     comparator, settlement_rule, origin, strategy_compliance, reference_odds_snapshot_id,
                     reference_decimal_odds, decision_opportunity_id, client_request_id, recorded_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,'twsport','twsport',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (uid, gid, market, side, o.get("display_line"), odds, stake, recorded, aid, day, o.get("market_type"),
                 o.get("period"), o.get("outcome_set"), o.get("model_target"), o.get("model_threshold"),
                 o.get("comparator"), o.get("settlement_rule"), origin, compliance, o.get("odds_snapshot_id"),
                 ref_odds if ref_odds is not None else o.get("decimal_odds"), o.get("id"), req, recorded))
    bid = cur.fetchone()["id"]
    cur.execute("INSERT INTO bet_events (bet_id, user_id, event_type, actor, payload) VALUES (%s, %s, 'recorded', 'user', '{}')",
                (bid, uid))
    return bid


def acct_version(db, aid):
    with db.cursor() as cur:
        cur.execute("SELECT risk_state_version FROM bankroll_accounts WHERE id = %s", (aid,))
        return cur.fetchone()["risk_state_version"]


def snapshots(db, uid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM decision_snapshots WHERE user_id = %s ORDER BY id", (uid,))
        return cur.fetchall()


def opps(db, sid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM decision_opportunities WHERE snapshot_id = %s ORDER BY display_rank", (sid,))
        return cur.fetchall()


def run(db, now=NOW, uid=None):
    return dj.decision_board_job(now, db=db, user_ids=[uid] if uid else None)


# ======================= migration / backward compatibility ======================= #

def test_legacy_bet_insert_still_works_and_gets_defaults(db, game):
    uid = user(db)
    with db.cursor() as cur:
        cur.execute("""INSERT INTO bets (user_id, game_id, market, selection, line, odds, stake, result, note)
                       VALUES (%s, %s, 'spread', 'home', -1.5, 1.87, 1000, 'pending', 'legacy') RETURNING *""", (uid, game))
        b = cur.fetchone()
    assert b["record_status"] == "active" and b["origin"] is None and b["recorded_at"] is None
    assert b["strategy_compliance"] is None and b["placed_at"] is not None


# ======================= immutability / correction ======================= #

def test_bet_execution_fields_immutable_and_never_deleted(db, game):
    uid = user(db)
    with db.transaction() as cur:
        bid = record_bet(cur, uid, game, stake=100.0)
    for sql in ("UPDATE bets SET stake = 999 WHERE id = %s", "UPDATE bets SET odds = 3.0 WHERE id = %s",
                "UPDATE bets SET selection = 'away' WHERE id = %s", "UPDATE bets SET placed_at = NOW() WHERE id = %s",
                "UPDATE bets SET game_id = game_id + 0, market = 'total' WHERE id = %s", "DELETE FROM bets WHERE id = %s"):
        with pytest.raises(psycopg.errors.RaiseException):
            with db.cursor() as cur:
                cur.execute(sql, (bid,))
    with db.cursor() as cur:                                   # 結算 / 補 bankroll basis 允許
        cur.execute("UPDATE bets SET result = 'win', payout = 190, settlement_source = 'manual' WHERE id = %s", (bid,))
        cur.execute("UPDATE bets SET stake_fraction_at_placement = 0.01 WHERE id = %s", (bid,))
    with pytest.raises(psycopg.errors.RaiseException):         # 只能補一次
        with db.cursor() as cur:
            cur.execute("UPDATE bets SET stake_fraction_at_placement = 0.02 WHERE id = %s", (bid,))
    with db.cursor() as cur:
        cur.execute("UPDATE bets SET record_status = 'voided', voided_at = NOW(), void_reason = 'typo' WHERE id = %s", (bid,))
    for sql in ("UPDATE bets SET record_status = 'active' WHERE id = %s", "UPDATE bets SET result = 'lose' WHERE id = %s"):
        with pytest.raises(psycopg.errors.RaiseException):
            with db.cursor() as cur:
                cur.execute(sql, (bid,))


def test_correction_supersedes_without_overwriting_history(db, game):
    uid = user(db)
    with db.transaction() as cur:
        old = record_bet(cur, uid, game, stake=1000.0, req="orig")
    with db.transaction() as cur:                              # 與 Node POST /bets/:id/correction 相同的語句
        cur.execute("""INSERT INTO bets (user_id, game_id, market, selection, odds, stake, result, origin,
                         strategy_compliance, client_request_id, recorded_at, supersedes_bet_id, betting_day)
                       VALUES (%s, %s, 'ml', 'home', 1.9, 100, 'pending', 'manual_unlinked', 'manual_unlinked', 'fix1',
                               NOW(), %s, '2026-10-22')""", (uid, game, old))
        cur.execute("""UPDATE bets SET record_status = 'superseded', voided_at = NOW(), void_reason = 'corrected:typo',
                         superseded_by_bet_id = (SELECT id FROM bets WHERE user_id = %s AND client_request_id = 'fix1')
                       WHERE id = %s AND record_status = 'active'""", (uid, old))
    with db.cursor() as cur:
        cur.execute("SELECT id, stake, record_status, supersedes_bet_id, superseded_by_bet_id FROM bets ORDER BY id")
        a, b = cur.fetchall()
    assert (a["stake"], a["record_status"]) == (1000.0, "superseded") and a["superseded_by_bet_id"] == b["id"]
    assert (b["stake"], b["record_status"], b["supersedes_bet_id"]) == (100.0, "active", a["id"])


def test_audit_tables_are_append_only(db, game):
    uid = user(db)
    aid = account(db, uid)
    with db.transaction() as cur:
        record_bet(cur, uid, game, stake=10.0, aid=aid, expected=1)
    for sql in ("UPDATE bankroll_ledger SET amount = 1", "DELETE FROM bankroll_ledger", "UPDATE bet_events SET actor = 'x'",
                "DELETE FROM bet_events", "DELETE FROM risk_state_claims", "DELETE FROM bankroll_accounts",
                "UPDATE bankroll_accounts SET risk_state_version = 0"):
        with pytest.raises(psycopg.errors.RaiseException):
            with db.cursor() as cur:
                cur.execute(sql)
    with pytest.raises(psycopg.errors.CheckViolation):          # 負數提出 / 無原因的調整不被接受
        with db.cursor() as cur:
            cur.execute("INSERT INTO bankroll_ledger (account_id, entry_type, amount, recorded_by, recorded_at) "
                        "VALUES (%s, 'withdrawal', -5, 'user', NOW())", (aid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute("INSERT INTO bankroll_ledger (account_id, entry_type, amount, recorded_by, recorded_at) "
                        "VALUES (%s, 'adjustment', -5, 'user', NOW())", (aid,))
    with pytest.raises(psycopg.errors.UniqueViolation):          # initial_funding 只能一次
        with db.cursor() as cur:
            cur.execute("INSERT INTO bankroll_ledger (account_id, entry_type, amount, recorded_by, recorded_at) "
                        "VALUES (%s, 'initial_funding', 5, 'user', NOW())", (aid,))


# ======================= concurrency / idempotency ======================= #

def test_two_simultaneous_submissions_cannot_both_use_same_capacity(db, game):
    """兩個分頁讀到同一個 risk_state_version 同時送出：claim 的 PK 讓第二個交易整個失敗（不會兩筆都寫入）。"""
    uid = user(db)
    aid = account(db, uid)
    barrier, results = threading.Barrier(2), {}

    def submit(tag):
        try:
            with db.transaction() as cur:
                barrier.wait(timeout=10)
                record_bet(cur, uid, game, stake=200.0, aid=aid, expected=1, req=f"tab-{tag}")
            results[tag] = "ok"
        except psycopg.errors.UniqueViolation:
            results[tag] = "conflict"

    ts = [threading.Thread(target=submit, args=(t,)) for t in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results.values()) == ["conflict", "ok"]
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM bets WHERE user_id = %s", (uid,))
        assert cur.fetchone()["n"] == 1
    assert acct_version(db, aid) == 2


def test_repeated_request_id_is_not_duplicated(db, game):
    uid = user(db)
    with db.transaction() as cur:
        record_bet(cur, uid, game, stake=50.0, req="same-key-123")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction() as cur:
            record_bet(cur, uid, game, stake=50.0, req="same-key-123")


# ======================= materialization end-to-end ======================= #

def _ready(db, game, setup):
    prepare(db, game, setup, T1)
    assert pricing_job(NOW, root=setup["root"], db=db).rows_inserted == 6
    assert sizing.sizing_job(NOW, db=db).rows_inserted == 6


def test_decision_job_materializes_freezes_day_start_and_is_idempotent(db, game, setup):
    _ready(db, game, setup)
    uid = user(db)
    aid = account(db, uid)
    rep = run(db, uid=uid)
    assert rep.snapshots_new >= 1 and rep.day_snapshots_new == 1
    [s] = [x for x in snapshots(db, uid) if x["betting_day"].isoformat() == "2026-10-22"]
    assert s["status"] == "ok" and s["risk_state_version"] == 1 and s["ledger_watermark"] >= 1
    assert s["risk_policy_version"] == "risk-v1" and s["decision_version"] == P.DECISION_VERSION
    os_ = opps(db, s["id"])
    tw = [o for o in os_ if o["scope_kind"] == "taiwan_primary"]
    intl = [o for o in os_ if o["scope_kind"] == "international_diagnostic"]
    assert tw and intl and all(o["max_additional_stake_amount"] is None for o in intl)
    q = [o for o in tw if o["decision_status"] == "qualified"]
    assert q and all(o["max_additional_stake_amount"] == pytest.approx(o["user_adjusted_fraction"] * 10000.0) for o in q)
    assert all(o["sizing_snapshot_id"] is not None for o in tw if o["pricing_snapshot_id"] is not None)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM bankroll_day_snapshots WHERE account_id = %s", (aid,))
        [d] = cur.fetchall()
    assert d["day_start_bankroll"] == 10000.0 and d["established_reason"] == "first_qualified_opportunity"
    # 同樣輸入 → 不新增，只推進 last_confirmed_at
    rep2 = run(db, NOW + timedelta(minutes=1), uid=uid)
    assert rep2.snapshots_new == 0 and rep2.snapshots_confirmed >= 1
    [s2] = [x for x in snapshots(db, uid) if x["betting_day"].isoformat() == "2026-10-22"]
    assert s2["id"] == s["id"] and s2["last_confirmed_at"] > s["last_confirmed_at"]
    with pytest.raises(psycopg.errors.RaiseException):
        with db.cursor() as cur:
            cur.execute("UPDATE decision_snapshots SET status = 'x' WHERE id = %s", (s["id"],))
    with pytest.raises(psycopg.errors.RaiseException):
        with db.cursor() as cur:
            cur.execute("UPDATE bankroll_day_snapshots SET day_start_bankroll = 1")


def test_recorded_bet_creates_new_snapshot_already_recorded_and_fills_basis(db, game, setup):
    _ready(db, game, setup)
    uid = user(db)
    aid = account(db, uid)
    run(db, uid=uid)
    [s1] = [x for x in snapshots(db, uid) if x["betting_day"].isoformat() == "2026-10-22"]
    o = next(o for o in opps(db, s1["id"]) if o["decision_status"] == "qualified")
    t = NOW + timedelta(minutes=2)
    with db.transaction() as cur:
        bid = record_bet(cur, uid, game, stake=50.0, odds=o["decimal_odds"] - 0.05, market=o["market"], side=o["side"],
                         aid=aid, expected=1, opp=dict(o), compliance="compliant", origin="platform_opportunity",
                         recorded=t)
    run(db, t + timedelta(minutes=1), uid=uid)
    snaps = [x for x in snapshots(db, uid) if x["betting_day"].isoformat() == "2026-10-22"]
    assert len(snaps) == 2 and snaps[1]["risk_state_version"] == 2                  # 舊 snapshot 保留
    o2 = next(x for x in opps(db, snaps[1]["id"]) if x["market"] == o["market"] and x["side"] == o["side"]
              and x["scope_kind"] == "taiwan_primary")
    assert o2["decision_status"] == "already_recorded" and o2["user_adjusted_fraction"] == 0.0
    assert o2["linked_bet_ids"] == [bid]
    exp = snaps[1]["actual_exposure"]
    assert exp["day_stake"] == 50.0 and exp["day_fraction"] == pytest.approx(0.005)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM bets WHERE id = %s", (bid,))
        b = cur.fetchone()
    assert b["stake_fraction_at_placement"] == pytest.approx(0.005) and b["bankroll_day_snapshot_id"] is not None
    assert b["reference_decimal_odds"] == pytest.approx(o["decimal_odds"]) and b["odds"] == pytest.approx(o["decimal_odds"] - 0.05)
    assert b["reference_odds_snapshot_id"] == o["odds_snapshot_id"]                 # 參考快照保留（實際賠率另存）
    with pytest.raises(psycopg.errors.ForeignKeyViolation):                       # 稽核參照：快照不能被刪
        with db.cursor() as cur:
            cur.execute("DELETE FROM odds_snapshots WHERE id = %s", (o["odds_snapshot_id"],))


def test_actual_bet_settlement_flows_to_ledger_append_only(db, game, setup):
    _ready(db, game, setup)
    uid = user(db)
    aid = account(db, uid)
    run(db, uid=uid)
    [s1] = [x for x in snapshots(db, uid) if x["betting_day"].isoformat() == "2026-10-22"]
    o = next(o for o in opps(db, s1["id"]) if o["decision_status"] == "qualified" and o["market"] == "ml")
    with db.transaction() as cur:
        bid = record_bet(cur, uid, game, stake=100.0, odds=2.0, market="ml", side=o["side"], aid=aid, expected=1,
                         opp=dict(o), compliance="compliant", origin="platform_opportunity")
    with db.cursor() as cur:                                     # 比賽結束（主隊勝 110–100）
        cur.execute("""UPDATE games SET status = 'final', home_pts = 110, away_pts = 100, home_q1 = 25, home_q2 = 25,
                         home_q3 = 30, home_q4 = 30, away_q1 = 25, away_q2 = 25, away_q3 = 25, away_q4 = 25,
                         home_h1 = 50, away_h1 = 50 WHERE id = %s""", (game,))
    later = TIP + timedelta(hours=3)
    rep = run(db, later, uid=uid)
    assert rep.settled == 1 and rep.ledger_entries == 1
    with db.cursor() as cur:
        cur.execute("SELECT result, payout, settlement_source FROM bets WHERE id = %s", (bid,))
        b = cur.fetchone()
        cur.execute("SELECT entry_type, amount FROM bankroll_ledger WHERE bet_id = %s ORDER BY id", (bid,))
        entries = cur.fetchall()
    won = o["side"] == "home"
    assert b["result"] == ("win" if won else "lose") and b["settlement_source"] == "settle-v1"
    assert [(e["entry_type"], e["amount"]) for e in entries] == [("bet_settlement", 100.0 if won else -100.0)]
    assert run(db, later + timedelta(minutes=1), uid=uid).ledger_entries == 0      # 冪等
    with db.cursor() as cur:                                     # 使用者手動改結算 → 沖銷 + 新入帳（不覆寫）
        cur.execute("UPDATE bets SET result = 'push', settlement_source = 'manual' WHERE id = %s", (bid,))
    run(db, later + timedelta(minutes=2), uid=uid)
    with db.cursor() as cur:
        cur.execute("SELECT entry_type, amount FROM bankroll_ledger WHERE bet_id = %s ORDER BY id", (bid,))
        e2 = [(e["entry_type"], e["amount"]) for e in cur.fetchall()]
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM bankroll_ledger WHERE account_id = %s", (aid,))
        total = cur.fetchone()["s"]
    assert e2[1:] == [("bet_settlement_reversal", -(100.0 if won else -100.0)), ("bet_settlement", 0.0)]
    assert total == pytest.approx(10000.0)                       # audit：initial + push（0）
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM paper_strategy_decisions")
        assert cur.fetchone()["n"] == 0                          # actual bet 不寫入 paper ledger


def test_day_start_frozen_mid_day_and_dry_run_writes_nothing(db, game, setup):
    _ready(db, game, setup)
    uid = user(db)
    aid = account(db, uid)
    dry = dj.decision_board_job(NOW, db=db, user_ids=[uid], dry_run=True)
    assert dry.boards and not snapshots(db, uid)
    run(db, uid=uid)
    with db.cursor() as cur:                                     # 當日中途存入 → day-start 不變
        cur.execute("INSERT INTO risk_state_claims (account_id, version, kind) VALUES (%s, 2, 'ledger_entry')", (aid,))
        cur.execute("UPDATE bankroll_accounts SET risk_state_version = 2 WHERE id = %s", (aid,))
        cur.execute("""INSERT INTO bankroll_ledger (account_id, entry_type, amount, recorded_by, recorded_at)
                       VALUES (%s, 'deposit', 5000, 'user', %s)""", (aid, NOW + timedelta(minutes=1)))
    run(db, NOW + timedelta(minutes=2), uid=uid)
    snaps = [x for x in snapshots(db, uid) if x["betting_day"].isoformat() == "2026-10-22"]
    assert len(snaps) == 2
    assert snaps[1]["bankroll"]["day_start_bankroll"] == 10000.0 and snaps[1]["bankroll"]["current_bankroll"] == 15000.0
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM bankroll_day_snapshots WHERE account_id = %s AND betting_day = '2026-10-22'",
                    (aid,))
        assert cur.fetchone()["n"] == 1
