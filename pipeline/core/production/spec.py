"""
Production 模型規格（C.5C 在驗證賽季 2023-24 選定，見 docs/phase-c5c-report.md §14）
------------------------------------------------------------
這裡只「固定」C.5C 的選擇，C.5D 不重新選模型 / 調參：
  target → (模型族, 特徵組, 超參數, 訓練起始賽季)

決策時點 profile（C.5C §12 的 early / T-60 是兩份各自訓練、各自評測的設定）：
  early  傷病 cutoff = ET 賽事日 00:00（台灣中午排程只看得到前一晚的報告）
  final  傷病 cutoff = 開賽前 60 分鐘
每個 profile 用「與推論時點一致」的傷病特徵訓練、並帶自己的傷病校準狀態（early 報告比較不可靠：Doubtful 93% vs 98%）。
推論時：距開賽 ≥ 3 小時 → early；< 3 小時 → final（3 小時 = InjuryCalibrator 的時距分桶界線）。
"""
from __future__ import annotations

from datetime import timedelta

MODEL_VERSION = "ml-v2.0"            # 寫入 predictions.model_version（C.5C 選定模型 + pregame-v2.1 特徵）
ARTIFACT_SCHEMA_VERSION = 1          # artifact 檔案格式版本（loader 只接受 SUPPORTED_SCHEMA_VERSIONS）
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

PRODUCTION_SPEC: dict[str, dict] = {
    "win":       {"family": "logistic", "group": "E",       "hp": 0.01,   "train_start": "2021-22"},
    "margin":    {"family": "ridge",    "group": "E",       "hp": 1000.0, "train_start": "2021-22"},
    "total":     {"family": "ridge",    "group": "E_count", "hp": 1.0,    "train_start": "2021-22"},
    "h1_margin": {"family": "ridge",    "group": "E_count", "hp": 1000.0, "train_start": "2021-22"},
    "h1_total":  {"family": "ridge",    "group": "E_count", "hp": 1.0,    "train_start": "2021-22"},
}
TARGET_Y = {"win": "y_home_win", "margin": "y_margin", "total": "y_total", "h1_margin": "y_h1_margin",
            "h1_total": "y_h1_total"}

PROFILES: dict[str, str] = {"early": "early", "final": "T-60"}     # profile → temporal_features timing
FINAL_PROFILE_MAX_LEAD = timedelta(hours=3)

# 重訓：只用「開賽時間早於 cutoff − 4 小時」且已 final 的比賽（確保比賽在 cutoff 前已結束、結果在 cutoff 前可知）
TRAINING_GAME_BUFFER = timedelta(hours=4)


def profile_for_lead(lead: timedelta) -> str:
    return "final" if lead < FINAL_PROFILE_MAX_LEAD else "early"


# 前端「特徵拆解」標籤（只列 production 特徵組會用到的欄位）
FEATURE_LABELS: dict[str, str] = {
    "elo_diff": "Elo 差距", "b2b_diff": "背靠背差（主−客）", "margin10_diff": "近10場分差差距（跨季）",
    "rest_diff": "休息天數差", "form10_diff": "近10場勝率差（跨季）",
    "est_net_rtg_blend_diff": "本季淨效率（含上季先驗）差", "margin_blend_diff": "本季分差（含上季先驗）差",
    "est_net_rtg_l10_diff": "近10場淨效率差", "est_net_rtg_l5_diff": "近5場淨效率差",
    "est_net_rtg_trend10_diff": "淨效率趨勢（近10−本季）差", "efg_pct_season_diff": "本季 eFG% 差",
    "tov_pct_season_diff": "本季失誤率差", "orb_pct_season_diff": "本季進攻籃板率差",
    "opp_efg_pct_season_diff": "本季對手 eFG% 差", "h1_margin_blend_diff": "本季上半場分差（含先驗）差",
    "h2h_season_margin_f": "本季交手平均分差", "est_net_rtg_blend_roster_diff": "淨效率（名單先驗）差",
    "ret_min_pct_diff": "名單延續性差", "starter_continuity_diff": "先發延續性差",
    "roster_prior_pm48_diff": "名單上季正負值差",
    "inj_min_lost_recent_diff": "傷病：近期分鐘損失差", "inj_min_lost_role_diff": "傷病：角色分鐘損失差",
    "inj_exp_starters_avail_diff": "傷病：預期先發可出賽差", "inj_rotation_avail_pct_diff": "傷病：輪替可用率差",
    "inj_top3_absent_w_diff": "傷病：前三主力缺陣差", "inj_n_out_diff": "傷病：Out 人數差",
    "total_est_f": "近10場攻守總分估計", "h1_total_est_f": "近10場上半場總分估計", "rest_sum": "雙方休息天數和",
    "b2b_sum": "雙方背靠背數", "is_playoffs": "季後賽", "exp_pts_blend_sum": "本季期望得分和（含先驗）",
    "exp_pace_blend": "期望節奏（含先驗）", "exp_total_poss_blend": "節奏×效率期望總分",
    "pace_l10_sum": "近10場節奏和", "pts_trend10_sum": "得分趨勢和", "league_pts_prev_f": "上季聯盟平均得分",
    "games_7d_sum": "近7天場次和", "ret_min_pct_sum": "名單延續性和", "inj_n_out_sum": "傷病：Out 人數和",
    "exp_h1_blend_sum": "本季上半場期望得分和（含先驗）",
}
