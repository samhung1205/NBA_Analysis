"""Reduced wire rows must produce the identical decision / sizing outputs."""
from dataclasses import asdict
from datetime import timedelta

from core.decision import board, job as decision
from core.odds.canonical import MONEYLINE, CLOSED
from core.pricing import job as pricing
from core.pricing.alignment import latest_snapshots_as_of, select_prediction
from core.pricing.queries import load_latest_odds
from core.sizing import job as sizing
from core.timeutil import tpe_date
from test_pricing_artifact import TIP, S, game, league, setup, insert_pred, write_odds  # noqa: F401
from test_sizing_db import prepare


def _legacy(cur, gid, now):
    out = {}
    for key, table, timecol in [('odds','odds_snapshots','fetched_at'),
                                ('preds','predictions','created_at'),
                                ('pricing','market_pricing_snapshots','analysis_as_of'),
                                ('sizing','bet_sizing_snapshots','analysis_as_of')]:
        cur.execute(f'SELECT * FROM {table} WHERE game_id=%s AND {timecol}<=%s ORDER BY id', (gid, now))
        out[key] = cur.fetchall()
    cur.execute('SELECT id,date_utc,status FROM games WHERE id=%s', (gid,))
    out['games'] = cur.fetchall()
    return out


def test_reduced_inputs_keep_fallback_asof_provenance_and_entire_board(db, game, setup):
    t1 = TIP-timedelta(hours=10)
    prepare(db, game, setup, t1)
    pricing.pricing_job(t1+timedelta(minutes=1), root=setup['root'], db=db)
    sizing.sizing_job(t1+timedelta(minutes=1), db=db)
    good = insert_pred(db, game, setup, t1+timedelta(minutes=20))
    now = t1+timedelta(minutes=25)
    pricing.pricing_job(now, root=setup['root'], db=db)
    sizing.sizing_job(now, db=db)
    bad = insert_pred(db, game, setup, now-timedelta(seconds=30))
    future = insert_pred(db, game, setup, now-timedelta(seconds=15))
    with db.cursor() as cur:
        cur.execute("UPDATE predictions SET features_json=features_json-'artifact_version' WHERE id=%s", (bad,))
        cur.execute("UPDATE predictions SET features_json=jsonb_set(features_json,'{prediction_as_of_utc}',to_jsonb(%s::text)) WHERE id=%s",
                    ((now+timedelta(hours=1)).isoformat(), future))
        old = _legacy(cur, game, now)
        reduced = decision.load_day_inputs(cur, tpe_date(TIP), now)
    assert select_prediction(reduced['preds'], game, now).prediction_id == good
    assert len(reduced['pricing']) < len(old['pricing'])
    assert len(reduced['sizing']) < len(old['sizing'])
    a = sizing.select_candidates(old['games'], old['odds'], old['preds'], old['pricing'], now)
    b = sizing.select_candidates(reduced['games'], reduced['odds'], reduced['preds'], reduced['pricing'], now)
    assert asdict(a) == asdict(b)
    def build(inp):
        return board.build_board(board.BoardInputs(now=now, betting_day=tpe_date(TIP), user_id=1,
            games=inp['games'], odds_rows=inp['odds'], pred_rows=inp['preds'],
            pricing_rows=inp['pricing'], sizing_rows=inp['sizing']))
    assert asdict(build(old)) == asdict(build(reduced))


def test_latest_closed_quote_ties_and_bookmaker_fallback_do_not_reopen_market(db, game, setup):
    t1 = TIP-timedelta(hours=10)
    prepare(db, game, setup, t1)
    write_odds(db, game, S(MONEYLINE, status=CLOSED, prices={'home':1.8,'away':2.0}), t1+timedelta(minutes=1))
    with db.cursor() as cur:
        # The ingestion uniqueness guard intentionally rejects changed content
        # at an identical source timestamp. Model legacy ties explicitly here.
        cur.execute("UPDATE odds_snapshots SET bookmaker='',fetched_at=%s WHERE game_id=%s AND market='ml' AND market_status='closed'", (t1, game))
    write_odds(db, game, S(MONEYLINE, prices={'home':2.1,'away':1.8}), t1+timedelta(hours=1))
    now = t1+timedelta(minutes=5)
    with db.cursor() as cur:
        old = _legacy(cur, game, now)['odds']
        reduced = load_latest_odds(cur, [game], now)
    assert latest_snapshots_as_of(old, now) == latest_snapshots_as_of(reduced, now)
    assert len(reduced) < len(old)
    assert next(r for r in reduced if r['market']=='ml')['market_status']=='closed'
