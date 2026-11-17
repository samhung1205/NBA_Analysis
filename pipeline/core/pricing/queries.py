"""Bounded live/as-of inputs: select in Postgres before transferring over Supavisor.

Keep the existing Python alignment/validation as the final authority. In
particular a closed latest quote must never fall back to an older open quote.
"""
from ..read_cache import read_unchanged
from .alignment import select_prediction
from .engine import PRICING_VERSION


def load_latest_odds(cur, game_ids, as_of):
    return read_unchanged(cur, ("live-odds", tuple(sorted(game_ids))), """SELECT * FROM (
        SELECT DISTINCT ON (game_id,source,COALESCE(NULLIF(bookmaker,''),source),market) *
        FROM odds_snapshots WHERE game_id=ANY(%s) AND fetched_at<=%s
        ORDER BY game_id,source,COALESCE(NULLIF(bookmaker,''),source),market,fetched_at DESC,id DESC
    ) latest""", (game_ids, as_of))


def load_predictions(cur, game_ids, as_of):
    # Keep every candidate: validation may reject the newest row or its as-of may
    # be in the future. Do not replace Python's validated fallback with MAX(id).
    return read_unchanged(cur, ("live-predictions", tuple(sorted(game_ids))),
        """SELECT id,game_id,model_version,created_at,home_win_prob,pred_margin,pred_total,
        pred_home_h1,pred_away_h1,features_json FROM predictions
        WHERE game_id=ANY(%s) AND created_at<=%s""", (game_ids, as_of))


def load_relevant_pricings(cur, game_ids, odds, predictions, as_of):
    selected = {gid: select_prediction(predictions, gid, as_of) for gid in game_ids}
    pred_ids = [p.prediction_id for p in selected.values() if p is not None]
    no_prediction = [gid for gid, p in selected.items() if p is None]
    return read_unchanged(cur, ("pricing", tuple(sorted(game_ids))), """SELECT * FROM market_pricing_snapshots
        WHERE game_id=ANY(%s) AND odds_snapshot_id=ANY(%s) AND analysis_as_of<=%s
          AND pricing_version=%s
          AND (prediction_id=ANY(%s) OR (prediction_id IS NULL AND game_id=ANY(%s)))
        """, (game_ids, [r["id"] for r in odds], as_of, PRICING_VERSION, pred_ids, no_prediction))


def load_sizing_references(cur, pricing_ids, risk_version, as_of):
    if not pricing_ids:
        return []
    cur.execute("""SELECT DISTINCT ON (market_pricing_snapshot_id)
        id,market_pricing_snapshot_id,analysis_as_of FROM bet_sizing_snapshots
        WHERE market_pricing_snapshot_id=ANY(%s) AND risk_policy_version=%s AND analysis_as_of<=%s
        ORDER BY market_pricing_snapshot_id,analysis_as_of DESC,id DESC""", (pricing_ids, risk_version, as_of))
    return cur.fetchall()
