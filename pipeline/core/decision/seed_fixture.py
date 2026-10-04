"""
本機 D1 / smoke test 用的 D.5 seed（不碰任何資料庫；只輸出 SQL）
------------------------------------------------------------
    python -m core.decision.seed_fixture        # 輸出 SQL，貼回 seed/seed.template.sql 的 D.5 GENERATED 區塊

demo 帳號（demo@example.com）：
    bankroll        initial_funding 100,000 TWD（BASE − 3 天）
    actual bets     #1 手動（manual_unlinked）LAL @ GSW 明日 大分 2,000（記錄於 BASE − 2 小時）
                    #2 legacy（D.5 以前的紀錄，origin = NULL）DEN @ OKC 明日 讓分主隊 1,000（placed_at BASE − 3 小時）
    decision board  明日 betting day：core.decision.board.build_board（與 production 同一函式）在 AS_OF = BASE − 1 分
                    以 seed 定價列（core.pricing.seed_fixture）為輸入、台彩-only D.3 + 實際下注 exposure 重新縮放

seed 預測不是 production ml-v2.0（model_version = elo-v0.1-seed），production 的 as-of 選擇不會採用 →
只在這裡用 candidate_selector / prediction_selector 把 seed 定價列直接當作候選（warnings 帶 seed_fixture）。
JSON 內的 game / bet / ledger id 以「全新 DB」（npm run db:reset:local）的插入順序為準。
"""
from __future__ import annotations

import json
from datetime import timedelta

from ..pricing.seed_fixture import BASE, _sql, seed_pricings
from ..sizing import engine as sz
from ..sizing.job import Selection
from . import bankroll as bk
from . import board
from . import evidence as ev
from . import exposure as ex
from . import job
from . import policy as P

AS_OF = BASE - timedelta(minutes=1)
AS_OF_TOKEN = "{{NOW:-1m}}"
DAY_TOKEN = "{{TPEDATE:+1}}"
# 明日（台灣）08:00 / 09:30 / 11:00 = BASE（台灣 1/1 08:00）+ 24h / 25.5h / 27h；seed games 插入順序 → id 1 / 2 / 3
GAMES = {"0022500101": (1, BASE + timedelta(hours=24)), "0022500102": (2, BASE + timedelta(hours=25, minutes=30)),
         "0022500103": (3, BASE + timedelta(hours=27))}
DEMO = "demo@example.com"
FUNDING = 100000.0
BETS = [
    dict(id=1, nba="0022500101", market="total", side="over", line=None, stake=2000.0, odds=1.88, origin="manual_unlinked",
         compliance="manual_unlinked", recorded=BASE - timedelta(hours=2), token="{{NOW:-2h}}", source="twsport"),
    dict(id=2, nba="0022500103", market="spread", side="home", line=-4.5, stake=1000.0, odds=1.75, origin=None,
         compliance=None, recorded=BASE - timedelta(hours=3), token="{{NOW:-3h}}", source=None),
]


def _game_sql(nba: str) -> str:
    return f"(SELECT id FROM games WHERE nba_game_id = '{nba}')"


def build():
    art, pricings = seed_pricings()
    rows, odds_rows, where = [], [], {}
    pid = 0
    for mp, r, tok, as_of_tok, pred_tok, nba_id in pricings:
        gid = GAMES[nba_id][0]
        odds_rows.append({**r, "game_id": gid, "bookmaker": None, "last_seen_at": None})
        for row in mp.to_rows():
            pid += 1
            row = {**row, "id": pid, "game_id": gid, "prediction_id": None}
            rows.append(row)
            where[pid] = (r, tok, nba_id)

    def selector(games, odds, preds, pricing, now):
        ids = {int(o["id"]) for o in odds}
        sel = Selection()
        for p in pricing:
            if int(p["odds_snapshot_id"]) in ids:
                start = next(GAMES[n][1] for n in GAMES if GAMES[n][0] == p["game_id"])
                sel.candidates.append(sz.candidate_from_pricing_row(p, game_start_utc=start, odds_last_seen_at=None))
        return sel

    def prediction(_rows, gid, _now):
        return {"prediction_id": gid, "model_version": "elo-v0.1-seed", "artifact_version": art.artifact_version,
                "prediction_kind": "seed", "created_at": None, "available_at": None, "pred_margin": None,
                "pred_total": None, "h1_margin": None, "h1_total": None, "data_quality_flags": ["seed_fixture"]}

    # legacy 注單沒有 betting_day 欄位：與 job.bet_from_row 相同，由比賽開賽時間導出
    bets = [ex.ActualBet(bet_id=b["id"], game_id=GAMES[b["nba"]][0], betting_day=sz.betting_day(GAMES[b["nba"]][1]),
                         market=b["market"], side=b["side"], stake=b["stake"], odds=b["odds"],
                         recorded_time=b["recorded"], origin=b["origin"], source=b["source"],
                         strategy_compliance=b["compliance"]) for b in BETS]
    ledger = [bk.LedgerEntry(1, "initial_funding", FUNDING, BASE - timedelta(days=3))]
    account = {"id": 1, "currency": "TWD", "risk_state_version": 2}
    tw_source = {"source_key": "twsport", "last_status": "ok", "last_outcome": "success",
                 "last_success_at": BASE - timedelta(minutes=18)}
    games = [{"id": gid, "date_utc": start, "status": "scheduled"} for gid, start in GAMES.values()]
    evidence = ev.evidence_panel(ev.paper_evidence([], strategy_id="execution-v1/risk-v1/…/twsport:twsport"))
    res = board.build_board(board.BoardInputs(
        now=AS_OF, betting_day=sz.betting_day(GAMES["0022500101"][1]), user_id=0, games=games, odds_rows=odds_rows,
        pred_rows=[], pricing_rows=rows, bets=bets, account=account, ledger=ledger, twsport_source=tw_source,
        paper={}, evidence=evidence, bet_event_watermark=1, candidate_selector=selector,
        prediction_selector=prediction))
    return art, res, where


def generate() -> str:
    art, res, where = build()
    user = f"(SELECT id FROM users WHERE email = '{DEMO}')"
    acct = f"(SELECT id FROM bankroll_accounts WHERE user_id = {user})"
    out = [
        f"INSERT INTO bankroll_accounts (user_id, currency, label, created_at, risk_state_version, risk_state_changed_at)\n"
        f"SELECT id, 'TWD', 'demo strategy bankroll', '{{{{NOW:-3d}}}}', 2, '{{{{NOW:-2h}}}}' FROM users WHERE email = '{DEMO}';",
        f"INSERT INTO risk_state_claims (account_id, version, kind, request_id, created_at) VALUES "
        f"({acct}, 1, 'ledger_entry', 'seed-ledger-1', '{{{{NOW:-3d}}}}'), ({acct}, 2, 'bet_recorded', 'seed-bet-1', '{{{{NOW:-2h}}}}');",
        f"INSERT INTO bankroll_ledger (account_id, entry_type, amount, reason, recorded_by, client_request_id, recorded_at) VALUES "
        f"({acct}, 'initial_funding', {FUNDING}, 'seed demo', 'user', 'seed-ledger-1', '{{{{NOW:-3d}}}}');",
    ]
    for b in BETS:
        gsql = _game_sql(b["nba"])
        if b["origin"] is None:      # legacy：只有 D.5 以前就有的欄位
            out.append("INSERT INTO bets (user_id, game_id, market, selection, line, odds, stake, result, note, placed_at) "
                       f"VALUES ({user}, {gsql}, '{b['market']}', '{b['side']}', {_sql(b['line'])}, {b['odds']}, {b['stake']}, "
                       f"'pending', 'seed：D.5 以前的 legacy 紀錄', '{b['token']}');")
        else:
            out.append("INSERT INTO bets (user_id, game_id, market, selection, line, odds, stake, result, note, placed_at, "
                       "account_id, betting_day, source, bookmaker, origin, strategy_compliance, risk_check, client_request_id, "
                       "recorded_at, record_status) VALUES "
                       f"({user}, {gsql}, '{b['market']}', '{b['side']}', {_sql(b['line'])}, {b['odds']}, {b['stake']}, 'pending', "
                       f"'seed：手動記錄', '{b['token']}', {acct}, '{DAY_TOKEN}', '{b['source']}', 'twsport', '{b['origin']}', "
                       f"'{b['compliance']}', '{{\"seed\": true}}', 'seed-bet-{b['id']}', '{b['token']}', 'active');")
            out.append("INSERT INTO bet_events (bet_id, user_id, event_type, actor, payload, created_at) VALUES "
                       f"((SELECT id FROM bets WHERE client_request_id = 'seed-bet-{b['id']}'), {user}, 'recorded', 'user', "
                       f"'{{\"seed\": true}}', '{b['token']}');")
    d = res.new_day_start
    out.append("INSERT INTO bankroll_day_snapshots (account_id, betting_day, day_start_bankroll, basis_as_of, ledger_balance, "
               "open_stake_excluded, ledger_watermark, established_reason, bankroll_version) VALUES "
               f"({acct}, '{DAY_TOKEN}', {d.day_start_bankroll}, '{{{{NOW:-3h}}}}', {d.ledger_balance}, {d.open_stake_excluded}, "
               f"{d.ledger_watermark}, '{d.established_reason}', '{d.bankroll_version}');")
    snap = dict(res.snapshot)
    cols = ("user_id", "account_id", "betting_day", "as_of", "last_confirmed_at", "decision_version",
            "risk_policy_version", "execution_policy_version", "scope", "input_fingerprint", "risk_state_version",
            "ledger_watermark", "bet_event_watermark", "bankroll_day_snapshot_id", "status") + job.SNAPSHOT_JSON
    vals = {"user_id": user, "account_id": acct, "betting_day": f"'{DAY_TOKEN}'", "as_of": f"'{AS_OF_TOKEN}'",
            "last_confirmed_at": f"'{AS_OF_TOKEN}'", "input_fingerprint": _sql(res.fingerprint),
            "bankroll_day_snapshot_id": f"(SELECT id FROM bankroll_day_snapshots WHERE account_id = {acct})"}
    for c in cols:
        if c in vals:
            continue
        v = snap[c]
        vals[c] = _sql(json.dumps(v, ensure_ascii=False, allow_nan=False, default=str)) if c in job.SNAPSHOT_JSON else _sql(v)
    out.append(f"INSERT INTO decision_snapshots ({', '.join(cols)}) VALUES ({', '.join(vals[c] for c in cols)});")
    snap_id = f"(SELECT id FROM decision_snapshots WHERE input_fingerprint = '{res.fingerprint}')"
    for o in res.opportunities:
        r, tok, nba_id = where[o["pricing_snapshot_id"]]
        osnap = (f"(SELECT o.id FROM odds_snapshots o WHERE o.game_id = {_game_sql(nba_id)} AND o.source = '{r['source']}' "
                 f"AND o.market = '{r['market']}' AND o.bookmaker IS NULL AND o.fetched_at = '{tok}')")
        mp = f"(SELECT m.id FROM market_pricing_snapshots m WHERE m.odds_snapshot_id = {osnap} AND m.side = '{o['side']}')"
        row = {**o, "snapshot_id": snap_id, "user_id": user, "betting_day": f"'{DAY_TOKEN}'", "game_id": _game_sql(nba_id),
               "odds_snapshot_id": osnap, "pricing_snapshot_id": mp, "odds_fetched_at": f"'{tok}'",
               "sizing_snapshot_id": f"(SELECT MAX(s.id) FROM bet_sizing_snapshots s WHERE s.market_pricing_snapshot_id = {mp})",
               "prediction_id": f"(SELECT p.id FROM predictions p WHERE p.game_id = {_game_sql(nba_id)} AND p.model_version = 'elo-v0.1-seed')"}
        raw = ("snapshot_id", "user_id", "betting_day", "game_id", "odds_snapshot_id", "pricing_snapshot_id",
               "odds_fetched_at", "sizing_snapshot_id", "prediction_id")
        vs = []
        for c in job.OPP_COLUMNS:
            v = row.get(c)
            if c in raw:
                vs.append(v)
            elif c in job.OPP_JSON:
                vs.append(_sql(json.dumps(v, ensure_ascii=False, allow_nan=False)))
            else:
                vs.append(_sql(v))
        out.append(f"INSERT INTO decision_opportunities ({', '.join(job.OPP_COLUMNS)}) VALUES ({', '.join(vs)});")
    body = "\n".join(out)
    # JSON 內的絕對時間 / 日期 → render-seed token（seed 以「現在」為錨點重新展開）
    day = sz.betting_day(GAMES["0022500101"][1])
    for when, tok in ((BASE - timedelta(hours=3), "{{NOW:-3h}}"), (BASE - timedelta(hours=2), "{{NOW:-2h}}"),
                      (AS_OF, AS_OF_TOKEN)):
        body = body.replace(when.isoformat(), tok)
    body = body.replace(f'"{day.isoformat()}"', f'"{DAY_TOKEN}"')
    for gid, start in GAMES.values():
        body = body.replace(start.isoformat(), {1: "{{TPE:+1 08:00}}", 2: "{{TPE:+1 09:30}}", 3: "{{TPE:+1 11:00}}"}[gid])
    header = ("-- >>> GENERATED by `cd pipeline && python -m core.decision.seed_fixture`"
              f"（artifact {art.artifact_version}，{P.DECISION_VERSION} / {P.RISK_POLICY.version}）\n"
              "-- demo bankroll / 實際下注 / day-start / decision board；數字 = core.decision.board.build_board（與 production 同一函式）。"
              "勿手動修改。id 以全新 DB（npm run db:reset:local）插入順序為準。\n")
    return header + body + "\n-- <<< GENERATED (D.5)\n"


if __name__ == "__main__":
    print(generate(), end="")
