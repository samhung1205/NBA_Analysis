"""
測試共用設定
------------------------------------------------------------
- 純邏輯測試（時間邊界 / 排程 / fallback 鏈 / 傷病時間）完全不連資料庫、不打網路。
- 需要 DB 的測試使用 `db` fixture：在真實 Postgres 裡建一個**獨立的暫存 schema**
  （c5a_test_<random>），套用 migrations/postgres/*.sql，測完 DROP SCHEMA CASCADE。
  所有 SQL 只會碰到暫存 schema，不會動到 public 的正式資料。連不上 DB 時這些測試自動 skip。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

PIPELINE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PIPELINE_DIR.parent
sys.path.insert(0, str(PIPELINE_DIR))

try:
    import core.config  # noqa: F401
except RuntimeError:  # 沒有 DATABASE_URL（例如純 CI）：純邏輯測試仍可跑
    os.environ["DATABASE_URL"] = "postgresql://invalid:invalid@127.0.0.1:1/none"
    import core.config  # noqa: F401

TABLES = ["injuries", "player_game_stats", "team_game_stats", "elo_ratings", "predictions", "odds_snapshots",
          "bets", "games", "players", "teams", "data_sources"]


@pytest.fixture(scope="session")
def _pg_schema():
    import psycopg
    from psycopg.rows import dict_row

    url = core.config.settings.database_url
    if url.startswith("postgresql://invalid"):
        pytest.skip("沒有可用的 DATABASE_URL，略過 DB 測試")
    schema = f"c5a_test_{uuid.uuid4().hex[:10]}"
    try:
        conn = psycopg.connect(url, autocommit=True, connect_timeout=15, row_factory=dict_row)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"連不上資料庫，略過 DB 測試：{type(e).__name__}")
    try:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
        for f in sorted((REPO_ROOT / "migrations" / "postgres").glob("*.sql")):
            conn.execute(f.read_text())
        yield schema
    finally:
        try:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            conn.close()


@pytest.fixture()
def db(_pg_schema, monkeypatch):
    """把 core.db 的連線池指向暫存 schema，並在每個測試前清空所有表。"""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    import core.db as coredb

    def configure(conn):
        conn.execute(f'SET search_path TO "{_pg_schema}"')

    pool = ConnectionPool(core.config.settings.database_url, min_size=1, max_size=3, open=True,
                          kwargs={"row_factory": dict_row, "autocommit": True}, configure=configure)
    monkeypatch.setattr(coredb, "_pool", pool)
    with coredb.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
    yield coredb
    pool.close()


@pytest.fixture()
def seeded(db):
    """3 支球隊 + 3 位球員。回傳 {abbr: team_id}。"""
    with db.cursor() as cur:
        ids = {}
        for nba_id, abbr, name in [(1610612738, "BOS", "Boston Celtics"), (1610612752, "NYK", "New York Knicks"),
                                    (1610612747, "LAL", "Los Angeles Lakers")]:
            ids[abbr] = db.upsert_team(cur, nba_team_id=nba_id, abbr=abbr, name=name)
        db.upsert_player(cur, nba_player_id=1627759, name="Jaylen Brown", team_id=ids["BOS"])
        db.upsert_player(cur, nba_player_id=1628369, name="Jayson Tatum", team_id=ids["BOS"])
        db.upsert_player(cur, nba_player_id=1629020, name="Jaren Jackson Jr.", team_id=ids["NYK"])
    return ids


def pytest_collection_modifyitems(items):
    for item in items:
        if "db" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)
