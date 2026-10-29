"""Read-only model review. No retraining, promotion, model_metrics writes or paid APIs."""
import argparse
import json
from pathlib import Path

from core.production.monitoring import evaluate_snapshot, fetch_snapshot


def main():
    parser = argparse.ArgumentParser(description="唯讀評估近期實際賽前預測；不重訓、不切換模型")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--snapshot", type=Path, help="離線讀取已擷取的快照（不載入 .env、不連 DB）")
    parser.add_argument("--output", type=Path, help="將評估 JSON 寫到本機檔案；預設輸出 stdout")
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.snapshot.read_text()) if args.snapshot else fetch_snapshot(args.days)
        report = evaluate_snapshot(snapshot)
    except Exception as exc:
        # DB exception 可能帶入連線資訊，僅輸出錯誤類別。
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1
    body = json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(body)
        print(f"已儲存模型評估：{args.output}")
    else:
        print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
