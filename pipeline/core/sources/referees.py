"""官方賽前裁判頁；獨立於 stats.nba.com。只解析 NBA 區塊，保留實際觀測時間。"""
from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser

from ..timeutil import ensure_utc

URL = "https://official.nba.com/referee-assignments/"


class _Assignments(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.scope = None
        self.meta = None
        self.heading = None
        self.is_nba = False
        self.date = ""
        self.table = False
        self.cell = None
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            self.heading = ""
        if tag == "div":
            self.depth += 1
            classes = attrs.get("class", "").split()
            if "entry-meta" in classes and self.is_nba:
                self.meta = self.depth
            if "nba-refs-content" in classes:
                self.scope = self.depth
        if tag == "table" and self.scope is not None:
            self.table = True
        if self.table and tag == "tr":
            self.row = []
        if self.table and tag in ("th", "td"):
            self.cell = ""

    def handle_data(self, data):
        if self.heading is not None:
            self.heading += data
        if self.meta is not None:
            self.date += data
        if self.cell is not None:
            self.cell += data + " "

    def handle_endtag(self, tag):
        if tag == "h1" and self.heading is not None:
            self.is_nba = " ".join(self.heading.split()) == "NBA Referee Assignments"
            self.heading = None
        if tag in ("th", "td") and self.cell is not None:
            self.row.append(" ".join(self.cell.split()))
            self.cell = None
        if tag == "tr" and self.table and self.row:
            self.rows.append(self.row)
        if tag == "table":
            self.table = False
        if tag == "div":
            if self.meta == self.depth:
                self.meta = None
            if self.scope == self.depth:
                self.scope = None
            self.depth -= 1


def parse_assignments(html: str, *, observed_at: datetime) -> dict:
    p = _Assignments()
    p.feed(html)
    expected = ["Game", "Crew Chief", "Referee", "Umpire", "Alternate"]
    if not p.rows or p.rows[0] != expected:
        raise ValueError("NBA 裁判頁表格結構不符；不能視為零場")
    try:
        report_date = datetime.strptime(" ".join(p.date.split()), "%B %d, %Y").date()
    except ValueError as e:
        raise ValueError("NBA 裁判頁日期無法辨識") from e
    rows = []
    for row in p.rows[1:]:
        if len(row) != 5 or not all(row[:4]) or "@" not in row[0]:
            raise ValueError("NBA 裁判列缺少對戰或三位裁判；不能視為完整資料")
        rows.append(dict(zip(("game", "crew_chief", "referee", "umpire", "alternate"), row)))
    return {"source": "nba_official_referees", "report_date_et": report_date.isoformat(),
            "observed_at": ensure_utc(observed_at).isoformat(), "assignments": rows}
