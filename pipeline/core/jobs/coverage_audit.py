"""
歷史資料完整度稽核（Phase C.5B）
------------------------------------------------------------
按賽季輸出各資料層的覆蓋率與缺漏數，並能定位缺漏的比賽/日期。分母一律是「已結束（final）的
例行賽 / 附加賽 / 季後賽」，不補造資料來讓數字好看。

稽核項目（key → 意義）：
  games_final           已結束賽事數（分母）
  official_id           已取得官方 NBA gameId（不是 'espn:' 暫存）
  score_present         雙方總分都有值
  quarters              逐節（Q1~Q4 雙方）齊全
  quarter_sum_ok        Q1~Q4(+OT) 加總 = 總分（資料自洽）
  h1_present            上半場（H1）與下半場（H2）雙方齊全
  team_stats            雙方球隊 box score 齊全（含 fgm/fga/fta 等原始計數）
  team_pts_ok           球隊統計得分 = 賽事比分
  player_stats          雙方都有球員統計
  starters_5            雙方各剛好 5 位先發
  minutes_ok            雙方球員分鐘加總 = 球隊總分鐘（±1 分）
  derived_metrics       雙方衍生進階指標（pace/ortg/drtg/…）齊全且為目前公式版本
  injury_both_teams     開賽前有已申報報告涵蓋「雙方」球隊（見 injury_asof）
  injury_any_team       開賽前至少一方被報告涵蓋

CLI：
  python -m core.jobs.coverage_audit                      # 全部賽季摘要
  python -m core.jobs.coverage_audit --season 2023-24 --missing team_stats --limit 50
  python -m core.jobs.coverage_audit --json out.json      # 含缺漏清單
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .. import injury_asof, metrics
from ..timeutil import et_date

AUDIT_KEYS = [
    "official_id", "score_present", "quarters", "quarter_sum_ok", "h1_present", "team_stats", "team_pts_ok",
    "player_stats", "starters_5", "minutes_ok", "derived_metrics", "injury_both_teams", "injury_any_team",
]
STAGES = ("regular", "playin", "playoffs")

_GAME_SQL = """
WITH p AS (
  SELECT game_id, team_id, count(*) AS n, sum(min) AS mins, sum(started) AS starters, sum(pts) AS pts
    FROM player_game_stats GROUP BY game_id, team_id
), t AS (
  SELECT game_id, team_id, pts, fga, fta, fgm, fg3m, fg3a, ftm, oreb, dreb, tov, ast, team_min
    FROM team_game_stats
), d AS (
  SELECT game_id, team_id, formula_version, ortg, drtg, pace, efg_pct, ts_pct, tov_pct
    FROM team_game_derived
)
SELECT g.id, g.nba_game_id, g.season, g.season_stage, g.date_utc,
       g.home_team_id, g.away_team_id, ht.abbr AS home_abbr, at.abbr AS away_abbr,
       g.home_pts, g.away_pts,
       g.home_q1, g.home_q2, g.home_q3, g.home_q4, g.home_ot, g.away_q1, g.away_q2, g.away_q3, g.away_q4, g.away_ot,
       g.home_h1, g.home_h2, g.away_h1, g.away_h2,
       ph.n AS h_n, ph.mins AS h_mins, ph.starters AS h_st, ph.pts AS h_ppts,
       pa.n AS a_n, pa.mins AS a_mins, pa.starters AS a_st, pa.pts AS a_ppts,
       th.pts AS h_tpts, th.fga AS h_fga, th.fta AS h_fta, th.fgm AS h_fgm, th.fg3m AS h_fg3m, th.fg3a AS h_fg3a,
       th.ftm AS h_ftm, th.oreb AS h_oreb, th.dreb AS h_dreb, th.tov AS h_tov, th.ast AS h_ast, th.team_min AS h_tmin,
       ta.pts AS a_tpts, ta.fga AS a_fga, ta.fta AS a_fta, ta.fgm AS a_fgm, ta.fg3m AS a_fg3m, ta.fg3a AS a_fg3a,
       ta.ftm AS a_ftm, ta.oreb AS a_oreb, ta.dreb AS a_dreb, ta.tov AS a_tov, ta.ast AS a_ast, ta.team_min AS a_tmin,
       dh.formula_version AS h_dver, dh.ortg AS h_ortg, dh.pace AS h_pace, dh.efg_pct AS h_efg,
       da.formula_version AS a_dver, da.ortg AS a_ortg, da.pace AS a_pace, da.efg_pct AS a_efg
  FROM games g
  JOIN teams ht ON ht.id = g.home_team_id JOIN teams at ON at.id = g.away_team_id
  LEFT JOIN p ph ON ph.game_id = g.id AND ph.team_id = g.home_team_id
  LEFT JOIN p pa ON pa.game_id = g.id AND pa.team_id = g.away_team_id
  LEFT JOIN t th ON th.game_id = g.id AND th.team_id = g.home_team_id
  LEFT JOIN t ta ON ta.game_id = g.id AND ta.team_id = g.away_team_id
  LEFT JOIN d dh ON dh.game_id = g.id AND dh.team_id = g.home_team_id
  LEFT JOIN d da ON da.game_id = g.id AND da.team_id = g.away_team_id
 WHERE g.status = 'final' AND g.season_stage = ANY(%s) {season_filter}
 ORDER BY g.date_utc, g.id
"""


def _has(*vals: Any) -> bool:
    return all(v is not None for v in vals)


def _raw(prefix: str, g: dict) -> dict:
    return {"pts": g[f"{prefix}_tpts"], "fga": g[f"{prefix}_fga"], "fgm": g[f"{prefix}_fgm"],
            "fg3m": g[f"{prefix}_fg3m"], "fg3a": g[f"{prefix}_fg3a"], "ftm": g[f"{prefix}_ftm"],
            "fta": g[f"{prefix}_fta"], "oreb": g[f"{prefix}_oreb"], "dreb": g[f"{prefix}_dreb"],
            "tov": g[f"{prefix}_tov"], "ast": g[f"{prefix}_ast"]}


def evaluate_game(g: dict, *, injury_index: injury_asof.InjuryIndex | None = None) -> dict[str, bool]:
    """單場的各項稽核旗標（True = 完整/正確）。純函式（injury 需要的索引由呼叫端提供）。"""
    f: dict[str, bool] = {}
    f["official_id"] = bool(g["nba_game_id"]) and not str(g["nba_game_id"]).startswith("espn:")
    f["score_present"] = _has(g["home_pts"], g["away_pts"])
    qs = [g[f"{s}_q{i}"] for s in ("home", "away") for i in range(1, 5)]
    f["quarters"] = _has(*qs)
    f["quarter_sum_ok"] = False
    if f["quarters"] and f["score_present"]:
        ok = True
        for s in ("home", "away"):
            total = sum(g[f"{s}_q{i}"] for i in range(1, 5)) + (g[f"{s}_ot"] or 0)
            ok &= total == g[f"{s}_pts"]
        f["quarter_sum_ok"] = ok
    f["h1_present"] = _has(g["home_h1"], g["home_h2"], g["away_h1"], g["away_h2"])
    h_raw, a_raw = _raw("h", g), _raw("a", g)
    f["team_stats"] = metrics.has_raw(h_raw) and metrics.has_raw(a_raw)
    f["team_pts_ok"] = f["team_stats"] and f["score_present"] and \
        (h_raw["pts"], a_raw["pts"]) == (g["home_pts"], g["away_pts"])
    f["player_stats"] = bool(g["h_n"]) and bool(g["a_n"])
    f["starters_5"] = f["player_stats"] and g["h_st"] == 5 and g["a_st"] == 5
    f["minutes_ok"] = False
    if f["player_stats"] and _has(g["h_tmin"], g["a_tmin"], g["h_mins"], g["a_mins"]):
        f["minutes_ok"] = abs(g["h_mins"] - g["h_tmin"]) <= 1.0 and abs(g["a_mins"] - g["a_tmin"]) <= 1.0
    f["derived_metrics"] = (g["h_dver"] == metrics.FORMULA_VERSION and g["a_dver"] == metrics.FORMULA_VERSION
                            and _has(g["h_ortg"], g["a_ortg"], g["h_pace"], g["a_pace"]))
    f["injury_both_teams"] = f["injury_any_team"] = False
    if injury_index is not None:
        gd, tip = et_date(g["date_utc"]), g["date_utc"]
        hs = injury_asof.team_state_asof(injury_index, team_abbr=g["home_abbr"], game_date=gd, cutoff_utc=tip)
        as_ = injury_asof.team_state_asof(injury_index, team_abbr=g["away_abbr"], game_date=gd, cutoff_utc=tip)
        f["injury_both_teams"] = hs.known and as_.known
        f["injury_any_team"] = hs.known or as_.known
    return f


def audit(cur, seasons: list[str] | None = None, *, injury_index: injury_asof.InjuryIndex | None = None,
          include_injury: bool = True) -> dict[str, Any]:
    """回傳 {season: {games_final, keys: {key: {have, missing, pct}}, missing: {key: [game…]}}, extras}。"""
    where = "AND g.season = ANY(%s)" if seasons else ""
    cur.execute(_GAME_SQL.format(season_filter=where), (list(STAGES),) + ((seasons,) if seasons else ()))
    games = cur.fetchall()
    if include_injury and injury_index is None:
        injury_index = injury_asof.load_index(cur)
    if not include_injury:
        injury_index = None

    by_season: dict[str, dict] = {}
    for g in games:
        s = by_season.setdefault(g["season"], {"games_final": 0, "have": Counter(), "missing": defaultdict(list),
                                               "ages": []})
        s["games_final"] += 1
        flags = evaluate_game(g, injury_index=injury_index)
        for k in AUDIT_KEYS:
            if flags[k]:
                s["have"][k] += 1
            else:
                s["missing"][k].append({
                    "game_id": g["id"], "nba_game_id": g["nba_game_id"], "date_et": et_date(g["date_utc"]).isoformat(),
                    "matchup": f"{g['away_abbr']}@{g['home_abbr']}", "stage": g["season_stage"]})
        if injury_index is not None and flags["injury_any_team"]:
            st = injury_asof.team_state_asof(injury_index, team_abbr=g["home_abbr"], game_date=et_date(g["date_utc"]),
                                             cutoff_utc=g["date_utc"])
            if st.known:
                s["ages"].append((g["date_utc"] - st.report_time_utc).total_seconds() / 60)

    result: dict[str, Any] = {}
    for season, s in sorted(by_season.items()):
        n = s["games_final"]
        keys = {k: {"have": s["have"][k], "missing": n - s["have"][k],
                    "pct": round(100.0 * s["have"][k] / n, 2) if n else None} for k in AUDIT_KEYS}
        ages = s["ages"]
        result[season] = {
            "games_final": n, "keys": keys,
            "missing_dates": {k: dict(Counter(m["date_et"] for m in s["missing"][k])) for k in AUDIT_KEYS},
            "missing": {k: s["missing"][k] for k in AUDIT_KEYS},
            "injury_report_age_min": ({"median": round(statistics.median(ages), 1),
                                       "p90": round(sorted(ages)[int(0.9 * (len(ages) - 1))], 1)} if ages else None),
        }
    return result


def injury_report_ledger(cur) -> dict[str, Any]:
    """injury_reports 的整體統計（每季報告數、列數、未對應比例）。"""
    cur.execute(
        """
        SELECT count(*) AS reports, coalesce(sum(n_entries), 0) AS entries, coalesce(sum(n_unmatched), 0) AS unmatched,
               min(report_time_utc) AS first_report, max(report_time_utc) AS last_report
          FROM injury_reports""")
    total = cur.fetchone()
    cur.execute("SELECT status, count(*) AS n FROM source_fetch_log WHERE kind = 'injury_report' GROUP BY status")
    return {"total": dict(total), "fetch_log": {r["status"]: r["n"] for r in cur.fetchall()}}


def format_table(result: dict[str, Any]) -> str:
    seasons = list(result)
    head = f"{'metric':<20}" + "".join(f"{s:>22}" for s in seasons)
    lines = [head, "-" * len(head)]
    lines.append(f"{'games_final':<20}" + "".join(f"{result[s]['games_final']:>22}" for s in seasons))
    for k in AUDIT_KEYS:
        cells = []
        for s in seasons:
            v = result[s]["keys"][k]
            cells.append(f"{v['have']}/{result[s]['games_final']} ({v['pct']}%) -{v['missing']}")
        lines.append(f"{k:<20}" + "".join(f"{c:>22}" for c in cells))
    return "\n".join(lines)


def main() -> None:
    from ..db import cursor
    ap = argparse.ArgumentParser(description="歷史資料完整度稽核")
    ap.add_argument("--season", nargs="+", default=None)
    ap.add_argument("--missing", choices=AUDIT_KEYS, help="列出該項缺漏的比賽")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--json", help="輸出完整結果（含缺漏清單）到檔案")
    ap.add_argument("--no-injury", action="store_true", help="略過傷病涵蓋稽核（較快）")
    args = ap.parse_args()
    with cursor() as cur:
        result = audit(cur, args.season, include_injury=not args.no_injury)
        ledger = injury_report_ledger(cur)
    print(format_table(result))
    for s, v in result.items():
        if v["injury_report_age_min"]:
            print(f"{s} 賽前最後一份傷病報告距開賽（分鐘）：{v['injury_report_age_min']}")
    print("傷病報告帳本：", json.dumps(ledger, default=str, ensure_ascii=False))
    if args.missing:
        for s, v in result.items():
            miss = v["missing"][args.missing]
            print(f"\n[{s}] {args.missing} 缺 {len(miss)} 場（依日期：{dict(list(v['missing_dates'][args.missing].items())[:10])}…）")
            for m in miss[: args.limit]:
                print("  ", m["date_et"], m["matchup"], m["nba_game_id"])
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"generated_at": datetime.utcnow().isoformat(), "seasons": result, "injury_ledger": ledger},
                      fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
