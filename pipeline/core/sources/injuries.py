"""
NBA 官方傷病報告 fetcher（套件 nbainjuries，解析官方 PDF）
------------------------------------------------------------
規格書 §1.1：2021-22 起有資料；賽前一日 17:00（當地，ET）前申報，
賽前會多次更新，需高頻重抓。官方報告時間點固定為每天
凌晨/上午/下午/傍晚幾個整點（如 05PM/08PM/10PM/12AM...），
本模組只需給某個時間點 timestamp，套件會自動組出對應 PDF 網址。

需要本機/容器有 Java 執行環境（套件透過 jpype+tabula 解析 PDF）。
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

log = logging.getLogger(__name__)


def check_report_valid(ts: datetime) -> bool:
    from nbainjuries import injury

    try:
        return bool(injury.check_reportvalid(ts))
    except Exception as e:
        log.debug("injury report 尚未發布 %s: %s", ts, e)
        return False


def fetch_injury_report(ts: datetime) -> list[dict]:
    """回傳 [{game_date, matchup, team, player_name, status, reason}]"""
    from nbainjuries import injury

    df: pd.DataFrame = injury.get_reportdata(ts, return_df=True)
    if df is None or df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        out.append({
            "game_date": row.get("Game Date"),
            "matchup": row.get("Matchup"),
            "team": row.get("Team"),
            "player_name": row.get("Player Name"),
            "status": row.get("Current Status"),
            "reason": row.get("Reason"),
        })
    return out
