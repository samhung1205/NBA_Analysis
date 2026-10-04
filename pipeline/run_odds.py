#!/usr/bin/env python
"""
Phase D.1：盤口快照 CLI（每個來源可單獨執行）
------------------------------------------------------------
  python run_odds.py --source twsport              # 台灣運彩（Playwright + 本機 Chrome）
  python run_odds.py --source oddsapi              # The Odds API（需 ODDS_API_KEY）
  python run_odds.py --source all --dry-run        # 抓取 + 解析 + 比對，不寫任何東西
  python run_odds.py --source twsport --from-file capture.har     # 匯入一般瀏覽器匯出的 HAR（合規備援）
  python run_odds.py --source twsport --from-file raw.json         # 匯入先前 --save-raw 的 raw（含 captured_at）
  python run_odds.py --source twsport --dry-run --save-raw out.json
  python run_odds.py --print-next-runs

輸出為 JSON 摘要（outcome、場次、比對、market counts、新增/未變/stale、延遲、額度）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from core.logging_conf import setup_logging


def _load_file(source: str, path: str):
    from core.odds import twsport
    from core.timeutil import ensure_utc

    data = json.load(open(path, encoding="utf-8"))
    if source == "twsport" and isinstance(data, dict) and "log" in data:
        return twsport.raw_from_har(data)
    cap = data.get("captured_at") if isinstance(data, dict) else None
    if not cap:
        sys.exit("raw 檔需含 captured_at（實際觀測時間，UTC ISO8601）；不可用匯入當下時間冒充")
    return data, ensure_utc(datetime.fromisoformat(cap.replace("Z", "+00:00")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["twsport", "oddsapi", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true", help="不寫入 DB（快照 / 對應 / 抓取紀錄 / 心跳）")
    ap.add_argument("--from-file", help="匯入 HAR（台彩）或 raw JSON，而不是即時抓取")
    ap.add_argument("--save-raw", help="把即時抓取的 raw 存成 JSON（含 captured_at）")
    ap.add_argument("--print-next-runs", action="store_true")
    args = ap.parse_args()
    setup_logging()

    if args.print_next_runs:
        from core.scheduling import job_specs, next_run_times
        for job_id, t in next_run_times(job_specs()).items():
            if job_id.startswith("odds_"):
                print(f"{job_id:16s} {t.isoformat() if t else 'PAUSED/NEVER'}")
        return

    from core.odds.ingest import _fetch_oddsapi, _fetch_twsport, run_odds_job
    from core.timeutil import now_utc

    sources = ["twsport", "oddsapi"] if args.source == "all" else [args.source]
    if args.from_file and len(sources) != 1:
        sys.exit("--from-file 需指定單一 --source")
    out = []
    for src in sources:
        fetch, fetched_at = None, None
        if args.from_file:
            raw, fetched_at = _load_file(src, args.from_file)
            fetch = lambda rep, raw=raw: raw  # noqa: E731
        elif args.save_raw:
            def fetch(rep, src=src):
                raw = _fetch_twsport(rep) if src == "twsport" else _fetch_oddsapi(rep, now_utc())
                raw = {**raw, "captured_at": now_utc().isoformat()}
                path = args.save_raw if len(sources) == 1 else f"{args.save_raw}.{src}.json"
                json.dump(raw, open(path, "w", encoding="utf-8"), ensure_ascii=False)
                return raw
        rep = run_odds_job(src, dry_run=args.dry_run, fetch=fetch, fetched_at=fetched_at)
        out.append(rep.summary())
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
