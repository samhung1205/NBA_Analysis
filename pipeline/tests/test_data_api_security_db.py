"""Data API boundary: all writes and probes stay in the fixture's random schema."""
from pathlib import Path

import psycopg
from psycopg import sql
import pytest

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/postgres/0010_data_api_lockdown.sql"


def test_all_tables_have_rls_and_view_uses_invoker(db):
    with db.cursor() as cur:
        cur.execute("""SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname=current_schema() AND c.relkind IN ('r','p') AND NOT relrowsecurity""")
        assert cur.fetchall() == []
        cur.execute("SELECT reloptions FROM pg_class WHERE oid='v_odds_quotes'::regclass")
        assert "security_invoker=true" in cur.fetchone()["reloptions"]


def test_read_only_security_audit_passes(db):
    from run_security_check import check

    with db.transaction() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT current_schema() AS s")
        schema = cur.fetchone()["s"]
        report = check(cur.connection, schema)
        assert report["status"] == "PASS", report
        assert report["denied_read_probes"] == 8


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_api_role_cannot_read_write_truncate_or_use_view(db, role):
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
        if not cur.fetchone():
            pytest.skip(f"{role} role is absent; run against a Supabase-compatible test DB")
        cur.execute("SELECT current_schema() AS s")
        schema = cur.fetchone()["s"]
        # Simulate old Supabase defaults in this isolated schema, then rerun the fix.
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT ALL ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        cur.execute(sql.SQL("GRANT ALL ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT ALL ON TABLES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT ALL ON SEQUENCES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        # No real user data; control proves the vulnerable source-to-sink path.
        cur.execute("CREATE TABLE security_probe (id bigserial PRIMARY KEY, value text)")
        cur.execute("GRANT SELECT (value) ON security_probe TO PUBLIC")
        cur.execute(sql.SQL("GRANT SELECT (value) ON security_probe TO {}").format(sql.Identifier(role)))
        cur.execute("INSERT INTO security_probe(value) VALUES ('synthetic')")
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        cur.execute("SELECT value FROM security_probe")
        assert cur.fetchone()["value"] == "synthetic"
        cur.execute("RESET ROLE")
        cur.execute(MIGRATION.read_text())
        cur.execute(MIGRATION.read_text())  # repeatability
        cur.execute("CREATE TABLE security_future_probe (id bigserial PRIMARY KEY)")
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        for query in (
            "SELECT * FROM security_probe", "SELECT value FROM security_probe",  # column grants too
            "INSERT INTO security_probe(value) VALUES ('attacker')",
            "UPDATE security_probe SET value='attacker'", "DELETE FROM security_probe",
            "TRUNCATE security_probe", "SELECT * FROM v_odds_quotes",
            "SELECT nextval('security_probe_id_seq')", "SELECT * FROM security_future_probe",
            "SELECT nextval('security_future_probe_id_seq')", "SELECT bets_guard()",
        ):
            # Savepoint keeps a rejected probe from aborting the outer transaction.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with cur.connection.transaction():
                    cur.execute(query)
        cur.execute("RESET ROLE")
        cur.execute("SELECT value FROM security_probe")
        assert cur.fetchone()["value"] == "synthetic"
        cur.execute("UPDATE security_probe SET value='backend'")
        cur.execute("SELECT value FROM security_probe")
        assert cur.fetchone()["value"] == "backend"
        cur.execute("DROP TABLE security_future_probe, security_probe")


def test_trigger_search_paths_are_fixed_and_not_publicly_executable(db):
    with db.cursor() as cur:
        cur.execute("""SELECT p.proname,p.proconfig,p.proacl FROM pg_proc p
                       JOIN pg_namespace n ON n.oid=p.pronamespace
                       WHERE n.nspname=current_schema()""")
        rows = cur.fetchall()
        assert len(rows) == 6
        for row in rows:
            assert "search_path=pg_catalog" in row["proconfig"]
            assert not any(acl.startswith("=X/") for acl in row["proacl"])
