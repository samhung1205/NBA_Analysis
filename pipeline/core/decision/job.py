"""
Decision board 物化工作（DB 讀寫；數學全部在 exposure / bankroll / board）
------------------------------------------------------------
    decision_board_job(now)          每分鐘（排程）：
        1. settle_actual_bets        有 canonical 市場身分（平台機會記錄）的 pending 實際注單 → settle-v1（只有 final 才結算）
        2. sync_settlement_ledger    bets 結算狀態 → bankroll_ledger 的 bet_settlement / reversal（append-only、冪等）
        3. materialize               每位使用者 × 每個 betting day（今日 / 明日 / 48 小時內有比賽的日期）：
                                     board.build_board → bankroll_day_snapshots（第一次需要時凍結）→ decision_snapshots
                                     （內容指紋相同 → 只推進 last_confirmed_at；不同 → 新列，舊列保留）
    Node API（Cloudflare Workers）不能跑 Python → 只讀這些表；使用者剛記錄的注單在下一次執行（≤ 1 分鐘）前，
    API 以 risk_state_version 不一致呈現 risk_recalculation_pending（不顯示舊額度為有效）。

讀取一致性：同一個 REPEATABLE READ 交易內先讀 bankroll_accounts.risk_state_version 再讀 bets / ledger；
Node 寫入注單與版本遞增是同一交易 → 物化結果的版本號永遠不會「新於」它看到的注單。
不寫入 paper_strategy_*、market_pricing_snapshots、bet_sizing_snapshots；不建立任何「真實下注」（只記錄使用者輸入的事實）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..execution import settlement as settle_v1
from ..execution.policy import EXECUTION_V1, PRIMARY_SCOPE, strategy_id
from ..pricing.job import HORIZON
from ..sizing import engine as sz
from ..timeutil import ensure_utc, now_utc, parse_utc, tpe_date, tpe_day_bounds_utc
from . import bankroll as bk
from . import board
from . import evidence as ev
from . import exposure as ex
from . import policy as P

log = logging.getLogger(__name__)

HEARTBEAT = dict(source_key="decision_board", display_name="決策中心物化（D.5 actual exposure / bankroll）",
                 category="model", expected_interval_min=1)
LOCK_KEY = "decision_board:materialize"

SNAPSHOT_COLUMNS = ("user_id", "account_id", "betting_day", "as_of", "last_confirmed_at", "decision_version",
                    "risk_policy_version", "execution_policy_version", "scope", "input_fingerprint",
                    "risk_state_version", "ledger_watermark", "bet_event_watermark", "bankroll_day_snapshot_id", "status",
                    "summary",
                    "bankroll", "actual_exposure", "risk_limits", "games", "evidence", "actual_performance",
                    "warnings")
SNAPSHOT_JSON = ("summary", "bankroll", "actual_exposure", "risk_limits", "games", "evidence", "actual_performance",
                 "warnings")
OPP_COLUMNS = (
    "snapshot_id", "user_id", "betting_day", "game_id", "scope_kind", "evidence_label", "source", "bookmaker",
    "market", "market_type", "period", "outcome_set", "side", "line", "display_line", "model_target",
    "model_threshold", "comparator", "settlement_rule", "decimal_odds", "odds_snapshot_id", "odds_fetched_at",
    "odds_last_seen_at", "pricing_snapshot_id", "sizing_snapshot_id", "prediction_id", "artifact_version",
    "pricing_version", "raw_implied_prob", "fair_no_vig_prob", "market_overround", "model_prob", "push_prob",
    "loss_prob", "edge_vs_fair", "ev_per_unit", "d3_qualification_status", "full_kelly_fraction",
    "fractional_kelly_fraction", "single_bet_capped_fraction", "theoretical_final_fraction", "actual_game_exposure",
    "remaining_game_fraction", "remaining_day_fraction", "game_scale_factor", "day_scale_factor",
    "user_adjusted_fraction", "day_start_bankroll", "max_additional_stake_amount", "suggested_stake_amount",
    "linked_bet_ids", "linked_actual_fraction", "paper_decision_id", "decision_status", "status_group",
    "display_rank", "reasons", "warnings")
OPP_JSON = ("linked_bet_ids", "reasons", "warnings")
RESULT_OF = {settle_v1.SETTLED_WIN: "win", settle_v1.SETTLED_DRAW_WIN: "win", settle_v1.SETTLED_LOSS: "lose",
             settle_v1.SETTLED_PUSH: "push", settle_v1.VOID: "void"}


@dataclass
class DecisionReport:
    as_of: str
    dry_run: bool
    settled: int = 0
    ledger_entries: int = 0
    snapshots_new: int = 0
    snapshots_confirmed: int = 0
    day_snapshots_new: int = 0
    bets_filled: int = 0
    days: list[str] = field(default_factory=list)
    users: int = 0
    boards: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None


def schema_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('decision_snapshots') IS NOT NULL AS d, to_regclass('bankroll_ledger') IS NOT NULL AS l,"
                " to_regclass('bet_events') IS NOT NULL AS e, to_regclass('bet_sizing_snapshots') IS NOT NULL AS s")
    r = cur.fetchone()
    return bool(r["d"] and r["l"] and r["e"] and r["s"])


def paper_ready(cur) -> bool:
    cur.execute("SELECT to_regclass('paper_strategy_decisions') IS NOT NULL AS d")
    return bool(cur.fetchone()["d"])


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, allow_nan=False, default=str)


# ------------------------------------------------------------------ #
# 1. 實際注單結算（settle-v1；只處理有 canonical 身分的 pending 注單）       #
# ------------------------------------------------------------------ #

def settle_actual_bets(cur, now: datetime, *, dry_run: bool = False) -> int:
    cur.execute("""SELECT b.*, g.status AS g_status, g.date_utc AS g_date_utc, g.home_pts, g.away_pts,
                          g.home_q1, g.home_q2, g.home_q3, g.home_q4, g.home_ot, g.away_q1, g.away_q2, g.away_q3,
                          g.away_q4, g.away_ot, g.home_h1, g.away_h1
                     FROM bets b JOIN games g ON g.id = b.game_id
                    WHERE b.record_status = 'active' AND COALESCE(b.result, 'pending') = 'pending'
                      AND b.model_target IS NOT NULL AND b.model_threshold IS NOT NULL AND b.comparator IS NOT NULL
                      AND b.settlement_rule IS NOT NULL
                    ORDER BY b.id""")
    n = 0
    for b in cur.fetchall():
        game = {k[2:] if k.startswith("g_") else k: b[k] for k in b if k.startswith("g_") or k.startswith(("home_", "away_"))}
        spec = {"settlement_rule": b["settlement_rule"], "market_type": b["market_type"], "period": b["period"],
                "outcome_set": b["outcome_set"], "model_threshold": b["model_threshold"],
                "model_target": b["model_target"], "comparator": b["comparator"], "stake_units": float(b["stake"]),
                "decimal_odds": float(b["odds"])}
        s = settle_v1.settle(spec, game)
        if s.status in RESULT_OF:
            result = RESULT_OF[s.status]
            payout = float(b["stake"]) + s.profit_units           # legacy 欄位：返還總額（本金 + 損益）
            if not dry_run:
                cur.execute("""UPDATE bets SET result = %s, payout = %s, settled_at = %s, settlement_source = 'settle-v1',
                                 settlement_reason = %s
                                WHERE id = %s AND record_status = 'active' AND COALESCE(result, 'pending') = 'pending'""",
                            (result, payout, now, s.reason or s.status, b["id"]))
                if cur.rowcount == 1:
                    cur.execute("INSERT INTO bet_events (bet_id, user_id, event_type, actor, payload, created_at) "
                                "VALUES (%s, %s, 'settled_auto', 'pipeline', %s::jsonb, %s)",
                                (b["id"], b["user_id"], _j({"settlement_status": s.status, "result": result,
                                                            "actual_value": s.actual_value,
                                                            "settlement_version": s.settlement_version}), now))
            n += 1
        elif s.status == settle_v1.UNGRADABLE and s.reason != b.get("settlement_reason") and not dry_run:
            cur.execute("UPDATE bets SET settlement_reason = %s WHERE id = %s AND record_status = 'active'",
                        (f"ungradable:{s.reason}", b["id"]))
    return n


# ------------------------------------------------------------------ #
# 2. ledger 損益同步                                                     #
# ------------------------------------------------------------------ #

def _load_user_state(cur, user_id: int) -> tuple[dict | None, list[bk.LedgerEntry], list[ex.ActualBet]]:
    """先讀版本（account / event watermark）再讀 bets / ledger：物化結果的版本號永遠不會新於它看到的資料。"""
    cur.execute("SELECT * FROM bankroll_accounts WHERE user_id = %s", (user_id,))
    account = cur.fetchone()
    entries = []
    if account is not None:
        cur.execute("SELECT * FROM bankroll_ledger WHERE account_id = %s ORDER BY id", (account["id"],))
        entries = [bk.entry_from_row(r) for r in cur.fetchall()]
    cur.execute("""SELECT b.*, g.date_utc AS game_date_utc FROM bets b LEFT JOIN games g ON g.id = b.game_id
                    WHERE b.user_id = %s ORDER BY b.id""", (user_id,))
    bets = [ex.bet_from_row(r, r.get("game_date_utc")) for r in cur.fetchall()]
    return account, entries, bets


def sync_settlement_ledger(cur, now: datetime, *, dry_run: bool = False) -> int:
    cur.execute("SELECT id, user_id FROM bankroll_accounts ORDER BY id")
    n = 0
    for acc in cur.fetchall():
        account, entries, bets = _load_user_state(cur, acc["user_id"])
        for a in bk.settlement_ledger_actions(bets, entries):
            n += 1
            if dry_run:
                continue
            cur.execute("""INSERT INTO bankroll_ledger (account_id, entry_type, amount, bet_id, settlement_key, reason,
                                                        recorded_by, recorded_at)
                           VALUES (%s, %s, %s, %s, %s, %s, 'pipeline', %s) ON CONFLICT DO NOTHING""",
                        (account["id"], a["entry_type"], a["amount"], a["bet_id"], a["settlement_key"], a["reason"], now))
    return n


# ------------------------------------------------------------------ #
# 3. materialize                                                       #
# ------------------------------------------------------------------ #

def target_days(cur, now: datetime, horizon: timedelta = HORIZON) -> list[date]:
    today = tpe_date(now)
    days = {today, today + timedelta(days=1)}
    cur.execute("SELECT date_utc FROM games WHERE date_utc > %s AND date_utc <= %s AND status <> 'final'",
                (now - timedelta(hours=6), now + horizon))
    days |= {sz.betting_day(ensure_utc(r["date_utc"])) for r in cur.fetchall()}
    return sorted(days)


def load_day_inputs(cur, day: date, now: datetime) -> dict[str, Any]:
    start, end = tpe_day_bounds_utc(day)
    cur.execute("SELECT id, date_utc, status FROM games WHERE date_utc >= %s AND date_utc < %s ORDER BY date_utc, id",
                (start, end))
    games = cur.fetchall()
    ids = [g["id"] for g in games]
    if not ids:
        return {"games": [], "odds": [], "preds": [], "pricing": [], "sizing": []}
    cur.execute("SELECT * FROM odds_snapshots WHERE game_id = ANY(%s) AND fetched_at <= %s ORDER BY id", (ids, now))
    odds = cur.fetchall()
    cur.execute("""SELECT id, game_id, model_version, created_at, home_win_prob, pred_margin, pred_total,
                          pred_home_h1, pred_away_h1, features_json
                     FROM predictions WHERE game_id = ANY(%s) AND created_at <= %s ORDER BY id""", (ids, now))
    preds = cur.fetchall()
    cur.execute("SELECT * FROM market_pricing_snapshots WHERE game_id = ANY(%s) AND analysis_as_of <= %s ORDER BY id",
                (ids, now))
    pricing = cur.fetchall()
    cur.execute("""SELECT id, market_pricing_snapshot_id, analysis_as_of FROM bet_sizing_snapshots
                    WHERE game_id = ANY(%s) AND risk_policy_version = %s AND analysis_as_of <= %s""",
                (ids, P.RISK_POLICY.version, now))
    return {"games": games, "odds": odds, "preds": preds, "pricing": pricing, "sizing": cur.fetchall()}


def load_paper(cur, day: date) -> dict[int, dict] | None:
    if not paper_ready(cur):
        return None
    sid = strategy_id(EXECUTION_V1, P.RISK_POLICY, PRIMARY_SCOPE)
    cur.execute("""SELECT id, game_id, decision_status, no_bet_reason, decision_time FROM paper_strategy_decisions
                    WHERE strategy_id = %s AND betting_day = %s""", (sid, day))
    out = {r["game_id"]: {"decision_id": r["id"], "decision_status": r["decision_status"],
                          "no_bet_reason": r["no_bet_reason"], "decision_time": ensure_utc(r["decision_time"]).isoformat(),
                          "wagers": []} for r in cur.fetchall()}
    cur.execute("""SELECT decision_id, game_id, market, side, display_line, decimal_odds, stake_fraction, settlement_status
                     FROM paper_strategy_bets WHERE strategy_id = %s AND betting_day = %s ORDER BY id""", (sid, day))
    for w in cur.fetchall():
        if w["game_id"] in out:
            out[w["game_id"]]["wagers"].append({k: w[k] for k in ("market", "side", "display_line", "decimal_odds",
                                                                   "stake_fraction", "settlement_status")})
    return out


def load_evidence(cur) -> dict[str, Any]:
    from ..execution.ledger import load_ledger, schema_ready as paper_schema_ready

    sid = strategy_id(EXECUTION_V1, P.RISK_POLICY, PRIMARY_SCOPE)
    decisions = load_ledger(cur, sid) if paper_schema_ready(cur) else None
    return ev.evidence_panel(ev.paper_evidence(decisions, strategy_id=sid))


def _day_snapshot(cur, account_id: int | None, day: date) -> dict | None:
    if account_id is None:
        return None
    cur.execute("SELECT * FROM bankroll_day_snapshots WHERE account_id = %s AND betting_day = %s", (account_id, day))
    return cur.fetchone()


def persist_board(cur, res: board.BoardResult, now: datetime) -> tuple[bool, int, int]:
    """→（是否新 snapshot, 新 day snapshot 數, 補填的 bets 數）。"""
    snap = dict(res.snapshot)
    new_days = filled = 0
    day_snap_id = None
    acc_id = snap.get("account_id")
    if acc_id is not None:
        existing = _day_snapshot(cur, acc_id, snap["betting_day"])
        if existing is None and res.new_day_start is not None:
            d = res.new_day_start.to_row()
            cur.execute("""INSERT INTO bankroll_day_snapshots (account_id, betting_day, day_start_bankroll, basis_as_of,
                             ledger_balance, open_stake_excluded, ledger_watermark, established_reason, bankroll_version)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                        (acc_id, d["betting_day"], d["day_start_bankroll"], d["basis_as_of"], d["ledger_balance"],
                         d["open_stake_excluded"], d["ledger_watermark"], d["established_reason"],
                         d["bankroll_version"]))
            r = cur.fetchone()
            if r is not None:
                new_days, day_snap_id = 1, r["id"]
        elif existing is not None:
            day_snap_id = existing["id"]
    if day_snap_id is not None:
        for bet_id, frac in sorted(res.bet_fractions.items()):
            cur.execute("""UPDATE bets SET bankroll_day_snapshot_id = COALESCE(bankroll_day_snapshot_id, %s),
                                           stake_fraction_at_placement = COALESCE(stake_fraction_at_placement, %s)
                            WHERE id = %s AND (bankroll_day_snapshot_id IS NULL OR stake_fraction_at_placement IS NULL)
                              AND (bankroll_day_snapshot_id IS NULL OR bankroll_day_snapshot_id = %s)""",
                        (day_snap_id, frac, bet_id, day_snap_id))
            filled += cur.rowcount
    snap.update(as_of=now, last_confirmed_at=now, input_fingerprint=res.fingerprint,
                bankroll_day_snapshot_id=day_snap_id)
    cols = ", ".join(SNAPSHOT_COLUMNS)
    ph = ", ".join("%s::jsonb" if c in SNAPSHOT_JSON else "%s" for c in SNAPSHOT_COLUMNS)
    cur.execute(f"INSERT INTO decision_snapshots ({cols}) VALUES ({ph}) "
                "ON CONFLICT (user_id, betting_day, input_fingerprint) DO NOTHING RETURNING id",
                tuple(_j(snap[c]) if c in SNAPSHOT_JSON else snap[c] for c in SNAPSHOT_COLUMNS))
    r = cur.fetchone()
    if r is None:
        cur.execute("""UPDATE decision_snapshots SET last_confirmed_at = GREATEST(last_confirmed_at, %s)
                        WHERE user_id = %s AND betting_day = %s AND input_fingerprint = %s""",
                    (now, snap["user_id"], snap["betting_day"], res.fingerprint))
        return False, new_days, filled
    sid = r["id"]
    ocols = ", ".join(OPP_COLUMNS)
    oph = ", ".join("%s::jsonb" if c in OPP_JSON else "%s" for c in OPP_COLUMNS)
    for o in res.opportunities:
        row = {**o, "snapshot_id": sid, "user_id": snap["user_id"], "betting_day": snap["betting_day"]}
        cur.execute(f"INSERT INTO decision_opportunities ({ocols}) VALUES ({oph})",
                    tuple(_j(row.get(c)) if c in OPP_JSON else row.get(c) for c in OPP_COLUMNS))
    return True, new_days, filled


def materialize(cur, now: datetime, *, dry_run: bool = False, user_ids: list[int] | None = None,
                days: list[date] | None = None, rep: DecisionReport | None = None) -> DecisionReport:
    rep = rep or DecisionReport(now.isoformat(), dry_run)
    days = days or target_days(cur, now)
    rep.days = [d.isoformat() for d in days]
    cur.execute("SELECT id FROM users ORDER BY id")
    users = [r["id"] for r in cur.fetchall() if user_ids is None or r["id"] in user_ids]
    rep.users = len(users)
    cur.execute("SELECT * FROM data_sources WHERE source_key = 'twsport'")
    tw_source = cur.fetchone()
    evidence = load_evidence(cur)
    day_inputs = {d: load_day_inputs(cur, d, now) for d in days}
    paper = {d: load_paper(cur, d) for d in days}
    for uid in users:
        cur.execute("SELECT COALESCE(MAX(id), 0) AS w FROM bet_events WHERE user_id = %s AND actor = 'user'", (uid,))
        watermark = int(cur.fetchone()["w"])
        account, entries, bets = _load_user_state(cur, uid)       # 先讀版本（account）再讀 bets / ledger
        for d in days:
            di = day_inputs[d]
            inp = board.BoardInputs(now=now, betting_day=d, user_id=uid, games=di["games"], odds_rows=di["odds"],
                                    pred_rows=di["preds"], pricing_rows=di["pricing"], sizing_rows=di["sizing"],
                                    bets=bets, account=account, ledger=entries,
                                    day_snapshot=_day_snapshot(cur, (account or {}).get("id"), d),
                                    bet_event_watermark=watermark,
                                    twsport_source=tw_source, paper=paper[d], evidence=evidence)
            res = board.build_board(inp)
            if dry_run:
                rep.boards.append({"snapshot": res.snapshot, "opportunities": res.opportunities,
                                   "fingerprint": res.fingerprint,
                                   "new_day_start": res.new_day_start.to_row() if res.new_day_start else None})
                continue
            new, nd, nf = persist_board(cur, res, now)
            rep.snapshots_new += int(new)
            rep.snapshots_confirmed += int(not new)
            rep.day_snapshots_new += nd
            rep.bets_filled += nf
    return rep


def decision_board_job(now: datetime | None = None, *, dry_run: bool = False, user_ids: list[int] | None = None,
                       days: list[date] | None = None, db=None) -> DecisionReport:
    from .. import db as default_db

    db = db or default_db
    now = ensure_utc(now or now_utc())
    rep = DecisionReport(now.isoformat(), dry_run)
    with db.cursor() as cur:
        if not schema_ready(cur):
            rep.note = "資料庫尚未套用 migrations/postgres/0008_phase_d5.sql（或 0006），不物化 decision board"
            if not dry_run:
                db.heartbeat(cur, status="warn", error=rep.note, **HEARTBEAT)
            return rep
    if dry_run:
        with db.cursor() as cur:
            rep.settled = settle_actual_bets(cur, now, dry_run=True)
            rep.ledger_entries = sync_settlement_ledger(cur, now, dry_run=True)
            return materialize(cur, now, dry_run=True, user_ids=user_ids, days=days, rep=rep)
    with db.transaction() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok", (LOCK_KEY,))
        if not cur.fetchone()["ok"]:
            rep.note = "另一個 decision board 物化正在執行，略過"
            return rep
        rep.settled = settle_actual_bets(cur, now)
        rep.ledger_entries = sync_settlement_ledger(cur, now)
    with db.transaction() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok", (LOCK_KEY,))
        if not cur.fetchone()["ok"]:
            rep.note = "另一個 decision board 物化正在執行，略過"
            return rep
        materialize(cur, now, user_ids=user_ids, days=days, rep=rep)
    return rep


def decision_board_scheduled() -> None:
    """排程進入點（每分鐘）：結算 → ledger → 物化；心跳 data_sources.decision_board。"""
    from .. import db

    status, err, n = "ok", None, 0
    try:
        rep = decision_board_job()
        n = rep.snapshots_new
        if rep.note and "0008" in rep.note:
            status, err = "warn", rep.note
    except Exception as e:  # noqa: BLE001
        log.exception("decision board job 失敗")
        status, err = "error", f"{type(e).__name__}: {e}"[:500]
    with db.cursor() as cur:
        db.heartbeat(cur, status=status, error=err, records_updated=n, **HEARTBEAT)
