"""pregame-v2（C.5C）：賽季邊界、本季窗口、上季先驗、名單延續性、傷病 walk-forward 校準、球員分鐘洩漏、決策時點"""
import copy
import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from core import injury_asof as ia
from core.injury_history import NOT_LISTED
from core.models import temporal_features as tf
from core.models.pregame_features import GameRecord, LeakageError
from core.timeutil import ET, UTC

T0 = datetime(2023, 10, 25, 23, 30, tzinfo=UTC)
A, B, C = 1, 2, 3
ABBR = {A: "AAA", B: "BBB", C: "CCC"}


def derived_for(gid, tid):
    base = 100.0 + gid * 0.5 + tid
    return {"pace": 95 + gid * 0.1, "ortg": base, "drtg": base - 2, "net_rtg": 2.0 + 0.01 * gid,
            "efg_pct": 0.5 + 0.001 * gid, "ts_pct": 0.56, "tov_pct": 0.13, "orb_pct": 0.25, "drb_pct": 0.75,
            "ftr": 0.2, "fg3a_rate": 0.4}


def roster(tid, season_idx=0, mins=(34, 32, 30, 28, 26, 20, 16, 12, 8, 6), swap=0):
    """10 位球員（前 5 先發）；swap = 換掉前幾位（模擬交易 / 名單變動）；player_id 依賽季可不同。"""
    out = []
    for i, m in enumerate(mins):
        pid = tid * 1000 + i + (500 + season_idx * 50 if i < swap else 0)
        out.append((tid, pid, float(m), i < 5, 1.0 if i < 5 else -1.0))
    return out


def make_season(season, start, n_games, *, gid0=0, swaps=None, season_idx=0, step=timedelta(days=2)):
    swaps = swaps or {}
    games, derived, players = [], {}, {}
    matchups = [(A, B), (B, C), (C, A)]
    for i in range(n_games):
        h, a = matchups[i % 3]
        gid = gid0 + i + 1
        tip = start + i * step
        games.append(GameRecord(gid, season, tip, h, a, ABBR[h], ABBR[a], 100 + (i % 9), 96 + (i % 5), 52, 49))
        derived[(gid, h)], derived[(gid, a)] = derived_for(gid, h), derived_for(gid, a)
        players[gid] = roster(h, season_idx, swap=swaps.get(h, 0)) + roster(a, season_idx, swap=swaps.get(a, 0))
    return games, derived, players


def two_seasons(n1=30, n2=30, swaps2=None):
    g1, d1, p1 = make_season("2023-24", T0, n1)
    g2, d2, p2 = make_season("2024-25", T0 + timedelta(days=370), n2, gid0=1000, swaps=swaps2, season_idx=1)
    return g1 + g2, {**d1, **d2}, {**p1, **p2}


def build(games, derived, players, index=None, **kw):
    return tf.build_temporal_features(games, derived, players, index, **kw)


def row(df, gid):
    return df[df.game_id == gid].iloc[0]


def team_games(games, tid, season=None, before=None):
    return [g for g in games if tid in (g.home_team_id, g.away_team_id)
            and (season is None or g.season == season) and (before is None or g.game_time_utc < before)]


def side_val(g, tid, derived, metric):
    if metric == "margin":
        return (g.home_pts - g.away_pts) if g.home_team_id == tid else (g.away_pts - g.home_pts)
    return derived[(g.game_id, tid)][{"est_off_rtg": "ortg", "est_net_rtg": "net_rtg"}.get(metric, metric)]


def same(a, b, cols):
    for c in cols:
        x, y = a[c], b[c]
        if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
            continue
        assert x == y, f"{c}: {x} != {y}"


# ---------------- 賽季邊界 ---------------- #

def test_season_windows_reset_at_season_boundary_and_prev_is_last_season_mean():
    games, derived, players = two_seasons()
    df = build(games, derived, players)
    first_new = next(g for g in games if g.season == "2024-25" and g.home_team_id == A)
    r = row(df, first_new.game_id)
    assert r["home_gp"] == 0
    for w in ("season", "l20", "l10", "l5"):
        assert math.isnan(r[f"home_est_off_rtg_{w}"])                      # 本季窗口不跨季
    prev = [side_val(g, A, derived, "est_off_rtg") for g in team_games(games, A, "2023-24")]
    assert r["home_est_off_rtg_prev"] == pytest.approx(np.mean(prev))
    assert r["home_prev_n"] == len(prev)


def test_prev_is_missing_when_previous_season_absent():
    g1, d1, p1 = make_season("2022-23", T0, 12)
    g2, d2, p2 = make_season("2024-25", T0 + timedelta(days=740), 12, gid0=1000)    # 中間缺一季
    df = build(g1 + g2, {**d1, **d2}, {**p1, **p2})
    r = row(df, 1001)
    assert math.isnan(r["home_est_net_rtg_prev"]) and r["home_prev_n"] == 0


def test_league_prev_mean_is_previous_season_league_average():
    games, derived, players = two_seasons()
    df = build(games, derived, players)
    s1 = [g for g in games if g.season == "2023-24"]
    pts = [x for g in s1 for x in (g.home_pts, g.away_pts)]
    r = row(df, 1001)
    assert r["league_pts_prev"] == pytest.approx(np.mean(pts))
    assert math.isnan(row(df, 1)["league_pts_prev"])


# ---------------- 本季窗口 ---------------- #

def test_current_season_rolling_windows_and_min_periods():
    games, derived, players = two_seasons(n2=60)
    df = build(games, derived, players)
    target = [g for g in games if g.season == "2024-25"][50]
    tid = target.home_team_id
    hist = [side_val(g, tid, derived, "est_off_rtg") for g in team_games(games, tid, "2024-25", target.game_time_utc)]
    r = row(df, target.game_id)
    assert r["home_gp"] == len(hist)
    assert r["home_est_off_rtg_season"] == pytest.approx(np.mean(hist))
    for w, n in (("l20", 20), ("l10", 10), ("l5", 5)):
        assert r[f"home_est_off_rtg_{w}"] == pytest.approx(np.mean(hist[-n:]))
    margins = [side_val(g, tid, derived, "margin") for g in team_games(games, tid, "2024-25", target.game_time_utc)]
    assert r["home_margin_l5"] == pytest.approx(np.mean(margins[-5:]))

    early = [g for g in games if g.season == "2024-25"][4]                # 主隊本季已賽 2~3 場
    re_ = row(df, early.game_id)
    assert re_["home_gp"] < 5
    assert not math.isnan(re_["home_est_off_rtg_season"]) and math.isnan(re_["home_est_off_rtg_l10"])


def test_current_game_result_does_not_enter_its_own_windows():
    games, derived, players = two_seasons()
    base = build(games, derived, players)
    g2, d2, p2 = copy.deepcopy(games), copy.deepcopy(derived), copy.deepcopy(players)
    t = next(g for g in g2 if g.game_id == 1020)
    t.home_pts, t.away_pts, t.home_h1, t.away_h1 = 200, 10, 120, 5
    for tid in (t.home_team_id, t.away_team_id):
        d2[(t.game_id, tid)] = {k: 999.0 for k in d2[(t.game_id, tid)]}
    p2[t.game_id] = [(tm, pid, 48.0, False, 50.0) for tm, pid, *_ in p2[t.game_id]]
    other = build(g2, d2, p2)
    cols = tf.feature_columns(base)
    same(row(base, 1020), row(other, 1020), cols)


def test_future_data_does_not_change_past_features_and_order_invariance():
    games, derived, players = two_seasons()
    full = build(games, derived, players)
    target = next(g for g in games if g.game_id == 1010)
    cols = tf.feature_columns(full)
    trunc = build([g for g in games if g.game_time_utc <= target.game_time_utc], derived, players)
    same(row(full, 1010), row(trunc, 1010), cols)
    pd.testing.assert_frame_equal(full, build(list(reversed(games)), derived, players))


def test_identical_tip_games_do_not_see_each_other():
    D_, E_ = 4, 5
    abbr = {**ABBR, D_: "DDD", E_: "EEE"}
    g = [GameRecord(1, "2023-24", T0, A, B, "AAA", "BBB", 100, 90),
         GameRecord(2, "2023-24", T0 + timedelta(days=1), D_, E_, "DDD", "EEE", 100, 90),
         GameRecord(3, "2023-24", T0 + timedelta(days=1), A, D_, "AAA", "DDD", 100, 90)]
    d = {(i, t): derived_for(i, t) for i, h, a in ((1, A, B), (2, D_, E_), (3, A, D_)) for t in (h, a)}
    p = {1: roster(A) + roster(B), 2: roster(D_) + roster(E_), 3: roster(A) + roster(D_)}
    df = build(g, d, p)
    assert row(df, 3)["away_gp"] == 0 and row(df, 3)["home_gp"] == 1
    tf.assert_no_leakage(df)
    assert abbr[D_] == "DDD"


# ---------------- 名單延續性 / 上季先驗 ---------------- #

def test_roster_continuity_full_and_none_and_unknown_before_first_game():
    games, derived, players = two_seasons(swaps2={B: 10})        # B 隊整隊換血，A、C 原班人馬
    df = build(games, derived, players)
    s2 = [g for g in games if g.season == "2024-25"]
    first_a = next(g for g in s2 if A in (g.home_team_id, g.away_team_id))
    side = "home" if first_a.home_team_id == A else "away"
    r0 = row(df, first_a.game_id)
    assert math.isnan(r0[f"{side}_ret_min_pct"])                           # 本季還沒比賽 → 未知，不補值
    later = s2[20]
    r = row(df, later.game_id)
    for s, tid in (("home", later.home_team_id), ("away", later.away_team_id)):
        expect = 0.0 if tid == B else 1.0
        assert r[f"{s}_ret_min_pct"] == pytest.approx(expect)
        assert r[f"{s}_ret_starter_min_pct"] == pytest.approx(expect)
        assert r[f"{s}_starter_continuity"] == pytest.approx(expect)
        assert r[f"{s}_rotation_continuity"] == pytest.approx(expect)
        assert r[f"{s}_prev_min_returning_pct"] == pytest.approx(expect)


def test_partial_continuity_counts_minutes_not_players():
    games, derived, players = two_seasons(swaps2={A: 1})          # 只換掉 A 隊分鐘最多的 1 人（34 分鐘）
    df = build(games, derived, players)
    later = next(g for g in games if g.season == "2024-25" and g.home_team_id == A and g.game_id > 1012)
    r = row(df, later.game_id)
    total = sum((34, 32, 30, 28, 26, 20, 16, 12, 8, 6))
    assert r["home_ret_min_pct"] == pytest.approx((total - 34) / total)
    assert r["home_starter_continuity"] == pytest.approx(4 / 5)


def test_roster_prior_uses_previous_season_player_plus_minus_with_shrinkage():
    games, derived, players = two_seasons()
    df = build(games, derived, players)
    later = next(g for g in games if g.season == "2024-25" and g.home_team_id == A and g.game_id > 1012)
    r = row(df, later.game_id)
    n_prev = len(team_games(games, A, "2023-24"))
    mins = (34, 32, 30, 28, 26, 20, 16, 12, 8, 6)
    total = sum(mins)
    expect = sum((m / total) * 48 * ((1.0 if i < 5 else -1.0) * n_prev) / (m * n_prev + tf.PM_SHRINK_MIN)
                 for i, m in enumerate(mins))
    assert r["home_roster_prior_pm48"] == pytest.approx(expect)


# ---------------- 傷病：walk-forward 校準 / 決策時點 ---------------- #

def injury_index(reports):
    """reports: [(id, time, {(date_iso, abbr): [(player_id, status)]})]"""
    reps, ents = [], []
    tid_of = {v: k for k, v in ABBR.items()}
    for rid, when, teams in reports:
        cov = {}
        for (iso, abbr), pl in teams.items():
            cov.setdefault(iso, {})[abbr] = {"n": len(pl), "nys": False, "implied": not pl}
            for pid, status in pl:
                ents.append({"report_id": rid, "game_date": date.fromisoformat(iso), "team_id": tid_of[abbr],
                             "player_id": pid, "player_name": f"P{pid}", "status": status, "reason": None})
        reps.append({"id": rid, "report_time_utc": when, "coverage": cov})
    return ia.build_index(reps, ents, ABBR)


def questionable_every_game(games, pid_of_team, lead=timedelta(hours=2)):
    reps = []
    for i, g in enumerate(games):
        iso = g.game_time_utc.astimezone(ET).date().isoformat()
        reps.append((i + 1, g.game_time_utc - lead,
                     {(iso, ABBR[g.home_team_id]): [(pid_of_team(g.home_team_id), "Questionable")],
                      (iso, ABBR[g.away_team_id]): []}))
    return injury_index(reps)


def test_calibrator_starts_at_prior_and_learns_only_from_finished_games():
    games, derived, players = make_season("2023-24", T0, 40)
    star = lambda tid: tid * 1000                                             # 每場都列 Questionable 的先發
    # 讓被列 Questionable 的主隊先發「實際都沒上場」
    p2 = {gid: [r for r in rows if not (r[1] == star(r[0]) and r[0] == g.home_team_id)]
          for g in games for gid, rows in [(g.game_id, players[g.game_id])]}
    idx = questionable_every_game(games, star)
    df, cal = build(games, derived, p2, idx, timing="T-60", return_calibrator=True)
    tf.assert_no_leakage(df)
    first = row(df, 1)
    assert first["calib_n_obs"] == 0 and pd.isna(first["calib_asof_utc"])
    log = pd.DataFrame(cal.log, columns=["season", "status", "lead_h", "p", "absent"])
    q = log[log["status"] == "Questionable"]
    assert q["p"].iloc[0] == pytest.approx(tf.PRIOR_P_ABSENT["Questionable"])
    assert q["p"].is_monotonic_increasing and q["p"].iloc[-1] > 0.8          # 只往觀測到的缺陣率移動
    assert (df["calib_asof_utc"].dropna() < df.loc[df["calib_asof_utc"].notna(), "game_time_utc"]).all()


def test_calibration_is_strictly_walk_forward_future_participation_does_not_change_past_features():
    games, derived, players = make_season("2023-24", T0, 40)
    star = lambda tid: tid * 1000
    idx = questionable_every_game(games, star)
    base = build(games, derived, players, idx)
    cut = 25
    p2 = copy.deepcopy(players)
    for g in games[cut:]:                                                      # 未來比賽：被列者全部缺陣
        p2[g.game_id] = [r for r in p2[g.game_id] if r[1] != star(r[0])]
    other = build(games, derived, p2, idx)
    cols = tf.feature_columns(base)
    for g in games[:cut + 1]:                                                  # 含第 cut 場本身
        same(row(base, g.game_id), row(other, g.game_id), cols)
    assert row(base, games[-1].game_id)["home_inj_min_lost_role"] != row(other, games[-1].game_id)["home_inj_min_lost_role"]


def test_fixed_probabilities_control_uses_constants():
    games, derived, players = make_season("2023-24", T0, 30)
    idx = questionable_every_game(games, lambda tid: tid * 1000)
    df = build(games, derived, players, idx, fixed_p_absent={"Questionable": 0.5})
    r = row(df, games[20].game_id)
    assert r["home_inj_min_lost_role"] == pytest.approx(0.5 * 34)


def test_calibrator_bucket_shrinks_toward_status_level():
    cal = tf.InjuryCalibrator()
    when = T0
    for _ in range(100):
        cal.observe("Doubtful", 1.0, True, when)
    assert cal.p_status("Doubtful") == pytest.approx((100 + tf.M_STATUS * 0.75) / (100 + tf.M_STATUS))
    assert cal.p_absent("Doubtful", 1.0) > cal.p_absent("Doubtful", 10.0) > 0.75   # 沒有 ge3h 觀測 → 向狀態層收縮


def test_minutes_weighted_injury_distinguishes_core_from_fringe_player():
    games, derived, players = make_season("2023-24", T0, 30)
    g = games[24]
    home = g.home_team_id
    iso = g.game_time_utc.astimezone(ET).date().isoformat()
    core_, fringe = home * 1000 + 0, home * 1000 + 9                           # 34 分鐘先發 vs 6 分鐘板凳
    mk = lambda pid: injury_index([(1, g.game_time_utc - timedelta(hours=3),
                                    {(iso, ABBR[home]): [(pid, "Out")], (iso, ABBR[g.away_team_id]): []})])
    a = row(build(games, derived, players, mk(core_), fixed_p_absent={"Out": 1.0}), g.game_id)
    b = row(build(games, derived, players, mk(fringe), fixed_p_absent={"Out": 1.0}), g.game_id)
    assert a["home_inj_n_out"] == b["home_inj_n_out"] == 1                     # 人數相同
    assert a["home_inj_min_lost_role"] == pytest.approx(34) and b["home_inj_min_lost_role"] == pytest.approx(6)
    assert a["home_inj_exp_starters_avail"] == pytest.approx(4) and b["home_inj_exp_starters_avail"] == pytest.approx(5)
    assert a["home_inj_rotation_avail_pct"] < b["home_inj_rotation_avail_pct"] == pytest.approx(1.0)
    assert a["home_inj_top3_absent_w"] == pytest.approx(1.0) and b["home_inj_top3_absent_w"] == 0


def test_player_importance_uses_only_pregame_minutes():
    """改變「這一場」的球員分鐘（含受傷球員本場有沒有上場）不影響本場傷病特徵。"""
    games, derived, players = make_season("2023-24", T0, 30)
    g = games[24]
    home = g.home_team_id
    iso = g.game_time_utc.astimezone(ET).date().isoformat()
    idx = injury_index([(1, g.game_time_utc - timedelta(hours=3),
                         {(iso, ABBR[home]): [(home * 1000, "Questionable")], (iso, ABBR[g.away_team_id]): []})])
    base = build(games, derived, players, idx)
    p2 = copy.deepcopy(players)
    p2[g.game_id] = [(t, pid, 48.0 if pid == home * 1000 else 1.0, s, 0.0) for t, pid, _, s, _ in p2[g.game_id]]
    other = build(games, derived, p2, idx)
    inj_cols = [c for c in tf.feature_columns(base) if "inj_" in c]
    same(row(base, g.game_id), row(other, g.game_id), inj_cols)


def test_recent_minutes_missing_ignores_player_already_out_for_weeks():
    """近期本來就沒上場的球員（長期缺陣）：相對滿編有分鐘損失，但相對近期常態幾乎沒有。"""
    games, derived, players = make_season("2023-24", T0, 45)
    home_team = games[42].home_team_id
    long_out = home_team * 1000
    p2 = copy.deepcopy(players)
    for g in games[20:]:
        p2[g.game_id] = [r for r in p2[g.game_id] if r[1] != long_out]
    g = games[42]
    iso = g.game_time_utc.astimezone(ET).date().isoformat()
    idx = injury_index([(1, g.game_time_utc - timedelta(hours=3),
                         {(iso, ABBR[home_team]): [(long_out, "Out")], (iso, ABBR[g.away_team_id]): []})])
    r = row(build(games, derived, p2, idx, fixed_p_absent={"Out": 1.0}), g.game_id)
    assert r["home_inj_min_lost_role"] == pytest.approx(34)
    assert r["home_inj_min_lost_recent"] == pytest.approx(0)


def test_unknown_injury_state_is_nan_not_zero():
    games, derived, players = make_season("2023-24", T0, 20)
    r = row(build(games, derived, players, injury_index([])), games[15].game_id)
    assert r["home_inj_known"] == 0 and math.isnan(r["home_inj_min_lost_role"]) and math.isnan(r["home_inj_exp_starters_avail"])


def test_prediction_cutoff_report_after_cutoff_is_ignored_and_early_uses_previous_evening():
    games, derived, players = make_season("2023-24", T0, 30)
    g = games[24]
    home = g.home_team_id
    iso = g.game_time_utc.astimezone(ET).date().isoformat()
    star = home * 1000
    tip = g.game_time_utc
    midnight_et = datetime.combine(tip.astimezone(ET).date(), datetime.min.time(), tzinfo=ET).astimezone(UTC)
    idx = injury_index([
        (1, midnight_et - timedelta(hours=5), {(iso, ABBR[home]): [(star, "Questionable")], (iso, ABBR[g.away_team_id]): []}),
        (2, tip - timedelta(minutes=90), {(iso, ABBR[home]): [(star, "Out")], (iso, ABBR[g.away_team_id]): []}),
        (3, tip - timedelta(minutes=10), {(iso, ABBR[home]): [], (iso, ABBR[g.away_team_id]): []}),
        (4, tip + timedelta(minutes=5), {(iso, ABBR[home]): [(star, "Available")], (iso, ABBR[g.away_team_id]): []}),
    ])
    fixed = {"Questionable": 0.5, "Out": 1.0}
    early = row(build(games, derived, players, idx, timing="early", fixed_p_absent=fixed), g.game_id)
    t60 = row(build(games, derived, players, idx, timing="T-60", fixed_p_absent=fixed), g.game_id)
    t0 = row(build(games, derived, players, idx, timing=0, fixed_p_absent=fixed), g.game_id)
    assert early["home_inj_min_lost_role"] == pytest.approx(0.5 * 34)       # 前一晚的 Questionable
    assert t60["home_inj_min_lost_role"] == pytest.approx(34)               # T-90 的 Out
    assert t0["home_inj_min_lost_role"] == pytest.approx(0)                 # T-10 已不在名單；開賽後報告不用
    for r in (early, t60):
        assert r["inj_cutoff_utc"] < r["game_time_utc"]
    assert t0["inj_cutoff_utc"] == t0["game_time_utc"]


def test_make_cutoff_variants_and_invalid():
    tip = datetime(2024, 1, 10, 0, 30, tzinfo=UTC)                           # ET 1/9 19:30
    assert tf.make_cutoff("T-60")(tip) == tip - timedelta(minutes=60)
    assert tf.make_cutoff(15)(tip) == tip - timedelta(minutes=15)
    assert tf.make_cutoff("early")(tip) == datetime(2024, 1, 9, 5, 0, tzinfo=UTC)   # ET 1/9 00:00
    with pytest.raises(ValueError):
        tf.make_cutoff("tomorrow")


def test_cutoff_not_before_tip_raises():
    games, derived, players = make_season("2023-24", T0, 3)
    with pytest.raises(LeakageError):
        build(games, derived, players, timing=-5)


def test_leakage_guard_detects_tampering():
    games, derived, players = two_seasons()
    df = build(games, derived, players)
    tf.assert_no_leakage(df)
    for col, delta in (("home_asof_game_utc", 0), ("calib_asof_utc", 0), ("inj_cutoff_utc", 1)):
        bad = df.copy()
        bad.loc[10, col] = bad.loc[10, "game_time_utc"] + timedelta(minutes=delta)
        with pytest.raises(LeakageError):
            tf.assert_no_leakage(bad)


def test_est_naming_and_no_label_in_features():
    games, derived, players = two_seasons(n1=12, n2=6)
    df = build(games, derived, players)
    cols = tf.feature_columns(df)
    assert not any(c.startswith("y_") or c.endswith("asof_game_utc") for c in cols)
    assert not any(c.startswith(("home_pace", "home_ortg", "home_drtg")) for c in cols)
    assert {"home_est_pace_season", "home_est_off_rtg_l10", "home_est_def_rtg_prev"} <= set(cols)
    assert NOT_LISTED in tf.PRIOR_P_ABSENT
