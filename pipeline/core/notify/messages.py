"""
推播訊息格式（notify-v1；純函式，不碰 DB / 網路）
------------------------------------------------------------
所有數字都來自 Python 已物化的結果（decision_opportunities / predictions / readiness checks），這裡只排版。
用語沿用決策中心：Qualified opportunity / Positive EV / 理論金額 —— 不寫「推薦」「必中」，每則附免責提醒。
動態文字一律 html.escape（Telegram HTML parse mode）。
"""
from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Any, Iterable

from ..timeutil import TPE, ensure_utc

MARKET = {"ml": "不讓分", "spread": "讓分", "total": "大小分", "h1_ml": "上半場獨贏", "h1_spread": "上半場讓分",
          "h1_total": "上半場大小"}
GROUP = {"actionable": "ACTIONABLE", "review": "REVIEW 需檢視"}
DISCLAIMER = "⚠️ 僅為模型與風控計算結果，非投注建議；仍在前瞻驗證期，沒有獲利證據。"


def _tpe(dt: datetime, fmt: str = "%m/%d %H:%M") -> str:
    return ensure_utc(dt).astimezone(TPE).strftime(fmt)


def _team(row: dict, side: str) -> str:
    return escape(str(row.get(f"{side}_name_zh") or row.get(f"{side}_abbr") or side))


def _matchup(row: dict) -> str:
    return f"{escape(str(row.get('away_abbr') or '?'))} @ {escape(str(row.get('home_abbr') or '?'))}"


def _num(v: Any, digits: int = 1, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.{digits}f}" if signed else f"{float(v):.{digits}f}"


def _pct(v: Any, digits: int = 1, signed: bool = False) -> str:
    return "—" if v is None else _num(float(v) * 100, digits, signed) + "%"


def _amount(v: Any) -> str:
    return "—" if v is None else f"{float(v):,.0f}"


def selection_text(o: dict) -> str:
    side = o.get("side")
    name = {"home": _team(o, "home"), "away": _team(o, "away"), "over": "大分", "under": "小分", "draw": "和局"}.get(
        side, escape(str(side)))
    line = o.get("display_line")
    if line is None or o.get("market_type") == "moneyline":
        return name
    return f"{name} {_num(line, 1, signed=o.get('market_type') == 'spread')}"


def format_actionable(betting_day: date, opps: list[dict], site_url: str | None = None) -> str:
    lines = [f"🏀 <b>決策中心：新的台彩機會</b>（betting day {betting_day:%m/%d}）", ""]
    by_game: dict[int, list[dict]] = {}
    for o in opps:
        by_game.setdefault(o["game_id"], []).append(o)
    for gid in sorted(by_game, key=lambda g: (by_game[g][0]["date_utc"], g)):
        g = by_game[gid][0]
        lines.append(f"<b>{_matchup(g)}</b>（{_tpe(g['date_utc'])}）")
        for o in by_game[gid]:
            lines.append(f"• [{GROUP.get(o.get('status_group'), 'ACTIONABLE')}] "
                         f"{MARKET.get(o.get('market'), escape(str(o.get('market'))))} {selection_text(o)} "
                         f"@ {_num(o.get('decimal_odds'), 2)}｜EV {_pct(o.get('ev_per_unit'), 1, signed=True)}")
            warn = [escape(str(w).split(":", 1)[1]) for w in (o.get("warnings") or [])
                    if str(w).startswith("data_quality:")] if o.get("status_group") == "review" else []
            if warn:
                lines.append(f"  ⚠ 資料品質：{', '.join(warn)}")
            lines.append(f"  模型 {_pct(o.get('model_prob'))} vs 去水公允 {_pct(o.get('fair_no_vig_prob'))}｜"
                         f"理論金額 {_amount(o.get('suggested_stake_amount'))}（剩餘上限 {_amount(o.get('max_additional_stake_amount'))}）")
        lines.append("")
    lines.append("下注前請到決策中心確認盤口仍有效；在台彩下注後回來按「記錄下注」。")
    if site_url:
        lines.append(f"{escape(site_url.rstrip('/'))}/decision")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_daily_summary(day: date, games: list[dict], site_url: str | None = None) -> str:
    lines = [f"📅 <b>NBA 賽事摘要</b>（台灣 {day:%m/%d}，共 {len(games)} 場）", ""]
    for g in sorted(games, key=lambda r: (r["date_utc"], r["id"])):
        head = f"{_tpe(g['date_utc'], '%H:%M')} <b>{_matchup(g)}</b>"
        if g.get("home_win_prob") is None and g.get("pred_margin") is None:
            lines.append(f"{head}　尚無預測")
            continue
        flags = [escape(str(f)) for f in (g.get("quality_flags") or [])]
        lines.append(f"{head}　主勝 {_pct(g.get('home_win_prob'))}｜分差(主−客) {_num(g.get('pred_margin'), 1, signed=True)}"
                     f"｜總分 {_num(g.get('pred_total'))}" + (f"　⚠ {', '.join(flags)}" if flags else ""))
    lines.append("")
    lines.append("預測會在傷病更新與開賽前約 60 分鐘再重算；以網站最新數字為準。")
    if site_url:
        lines.append(escape(site_url.rstrip("/")) + "/")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_system_alert(current: dict[str, str], new: Iterable[str], resolved: Iterable[str], *,
                        reminder_since: datetime | None = None) -> str:
    new, resolved = sorted(new), sorted(resolved)
    if reminder_since is not None:
        lines = [f"🚨 <b>系統異常仍未排除</b>（{len(current)} 項 FAIL，自 {_tpe(reminder_since)} 起）"]
    elif current:
        lines = [f"🚨 <b>系統異常</b>（{len(current)} 項 FAIL）"]
    else:
        lines = ["✅ <b>系統已恢復</b>（目前沒有 FAIL）"]
    if current:
        lines.append("")
        for k in sorted(current):
            mark = "🆕 " if k in new else ""
            lines.append(f"• {mark}<b>{escape(k)}</b>：{escape(current[k][:300])}")
    if resolved:
        lines.append("")
        lines.append("已恢復：" + "、".join(escape(k) for k in resolved))
    if current:
        lines.append("")
        lines.append("處理方式見 docs/production-runbook.md §5（或執行 run_production_check.py）。")
    return "\n".join(lines)
