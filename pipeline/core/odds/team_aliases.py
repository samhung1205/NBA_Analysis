"""
球隊別名表（Phase D.1）—— 盤口來源隊名 → teams.abbr
------------------------------------------------------------
可檢查的單一來源：所有來源（英文全名、城市/簡寫變體、台彩中文名）都列在這裡，
不在各 adapter 散落 string replace。比對規則：normalize() 後**完全相等**才算（不做模糊比對）。

刻意**不收錄**：只有城市的名稱（"Los Angeles" / "LA" / "洛杉磯" 同時對應兩隊）。
查不到的隊名 → None → 該事件記為 unmatched（reason=unknown_team）並告警，由人補進這張表。

台彩中文名：2026-10-04 實測 金州勇士 / 洛杉磯快艇 / 猶他爵士 / 丹佛金塊（其餘為台灣常用譯名 + 常見異體，
例行賽開打後以 odds_event_links 的 unknown_team 告警補齊）。
"""
from __future__ import annotations

import re
import unicodedata

TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "ATL": ("Atlanta Hawks", "Hawks", "亞特蘭大老鷹", "老鷹"),
    "BOS": ("Boston Celtics", "Celtics", "波士頓塞爾提克", "塞爾提克", "波士頓凱爾特人"),
    "BKN": ("Brooklyn Nets", "Nets", "布魯克林籃網", "籃網"),
    "CHA": ("Charlotte Hornets", "Hornets", "夏洛特黃蜂", "黃蜂"),
    "CHI": ("Chicago Bulls", "Bulls", "芝加哥公牛", "公牛"),
    "CLE": ("Cleveland Cavaliers", "Cavaliers", "Cavs", "克里夫蘭騎士", "克利夫蘭騎士", "騎士"),
    "DAL": ("Dallas Mavericks", "Mavericks", "Mavs", "達拉斯獨行俠", "達拉斯小牛", "獨行俠"),
    "DEN": ("Denver Nuggets", "Nuggets", "丹佛金塊", "金塊"),
    "DET": ("Detroit Pistons", "Pistons", "底特律活塞", "活塞"),
    "GSW": ("Golden State Warriors", "Golden State", "Warriors", "金州勇士", "勇士"),
    "HOU": ("Houston Rockets", "Rockets", "休士頓火箭", "休斯頓火箭", "火箭"),
    "IND": ("Indiana Pacers", "Pacers", "印第安納溜馬", "印地安那溜馬", "印第安那溜馬", "溜馬"),
    "LAC": ("Los Angeles Clippers", "LA Clippers", "L.A. Clippers", "Clippers", "洛杉磯快艇", "快艇"),
    "LAL": ("Los Angeles Lakers", "LA Lakers", "L.A. Lakers", "Lakers", "洛杉磯湖人", "湖人"),
    "MEM": ("Memphis Grizzlies", "Grizzlies", "曼斐斯灰熊", "曼菲斯灰熊", "孟菲斯灰熊", "灰熊"),
    "MIA": ("Miami Heat", "Heat", "邁阿密熱火", "熱火"),
    "MIL": ("Milwaukee Bucks", "Bucks", "密爾瓦基公鹿", "密爾沃基公鹿", "公鹿"),
    "MIN": ("Minnesota Timberwolves", "Timberwolves", "明尼蘇達灰狼", "明尼蘇達木狼", "灰狼"),
    "NOP": ("New Orleans Pelicans", "Pelicans", "紐奧良鵜鶘", "紐奧爾良鵜鶘", "新奧爾良鵜鶘", "鵜鶘"),
    "NYK": ("New York Knicks", "Knicks", "紐約尼克", "紐約尼克斯", "尼克"),
    "OKC": ("Oklahoma City Thunder", "Thunder", "奧克拉荷馬雷霆", "奧克拉荷馬市雷霆", "俄克拉荷馬城雷霆", "雷霆"),
    "ORL": ("Orlando Magic", "Magic", "奧蘭多魔術", "魔術"),
    "PHI": ("Philadelphia 76ers", "76ers", "Sixers", "費城76人", "費城七六人", "76人", "七六人"),
    "PHX": ("Phoenix Suns", "Suns", "鳳凰城太陽", "太陽"),
    "POR": ("Portland Trail Blazers", "Trail Blazers", "Blazers", "波特蘭拓荒者", "波特蘭開拓者", "拓荒者"),
    "SAC": ("Sacramento Kings", "Kings", "沙加緬度國王", "薩克拉門托國王", "國王"),
    "SAS": ("San Antonio Spurs", "Spurs", "聖安東尼奧馬刺", "聖安東尼馬刺", "馬刺"),
    "TOR": ("Toronto Raptors", "Raptors", "多倫多暴龍", "多倫多猛龍", "暴龍"),
    "UTA": ("Utah Jazz", "Jazz", "猶他爵士", "爵士"),
    "WAS": ("Washington Wizards", "Wizards", "華盛頓巫師", "華盛頓奇才", "巫師"),
}

_PUNCT = re.compile(r"[.·'’]")
_SPACE = re.compile(r"\s+")


def normalize(name: str | None) -> str:
    """NFKC（全形→半形）、去 \\r 等控制字元、去句點/撇號、合併空白、小寫。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = _PUNCT.sub("", s)
    return _SPACE.sub(" ", s).strip().lower()


def build_alias_index(aliases: dict[str, tuple[str, ...]] = TEAM_ALIASES) -> dict[str, str]:
    """normalized alias → abbr。同一別名對應兩隊 → ValueError（表本身有錯，不能上線）。"""
    index: dict[str, str] = {}
    for abbr, names in aliases.items():
        for n in (abbr, *names):
            k = normalize(n)
            if k in index and index[k] != abbr:
                raise ValueError(f"別名 {n!r} 同時對應 {index[k]} 與 {abbr}")
            index[k] = abbr
    return index


ALIAS_INDEX = build_alias_index()


def resolve_abbr(name: str | None) -> str | None:
    return ALIAS_INDEX.get(normalize(name))
