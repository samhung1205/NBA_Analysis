"""
由基本 box score 可重現計算的球隊進階指標（不依賴 stats.nba.com）
------------------------------------------------------------
輸入：同一場比賽雙方的 team_game_stats 原始計數；輸出：寫入 team_game_derived 的欄位。
所有公式集中在這裡，FORMULA_VERSION 變動時整批重算（見 jobs/derive_metrics.py）。

符號：PTS 得分、FGM/FGA 投籃命中/出手、3PM/3PA 三分、FTM/FTA 罰球、OREB/DREB 進攻/防守籃板
（含球隊籃板）、TOV 失誤（含球隊失誤）、AST 助攻、MIN 球隊總分鐘（240 + 25×延長賽節數）。
下標 o = 對手。

  估計控球數（Basketball-Reference 球隊公式，雙方平均）
    poss_i = FGA + 0.4·FTA − 1.07·OREB/(OREB + DREB_o)·(FGA − FGM) + TOV
    poss   = (poss_i + poss_o) / 2                          ← 同場兩隊相同
  pace     = 48 · poss / (MIN / 5)                          （每 48 分鐘控球數；OT 場以分鐘正規化）
  ortg     = 100 · PTS   / poss
  drtg     = 100 · PTS_o / poss
  net_rtg  = ortg − drtg
  efg_pct  = (FGM + 0.5·3PM) / FGA
  ts_pct   = PTS / (2·(FGA + 0.44·FTA))
  tov_pct  = TOV / (FGA + 0.44·FTA + TOV)
  orb_pct  = OREB / (OREB + DREB_o)
  drb_pct  = DREB / (DREB + OREB_o)
  ftr      = FTA / FGA
  fg3a_rate= 3PA / FGA
  ast_pct  = AST / FGM

來源：cdn.nba.com 基本 box score（球隊統計）。與官方 advanced 的差異：官方 pace/off_rtg 是以
逐球 play-by-play 計算的控球數，這裡是公式估計（通常誤差約 1%）；eFG / TS 是精確公式，與官方一致。
官方欄位（team_game_stats.pace / off_rtg …）語意不變，這裡寫入獨立的 team_game_derived。
"""
from __future__ import annotations

from typing import Any, Mapping

FORMULA_VERSION = "box-v1"

REQUIRED_RAW = ("pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "tov", "ast")
DERIVED_COLUMNS = ("poss", "pace", "ortg", "drtg", "net_rtg", "efg_pct", "ts_pct", "tov_pct",
                   "orb_pct", "drb_pct", "ftr", "fg3a_rate", "ast_pct")


def _div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def has_raw(team: Mapping[str, Any]) -> bool:
    return all(team.get(k) is not None for k in REQUIRED_RAW)


def team_possessions(team: Mapping[str, Any], opp: Mapping[str, Any]) -> float | None:
    """單隊視角的控球數估計 poss_i。"""
    if not (has_raw(team) and has_raw(opp)):
        return None
    orb = _div(team["oreb"], team["oreb"] + opp["dreb"])
    if orb is None:
        return None
    return team["fga"] + 0.4 * team["fta"] - 1.07 * orb * (team["fga"] - team["fgm"]) + team["tov"]


def game_possessions(home: Mapping[str, Any], away: Mapping[str, Any]) -> float | None:
    ph, pa = team_possessions(home, away), team_possessions(away, home)
    if ph is None or pa is None:
        return None
    return (ph + pa) / 2


def derive_side(team: Mapping[str, Any], opp: Mapping[str, Any], poss: float | None,
                minutes: float | None) -> dict[str, float | None]:
    """單隊的衍生指標。raw 不完整則全部 None（不補造）。"""
    out: dict[str, float | None] = {c: None for c in DERIVED_COLUMNS}
    if not (has_raw(team) and has_raw(opp)):
        return out
    mins = minutes if minutes and minutes > 0 else 240.0
    out["poss"] = poss
    out["pace"] = _div(48.0 * poss, mins / 5.0) if poss is not None else None
    out["ortg"] = _div(100.0 * team["pts"], poss)
    out["drtg"] = _div(100.0 * opp["pts"], poss)
    if out["ortg"] is not None and out["drtg"] is not None:
        out["net_rtg"] = out["ortg"] - out["drtg"]
    out["efg_pct"] = _div(team["fgm"] + 0.5 * team["fg3m"], team["fga"])
    out["ts_pct"] = _div(team["pts"], 2.0 * (team["fga"] + 0.44 * team["fta"]))
    out["tov_pct"] = _div(team["tov"], team["fga"] + 0.44 * team["fta"] + team["tov"])
    out["orb_pct"] = _div(team["oreb"], team["oreb"] + opp["dreb"])
    out["drb_pct"] = _div(team["dreb"], team["dreb"] + opp["oreb"])
    out["ftr"] = _div(team["fta"], team["fga"])
    out["fg3a_rate"] = _div(team["fg3a"], team["fga"])
    out["ast_pct"] = _div(team["ast"], team["fgm"])
    return out


def derive_game(home: Mapping[str, Any], away: Mapping[str, Any]) -> tuple[dict, dict]:
    """同一場比賽雙方的衍生指標 → (home_metrics, away_metrics)。"""
    poss = game_possessions(home, away)
    minutes = home.get("team_min") or away.get("team_min")
    return derive_side(home, away, poss, minutes), derive_side(away, home, poss, minutes)
