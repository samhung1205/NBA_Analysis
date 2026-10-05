#!/usr/bin/env python
"""
台灣運彩 HAR 手動匯入（合規備援；不繞過 Cloudflare）
------------------------------------------------------------
流程：一般 Chrome 開台彩 NBA 頁面 → DevTools → Network → Save all as HAR with content
      → python run_twsport_har.py capture.har            （預設：驗證 + dry-run，不寫 DB）
      → python run_twsport_har.py capture.har --import   （確認無誤後才寫入）
      → 加 --delete-after：匯入成功後覆寫並刪除 HAR（HAR 內有 cookie / token）

as-of 語意（防洩漏）：
  fetched_at = HAR 內所用回應的「回應完成時間」（startedDateTime + time），**不是**匯入當下。
  execution-v1 在 T = 開賽 − 60 分只會用 fetched_at ≤ T 的快照；T 之後才擷取的 HAR 不會被當成 T 之前就存在。
  請在 HAR 擷取後盡快匯入，且擷取時機要在 T 之前才有用（T 之後的擷取只會是 stale / 盤中，不會進決策）。

安全：HAR 可能含 cookie / Authorization；本工具絕不印出 header / cookie / body，只印統計。
      *.har 已列入 .gitignore 與 .dockerignore；不要 commit、不要貼到聊天。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

SENSITIVE_HEADERS = {"cookie", "authorization", "set-cookie", "x-csrf-token", "x-auth-token"}
STALE_WARN = timedelta(hours=2)


def sensitive_entry_count(har: dict) -> int:
    n = 0
    for e in (har.get("log") or {}).get("entries") or []:
        names = {str(h.get("name", "")).lower() for h in (e.get("request") or {}).get("headers") or []}
        names |= {str(h.get("name", "")).lower() for h in (e.get("response") or {}).get("headers") or []}
        if names & SENSITIVE_HEADERS:
            n += 1
    return n


def shred(path: Path) -> None:
    """盡力覆寫後刪除（SSD / APFS 無法保證實體抹除；真正敏感時請同時登出台彩並清除瀏覽器 session）。"""
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.write(b"\0" * size)
        f.flush()
        os.fsync(f.fileno())
    path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("har", help="DevTools 匯出的 .har 檔")
    ap.add_argument("--import", dest="do_import", action="store_true", help="實際寫入資料庫（預設只驗證 + dry-run）")
    ap.add_argument("--delete-after", action="store_true", help="成功匯入後覆寫並刪除 HAR")
    ap.add_argument("--allow-stale", action="store_true", help=f"允許擷取時間距今超過 {STALE_WARN}（仍保留原擷取時間，不會冒充現在）")
    args = ap.parse_args()
    if args.delete_after and not args.do_import:
        sys.exit("--delete-after 只能搭配 --import")

    path = Path(args.har)
    if not path.is_file():
        sys.exit(f"找不到檔案：{path}")
    from core.logging_conf import setup_logging
    setup_logging()
    from core.odds import twsport
    from core.odds.errors import OddsSourceError
    from core.odds.ingest import run_odds_job
    from core.timeutil import now_utc

    try:
        har = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        sys.exit("不是合法的 HAR（JSON）檔")
    n_sens = sensitive_entry_count(har)
    if n_sens:
        print(f"⚠ 此 HAR 有 {n_sens} 筆請求 / 回應帶 cookie 或 authorization header（內容不會被印出）。"
              "匯入後請刪除檔案，且不要 commit / 分享。")
    try:
        raw, fetched_at = twsport.raw_from_har(har)
    except OddsSourceError as e:
        sys.exit(f"HAR 無法使用：{e}")

    now = now_utc()
    age = now - fetched_at
    print(f"擷取時間（回應完成，UTC）：{fetched_at.isoformat()}   距今 {age.total_seconds() / 60:.0f} 分鐘")
    if age > STALE_WARN and not args.allow_stale:
        sys.exit(f"HAR 已超過 {STALE_WARN}；舊資料對 T-60 決策沒有價值。若仍要記錄請加 --allow-stale"
                 "（會以原擷取時間寫入，不會當成現在）")

    def fetch(_rep, raw=raw):
        return raw

    rep = run_odds_job("twsport", dry_run=not args.do_import, fetch=fetch, fetched_at=fetched_at)
    s = rep.summary()
    keys = ("outcome", "n_events", "n_matched", "n_unmatched", "n_ambiguous", "n_rejected", "n_markets",
            "n_snapshots_new", "n_snapshots_unchanged", "n_snapshots_stale", "fetched_at", "error")
    print(json.dumps({k: s.get(k) for k in keys}, ensure_ascii=False, indent=1, default=str))
    in_play = [e for e in rep.events if e.get("skipped_in_play")]
    if in_play:
        print(f"ℹ {len(in_play)} 場在擷取時已開賽，不會寫入（D.1 只收賽前盤）")
    if rep.outcome not in ("success", "partial"):
        print(f"✗ outcome={rep.outcome}，未視為成功" + ("；HAR 未刪除" if args.delete_after else ""))
        return 1
    if not args.do_import:
        print("dry-run 完成，未寫入。確認無誤後加 --import（可再加 --delete-after）。")
        return 0
    print(f"✓ 已匯入，快照 fetched_at = {fetched_at.isoformat()}（原擷取時間）")
    if args.delete_after:
        shred(path)
        print(f"✓ 已覆寫並刪除 {path.name}")
    else:
        print("提醒：HAR 仍在磁碟上，含 cookie / token，確認不再需要後請刪除（或下次加 --delete-after）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
