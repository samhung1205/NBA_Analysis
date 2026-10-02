"""建立賽前特徵底座並輸出到 pipeline/artifacts/（csv.gz）。用法：python run_build_features.py [--offset 0]"""
import argparse
import logging
from pathlib import Path

from core.db import cursor
from core.injury_asof import load_index
from core.logging_conf import setup_logging
from core.models import pregame_features as pf

log = logging.getLogger("build_features")


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=0, help="傷病決策時點：開賽前 X 分鐘（預設 0 = 開賽前最後一份）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    with cursor() as cur:
        games, derived, players = pf.load_inputs(cur)
        index = load_index(cur)
    df = pf.build_pregame_features(games, derived, players, index, decision_offset_min=args.offset)
    pf.assert_no_leakage(df)
    out = Path(args.out or Path(__file__).parent / "artifacts" / f"pregame_features_{pf.FEATURE_VERSION}_off{args.offset}.csv.gz")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    cols = pf.feature_columns(df)
    log.info("輸出 %s：%d 場、%d 個特徵欄；NaN 比例最高的 5 欄：%s", out, len(df), len(cols),
             df[cols].isna().mean().sort_values(ascending=False).head(5).round(3).to_dict())


if __name__ == "__main__":
    main()
