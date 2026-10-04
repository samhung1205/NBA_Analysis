"""
本機 D1 / smoke test 用的 seed 定價列產生器（不碰任何資料庫）
------------------------------------------------------------
    python -m core.pricing.seed_fixture > /tmp/seed_pricing.sql     # 再貼回 seed/seed.template.sql 的 GENERATED 區塊

seed 的預測是假資料（model_version = elo-v0.1-seed，沒有 artifact），無法走 production 的
line_probability_from_prediction_row。為了讓 Node API 的讀取路徑有東西可測、又不在任何地方另寫一份數學：
  * 盤口：解析 seed.template.sql 的 odds_snapshots，經 D.1 canonical（odds_input_from_row）
  * 模型機率：C.5E probability.predict_line_probability(本機 production artifact, 'final', target, seed 點預測, 線)
  * 去水 / edge / EV：engine.price_market（與 production 同一個函式）
輸出列標記 warnings 含 "seed_fixture"；時間欄位保留 {{NOW:…}} token，交給 scripts/render-seed.mjs 展開。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..production import artifact as art_mod
from ..production import probability
from .alignment import latest_snapshots_as_of, odds_input_from_row
from .engine import PredictionInput, price_market
from .job import COLUMNS

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "seed" / "seed.template.sql"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
GAMES = ("0022500101", "0022500102", "0022500103")          # 明日 3 場（總覽頁 / smoke test 使用）

ODDS_RE = re.compile(r"SELECT '\{\{NOW:-(\d+)([mh])\}\}', id, '(\w+)',\s*'(\w+)',\s*([-\d.]+|NULL),\s*([-\d.]+|NULL),"
                     r"\s*([-\d.]+|NULL),\s*([-\d.]+|NULL),\s*([-\d.]+|NULL) FROM games WHERE nba_game_id='(\d+)'")
PRED_RE = re.compile(r"SELECT id, 'elo-v0.1-seed', '\{\{NOW:-(\d+)([mh])\}\}',\s*([-\d.]+), ([-\d.]+), ([-\d.]+), "
                     r"([-\d.]+), ([-\d.]+),[^\n]*\n[^\n]*\nFROM games WHERE nba_game_id='(\d+)'")


def _minutes(n: str, unit: str) -> int:
    return int(n) * (60 if unit == "h" else 1)


def _num(x: str):
    return None if x == "NULL" else float(x)


class SeedProbabilities:
    def __init__(self, art, points: dict[str, float]):
        self.art, self.points = art, points

    def line(self, target, threshold):
        return probability.predict_line_probability(self.art, "final", target, self.points[target], threshold)


def _sql(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    return "'" + str(v).replace("'", "''") + "'"


def seed_pricings(root=None):
    """→ (artifact, [(MarketPricing, odds_row, odds_token, analysis_as_of_token, prediction_token, nba_game_id)])。
    D.3 seed sizing（core.sizing.seed_fixture）也用這份結果，兩者不會不一致。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    art = art_mod.load_current(root)
    odds_rows, token = [], {}
    for i, m in enumerate(ODDS_RE.finditer(text), start=1):
        mins = _minutes(m[1], m[2])
        r = {"id": i, "game_id": int(m[10][-3:]), "source": m[3], "market": m[4], "line": _num(m[5]),
             "home_odds": _num(m[6]), "away_odds": _num(m[7]), "over_odds": _num(m[8]), "under_odds": _num(m[9]),
             "fetched_at": BASE - timedelta(minutes=mins)}
        odds_rows.append(r)
        token[i] = (f"{{{{NOW:-{m[1]}{m[2]}}}}}", mins, m[10])
    preds = {}
    for m in PRED_RE.finditer(text):
        preds[m[8]] = {"minutes": _minutes(m[1], m[2]), "token": f"{{{{NOW:-{m[1]}{m[2]}}}}}",
                       "home_win_prob": float(m[3]), "margin": float(m[4]), "total": float(m[5]),
                       "h1_home": float(m[6]), "h1_away": float(m[7])}
    out = []
    for nba_id in GAMES:
        p = preds[nba_id]
        gid = int(nba_id[-3:])
        created = BASE - timedelta(minutes=p["minutes"])
        pin = PredictionInput(None, gid, "elo-v0.1-seed", created, created, art.artifact_version, "final", "seed",
                              p["home_win_prob"])
        model = SeedProbabilities(art, {"margin": p["margin"], "total": p["total"],
                                        "h1_margin": p["h1_home"] - p["h1_away"],
                                        "h1_total": p["h1_home"] + p["h1_away"]})
        for r in latest_snapshots_as_of([r for r in odds_rows if r["game_id"] == gid], BASE):
            mp = price_market(odds_input_from_row(r), pin, model, as_of=BASE)
            mp.warnings.append("seed_fixture")
            tok, mins, _ = token[r["id"]]
            as_of_tok = tok if mins <= p["minutes"] else p["token"]
            out.append((mp, r, tok, as_of_tok, p["token"], nba_id))
    return art, out


def generate(root=None) -> str:
    import json

    art, pricings = seed_pricings(root)
    out = []
    for mp, r, tok, as_of_tok, pred_tok, nba_id in pricings:
        bm = "IS NULL"
        for row in mp.to_rows():
            vals = []
            for c in COLUMNS:
                if c == "odds_snapshot_id":
                    vals.append(f"(SELECT o.id FROM odds_snapshots o WHERE o.game_id = g.id AND o.source = "
                                f"'{r['source']}' AND o.market = '{r['market']}' AND o.bookmaker {bm} "
                                f"AND o.fetched_at = '{tok}')")
                elif c == "prediction_id":
                    vals.append("(SELECT p.id FROM predictions p WHERE p.game_id = g.id AND "
                                "p.model_version = 'elo-v0.1-seed')")
                elif c == "game_id":
                    vals.append("g.id")
                elif c == "analysis_as_of":
                    vals.append(f"'{as_of_tok}'")
                elif c == "odds_fetched_at":
                    vals.append(f"'{tok}'")
                elif c == "prediction_created_at":
                    vals.append(f"'{pred_tok}'")
                elif c in ("warnings", "diagnostics"):
                    vals.append(_sql(json.dumps(row[c], ensure_ascii=False, allow_nan=False)))
                else:
                    vals.append(_sql(row[c]))
            out.append(f"INSERT INTO market_pricing_snapshots ({', '.join(COLUMNS)})\nSELECT {', '.join(vals)}"
                       f"\nFROM games g WHERE g.nba_game_id = '{nba_id}';")
    header = (f"-- >>> GENERATED by `cd pipeline && python -m core.pricing.seed_fixture`（artifact {art.artifact_version}）\n"
              "-- seed 預測是假資料：模型機率 = C.5E predict_line_probability(production artifact, seed 點預測)，\n"
              "-- 去水 / edge / EV = core.pricing.engine.price_market（與 production 同一函式）。勿手動修改。\n")
    return header + "\n".join(out) + "\n-- <<< GENERATED\n"


if __name__ == "__main__":
    print(generate(), end="")
