"""賽前特徵底座：只用嚴格早於開賽的資料（洩漏防護）、滾動指標、輪替分鐘、傷病衝擊"""
import copy
import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from core import injury_asof as ia
from core.models import pregame_features as pf
from core.timeutil import UTC

T0 = datetime(2023, 11, 1, 0, 0, tzinfo=UTC)
A, B, C = 1, 2, 3
ABBR = {A: "AAA", B: "BBB", C: "CCC"}


def derived_for(gid, tid):
    base = 100.0 + gid * 1.5 + tid
    return {"pace": 95 + gid * 0.1, "ortg": base, "drtg": base - 3, "net_rtg": 3.0 + 0.01 * gid,
            "efg_pct": 0.5 + 0.001 * gid + 0.01 * tid, "ts_pct": 0.56, "tov_pct": 0.13, "orb_pct": 0.25,
            "drb_pct": 0.75, "ftr": 0.2, "fg3a_rate": 0.4}


def lineup_rows(gid, tid, mins=(34, 32, 30, 28, 26, 20, 16, 12, 8, 6)):
    """10 位球員，前 5 位先發；player_id = tid*100 + i。"""
    return [(tid, tid * 100 + i, float(m), i < 5) for i, m in enumerate(mins)]


def make_league(n_games=24, *, same_tip_pairs=(), start=T0, season="2023-24", tip_step=timedelta(days=2)):
    """依序輪流 A@B, B@C, C@A…；same_tip_pairs 內的 (i, i+1) 兩場同開賽時間（用不同球隊不可能，故只用於 4 隊情境）。"""
    games, derived, players = [], {}, {}
    matchups = [(A, B), (B, C), (C, A)]
    for i in range(n_games):
        h, a = matchups[i % 3]
        gid = i + 1
        tip = start + i * tip_step
        games.append(pf.GameRecord(gid, season, tip, h, a, ABBR[h], ABBR[a], 100 + i, 95 + (i % 7), 50, 48))
        derived[(gid, h)], derived[(gid, a)] = derived_for(gid, h), derived_for(gid, a)
        players[gid] = lineup_rows(gid, h) + lineup_rows(gid, a)
    return games, derived, players


def feats(games, derived, players, index=None, **kw):
    return pf.build_pregame_features(games, derived, players, index, **kw)


def row(df, gid):
    return df[df.game_id == gid].iloc[0]


def same(a: pd.Series, b: pd.Series, cols):
    for c in cols:
        x, y = a[c], b[c]
        if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
            continue
        assert x == y, f"{c}: {x} != {y}"


# ---------------- 基本形狀 / 滾動 ---------------- #

def test_first_games_are_nan_and_prior_counts_increase():
    df = feats(*make_league(9))
    first = row(df, 1)
    assert first["home_games_prior"] == 0 and first["away_games_prior"] == 0
    assert math.isnan(first["home_ortg_r5"]) and math.isnan(first["home_rest_days"])
    assert row(df, 4)["home_games_prior"] >= 2


def test_rolling_mean_uses_only_previous_games_and_excludes_current():
    games, derived, players = make_league(30)
    df = feats(games, derived, players)
    g = games[20]
    prev = [x for x in games[:20] if g.home_team_id in (x.home_team_id, x.away_team_id)]
    last5 = [derived[(x.game_id, g.home_team_id)]["ortg"] for x in prev[-5:]]
    last10 = [derived[(x.game_id, g.home_team_id)]["ortg"] for x in prev[-10:]]
    r = row(df, g.game_id)
    assert r["home_ortg_r5"] == pytest.approx(np.mean(last5))
    assert r["home_ortg_r10"] == pytest.approx(np.mean(last10))
    current = derived[(g.game_id, g.home_team_id)]["ortg"]
    assert r["home_ortg_r5"] != pytest.approx(current)


def test_opponent_allowed_metrics_use_opponents_values_in_past_games():
    games, derived, players = make_league(30)
    df = feats(games, derived, players)
    g = games[20]
    tid = g.home_team_id
    prev = [x for x in games[:20] if tid in (x.home_team_id, x.away_team_id)][-5:]
    opp_efg = [derived[(x.game_id, x.away_team_id if x.home_team_id == tid else x.home_team_id)]["efg_pct"]
               for x in prev]
    assert row(df, g.game_id)["home_opp_efg_pct_r5"] == pytest.approx(np.mean(opp_efg))


def test_rest_days_and_season_game_no():
    games, derived, players = make_league(12, tip_step=timedelta(days=3))
    r = row(feats(games, derived, players), 10)
    assert r["home_rest_days"] >= 0 and r["home_season_game_no"] >= 2


def test_cross_season_rolling_continues_but_season_game_no_resets():
    g1, d1, p1 = make_league(12, season="2023-24")
    g2, d2, p2 = make_league(6, start=T0 + timedelta(days=400), season="2024-25")
    for g in g2:
        g.game_id += 100
    d2 = {(gid + 100, t): v for (gid, t), v in d2.items()}
    p2 = {gid + 100: v for gid, v in p2.items()}
    df = feats(g1 + g2, {**d1, **d2}, {**p1, **p2})
    first_new = row(df, 101)
    assert first_new["home_season_game_no"] == 0 and first_new["home_games_prior"] > 3
    assert not math.isnan(first_new["home_ortg_r5"])                      # 窗口跨賽季連續


# ---------------- 洩漏防護 ---------------- #

def test_no_leakage_guard_passes_on_built_features_and_fails_when_tampered():
    df = feats(*make_league(15))
    pf.assert_no_leakage(df)
    bad = df.copy()
    bad.loc[5, "home_asof_game_utc"] = bad.loc[5, "game_time_utc"]        # 用到「同時刻」的資料
    with pytest.raises(pf.LeakageError):
        pf.assert_no_leakage(bad)


def test_asof_game_is_strictly_before_tip_for_every_row():
    df = feats(*make_league(30))
    for side in ("home", "away"):
        c = df[f"{side}_asof_game_utc"]
        assert (c[c.notna()] < df.loc[c.notna(), "game_time_utc"]).all()


def test_future_data_does_not_change_past_features():
    """把某場「之後」的所有資料大幅竄改 / 整個移除，該場特徵必須完全相同。"""
    games, derived, players = make_league(30)
    full = feats(games, derived, players)
    cut = 15
    target = games[cut]
    cols = pf.feature_columns(full)

    truncated = [g for g in games if g.game_time_utc <= target.game_time_utc]
    trunc_df = feats(truncated, derived, players)
    same(row(full, target.game_id), row(trunc_df, target.game_id), cols)

    d2, p2 = copy.deepcopy(derived), copy.deepcopy(players)
    g2 = copy.deepcopy(games)
    for g in g2:
        if g.game_time_utc > target.game_time_utc:
            g.home_pts, g.away_pts = 1, 999
            for t in (g.home_team_id, g.away_team_id):
                d2[(g.game_id, t)] = {k: 9999.0 for k in d2[(g.game_id, t)]}
            p2[g.game_id] = [(t, pid, 48.0, False) for t, pid, _, _ in p2[g.game_id]]
    pert = feats(g2, d2, p2)
    same(row(full, target.game_id), row(pert, target.game_id), cols)


def test_own_game_result_does_not_affect_own_features():
    games, derived, players = make_league(20)
    base = feats(games, derived, players)
    g2 = copy.deepcopy(games)
    d2, p2 = copy.deepcopy(derived), copy.deepcopy(players)
    t = g2[12]
    t.home_pts, t.away_pts = 200, 0
    for team in (t.home_team_id, t.away_team_id):
        d2[(t.game_id, team)] = {k: 7777.0 for k in d2[(t.game_id, team)]}
    p2[t.game_id] = [(tm, pid, 1.0, True) for tm, pid, _, _ in p2[t.game_id]]
    other = feats(g2, d2, p2)
    cols = pf.feature_columns(base)
    same(row(base, t.game_id), row(other, t.game_id), cols)
    assert row(base, t.game_id)["y_margin"] != row(other, t.game_id)["y_margin"]     # 只有標籤不同


def test_games_with_identical_tip_do_not_see_each_other():
    """四隊兩場同時開打：兩場互不為對方的「歷史」。"""
    D_, E_ = 4, 5
    ABBR.update({D_: "DDD", E_: "EEE"})
    g = [pf.GameRecord(1, "2023-24", T0, A, B, "AAA", "BBB", 100, 90),
         pf.GameRecord(2, "2023-24", T0 + timedelta(days=1), D_, E_, "DDD", "EEE", 100, 90),
         pf.GameRecord(3, "2023-24", T0 + timedelta(days=1), A, C, "AAA", "CCC", 100, 90),   # 與 2 同時開賽
         pf.GameRecord(4, "2023-24", T0 + timedelta(days=2), A, D_, "AAA", "DDD", 100, 90)]
    derived = {(i, t): derived_for(i, t) for i, h, a in ((1, A, B), (2, D_, E_), (3, A, C), (4, A, D_)) for t in (h, a)}
    players = {i: lineup_rows(i, h) + lineup_rows(i, a) for i, h, a in ((1, A, B), (2, D_, E_), (3, A, C), (4, A, D_))}
    df = feats(g, derived, players)
    assert row(df, 3)["home_games_prior"] == 1                            # 只看到 game 1，看不到同時開賽的 game 2
    assert row(df, 4)["away_games_prior"] == 1 and row(df, 4)["home_games_prior"] == 2
    pf.assert_no_leakage(df)


def test_input_order_does_not_matter():
    games, derived, players = make_league(20)
    a = feats(games, derived, players)
    b = feats(list(reversed(games)), derived, players)
    pd.testing.assert_frame_equal(a, b)


def test_labels_are_excluded_from_feature_columns():
    df = feats(*make_league(12))
    cols = pf.feature_columns(df)
    assert not any(c.startswith("y_") or c.endswith("_asof_game_utc") for c in cols)
    assert {"game_id", "season", "game_time_utc", "home_team_id"}.isdisjoint(cols)
    assert {"y_home_win", "y_margin", "y_total", "y_h1_margin", "y_h1_total"} <= set(df.columns)
    r = row(df, 1)
    assert (r["y_home_win"], r["y_margin"], r["y_total"], r["y_h1_margin"], r["y_h1_total"]) == (1, 5, 195, 2, 98)


# ---------------- 輪替 / 先發分鐘 ---------------- #

def test_rotation_features_from_previous_games_only():
    games, derived, players = make_league(30)
    r = row(feats(games, derived, players), 25)
    mins = (34, 32, 30, 28, 26, 20, 16, 12, 8, 6)
    assert r["home_starter_min_r5"] == pytest.approx(np.mean(mins[:5]))
    assert r["home_rot_size_r10"] == pytest.approx(8)                      # ≥10 分鐘者 8 人（34..12）
    assert r["home_top8_min_share_r10"] == pytest.approx(sum(sorted(mins, reverse=True)[:8]) / sum(mins))
    assert r["home_bench_min_share_r10"] == pytest.approx(sum(mins[5:]) / sum(mins))


# ---------------- 傷病 ---------------- #

def build_index(reports):
    """reports: [(id, time, {(date_iso, abbr): [(player_id, status)]})]；未列在 dict 的球隊視為未涵蓋。"""
    reps, ents = [], []
    for rid, when, teams in reports:
        cov = {}
        for (iso, abbr), pl in teams.items():
            cov.setdefault(iso, {})[abbr] = {"n": len(pl), "nys": False, "implied": not pl}
        reps.append({"id": rid, "report_time_utc": when, "coverage": cov})
        tid = {v: k for k, v in ABBR.items()}
        for (iso, abbr), pl in teams.items():
            for pid, status in pl:
                ents.append({"report_id": rid, "game_date": date.fromisoformat(iso), "team_id": tid[abbr],
                             "player_id": pid, "player_name": f"P{pid}", "status": status, "reason": None})
    return ia.build_index(reps, ents, ABBR)


def game_index(games, gid):
    return next(i for i, g in enumerate(games) if g.game_id == gid)


def test_injury_minutes_weighted_impact_and_expected_starters():
    games, derived, players = make_league(30)
    g = games[24]                                                          # 主隊 = (24%3==0) → A
    home = g.home_team_id
    star, role = home * 100 + 0, home * 100 + 5                           # 34 分鐘先發 / 20 分鐘輪替
    iso = g.game_time_utc.astimezone(ia.ET).date().isoformat()
    idx = build_index([(1, g.game_time_utc - timedelta(hours=3),
                        {(iso, ABBR[home]): [(star, "Out"), (role, "Questionable"), (home * 100 + 9, "Available")],
                         (iso, ABBR[g.away_team_id]): []})])
    r = row(feats(games, derived, players, idx), g.game_id)
    assert r["home_inj_known"] == 1.0 and r["away_inj_known"] == 1.0
    assert r["home_inj_n_out"] == 1 and r["home_inj_n_questionable"] == 1
    assert r["home_inj_min_lost"] == pytest.approx(1.0 * 34 + 0.5 * 20)
    assert r["home_inj_min_lost_share"] == pytest.approx((34 + 10) / 240)
    assert r["home_exp_starters_n_out"] == 1 and r["home_exp_starters_absent_w"] == pytest.approx(1.0)
    assert r["home_exp_starters_min_lost_share"] == pytest.approx(34 / 240)
    assert r["away_inj_min_lost"] == 0.0 and r["away_exp_starters_absent_w"] == 0.0   # 已申報、無人列 → 0（不是 NaN）
    assert r["inj_min_lost_share_diff"] == pytest.approx((34 + 10) / 240)
    assert 170 < r["home_inj_report_age_min"] < 190


def test_no_covering_report_is_unknown_not_healthy():
    games, derived, players = make_league(30)
    g = games[24]
    r = row(feats(games, derived, players, build_index([])), g.game_id)
    assert r["home_inj_known"] == 0.0 and math.isnan(r["home_inj_min_lost"]) and math.isnan(r["home_exp_starters_absent_w"])
    r2 = row(feats(games, derived, players, None), g.game_id)               # 沒傳 index 也一樣
    assert r2["home_inj_known"] == 0.0 and math.isnan(r2["home_inj_n_out"])


def test_injury_report_after_tip_is_ignored_and_offset_selects_earlier_report():
    games, derived, players = make_league(30)
    g = games[24]
    home = g.home_team_id
    iso = g.game_time_utc.astimezone(ia.ET).date().isoformat()
    star = home * 100
    idx = build_index([
        (1, g.game_time_utc - timedelta(hours=4), {(iso, ABBR[home]): [(star, "Questionable")], (iso, ABBR[g.away_team_id]): []}),
        (2, g.game_time_utc - timedelta(minutes=30), {(iso, ABBR[home]): [(star, "Out")], (iso, ABBR[g.away_team_id]): []}),
        (3, g.game_time_utc + timedelta(minutes=5), {(iso, ABBR[home]): [(star, "Available")], (iso, ABBR[g.away_team_id]): []}),
    ])
    at_tip = row(feats(games, derived, players, idx), g.game_id)
    assert at_tip["home_inj_min_lost"] == pytest.approx(34.0)              # 最後一份「開賽前」＝報告 2（Out）
    early = row(feats(games, derived, players, idx, decision_offset_min=60), g.game_id)
    assert early["home_inj_min_lost"] == pytest.approx(17.0)               # 開賽前 60 分鐘決策 → 報告 1（Questionable）
    pf.assert_no_leakage(feats(games, derived, players, idx))


def test_disappeared_injured_player_no_longer_counts_as_lost_minutes():
    games, derived, players = make_league(30)
    g = games[24]
    home = g.home_team_id
    iso = g.game_time_utc.astimezone(ia.ET).date().isoformat()
    star, other = home * 100, home * 100 + 7
    idx = build_index([
        (1, g.game_time_utc - timedelta(hours=5), {(iso, ABBR[home]): [(star, "Out")], (iso, ABBR[g.away_team_id]): []}),
        (2, g.game_time_utc - timedelta(hours=1), {(iso, ABBR[home]): [(other, "Probable")], (iso, ABBR[g.away_team_id]): []}),
    ])
    r = row(feats(games, derived, players, idx), g.game_id)
    assert r["home_inj_min_lost"] == pytest.approx(0.15 * 12)              # 只剩 Probable 的 other（12 分鐘）
    assert r["home_exp_starters_n_out"] == 0                               # star 已不在名單上


def test_injury_for_other_game_date_is_not_used():
    games, derived, players = make_league(30)
    g = games[24]
    home = g.home_team_id
    other_iso = (g.game_time_utc.astimezone(ia.ET).date() + timedelta(days=1)).isoformat()
    idx = build_index([(1, g.game_time_utc - timedelta(hours=3), {(other_iso, ABBR[home]): [(home * 100, "Out")]})])
    r = row(feats(games, derived, players, idx), g.game_id)
    assert r["home_inj_known"] == 0.0
