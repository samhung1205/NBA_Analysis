"""Explicit independent shadow collector / materializer. Never promotes a model."""
import argparse

from core.production.shadow import collect, dumps
from core.production.observability import materialize


def main():
    parser = argparse.ArgumentParser(description="獨立影子實驗；不改正式預測、不定價、不下注")
    parser.add_argument("--activate", action="store_true", help="寫入啟用時間與凍結規則，從未來比賽開始")
    parser.add_argument("--collect", action="store_true", help="觀測賽果並擷取當前 T-75~T-60 窗口，會寫入影子 ledger")
    parser.add_argument("--materialize", action="store_true", help="將前瞻績效彙總寫入網站報告表")
    args = parser.parse_args()
    if not any((args.activate, args.collect, args.materialize)):
        parser.print_help()
        return 0
    try:
        if args.activate or args.collect:
            print(dumps(collect(activate=args.activate)))
        if args.materialize:
            print(dumps(materialize()))
    except Exception as exc:
        print(dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
