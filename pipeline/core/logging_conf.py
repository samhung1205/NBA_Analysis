import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # nba_api 內部用 urllib3，過於吵雜時可調高其等級
    logging.getLogger("urllib3").setLevel(logging.WARNING)
