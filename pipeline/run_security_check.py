#!/usr/bin/env python
"""唯讀檢查 Data API 邊界；不輸出資料列、憑證、帳號或原始錯誤。"""
from __future__ import annotations

import json

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from core.config import settings


def check(conn, schema="public"):
    problems = []
    relations = conn.execute("""
        SELECT c.oid,c.relname,c.relkind,c.relrowsecurity,c.reloptions
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') ORDER BY c.relname
    """, (schema,)).fetchall()
    if not relations:
        problems.append("application_schema_empty")
    for row in relations:
        if row["relkind"] in ("r", "p") and not row["relrowsecurity"]:
            problems.append(f"rls_disabled:{row['relname']}")
        if row["relkind"] == "v" and "security_invoker=true" not in (row["reloptions"] or []):
            problems.append(f"owner_view:{row['relname']}")
    roles = conn.execute("SELECT oid,rolname,rolsuper,rolbypassrls FROM pg_roles WHERE rolname IN ('anon','authenticated')").fetchall()
    if len(roles) != 2:
        problems.append("supabase_api_roles_missing")
    denied = 0
    for role in roles:
        name = role["rolname"]
        if role["rolsuper"] or role["rolbypassrls"]:
            problems.append(f"api_role_bypasses_rls:{name}")
        for row in conn.execute("""
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND CASE WHEN c.relkind IN ('r','p','v','m','f') THEN (
                has_table_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')) ELSE false END
        """, (schema, name, name)).fetchall():
            problems.append(f"api_relation_grant:{name}:{row['relname']}")
        for row in conn.execute("""
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND CASE WHEN c.relkind='S'
                THEN has_sequence_privilege(%s,c.oid,'USAGE,SELECT,UPDATE') ELSE false END
        """, (schema, name)).fetchall():
            problems.append(f"api_sequence_grant:{name}:{row['relname']}")
        for row in conn.execute("""
            SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname=%s AND has_function_privilege(%s,p.oid,'EXECUTE')
        """, (schema, name)).fetchall():
            problems.append(f"api_function_grant:{name}:{row['proname']}")
        # Actual role checks through the same Postgres authorization boundary as PostgREST.
        # LIMIT 0 never returns user data; each denied read rolls back only its savepoint.
        for table in ("users", "bets", "bankroll_ledger", "v_odds_quotes"):
            try:
                with conn.transaction():
                    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(name)))
                    conn.execute(sql.SQL("SELECT * FROM {}.{} LIMIT 0").format(sql.Identifier(schema), sql.Identifier(table)))
                    conn.execute("RESET ROLE")
                problems.append(f"api_read_not_denied:{name}:{table}")
            except psycopg.errors.InsufficientPrivilege:
                denied += 1
    defaults = conn.execute("""
        SELECT count(*) AS n FROM pg_default_acl d
        CROSS JOIN LATERAL aclexplode(d.defaclacl) a
        WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=current_user)
          AND d.defaclnamespace IN (0,(SELECT oid FROM pg_namespace WHERE nspname=%s))
          AND d.defaclobjtype IN ('r','S')
          AND (a.grantee=0 OR a.grantee IN (SELECT oid FROM pg_roles WHERE rolname IN ('anon','authenticated')))
    """, (schema,)).fetchone()["n"]
    if defaults:
        problems.append("unsafe_future_table_or_sequence_defaults")
    return {"status": "FAIL" if problems else "PASS", "tables": sum(r["relkind"] in ("r", "p") for r in relations),
            "views": sum(r["relkind"] == "v" for r in relations), "denied_read_probes": denied, "problems": problems}


def main():
    try:
        with psycopg.connect(settings.database_url, connect_timeout=15, row_factory=dict_row) as conn:
            conn.read_only = True
            report = check(conn)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "PASS" else 1
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
