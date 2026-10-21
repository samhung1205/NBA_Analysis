-- 此專案僅由 Hono / Python 直接連 Postgres；不開放 Supabase Data API。
-- 不建立 USING (true) policy：網站登入與 Supabase Auth 是不同的身分系統。
-- 使用 current_schema()，讓 DB fixture 僅修改隨機測試 schema，不碰 public。
-- 由現有物件 owner 執行；整個 DO 為原子操作，可安全重跑。
DO $$
DECLARE
  target_schema text := current_schema();
  object_record record;
  api_role text;
BEGIN
  IF target_schema IS NULL OR target_schema IN ('pg_catalog', 'information_schema') THEN
    RAISE EXCEPTION 'An explicit application schema is required';
  END IF;

  FOR object_record IN
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = target_schema AND c.relkind IN ('r', 'p')
  LOOP
    EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', target_schema, object_record.relname);
  END LOOP;

  -- Owner-executed views can otherwise bypass the underlying tables' RLS.
  EXECUTE format('ALTER VIEW %I.v_odds_quotes SET (security_invoker = true)', target_schema);

  -- RLS does not cover TRUNCATE / sequences / function execution. Revoke grants too.
  EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', target_schema);
  EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', target_schema);
  EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC', target_schema);
  EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON TABLES FROM PUBLIC', target_schema);
  EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM PUBLIC', target_schema);

  -- Table-level REVOKE does not remove separately granted column privileges.
  FOR object_record IN
    SELECT c.relname, a.attname FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = target_schema AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND a.attnum > 0 AND NOT a.attisdropped AND a.attacl IS NOT NULL
  LOOP
    EXECUTE format('REVOKE ALL (%I) ON TABLE %I.%I FROM PUBLIC',
                   object_record.attname, target_schema, object_record.relname);
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
        EXECUTE format('REVOKE ALL (%I) ON TABLE %I.%I FROM %I',
                       object_record.attname, target_schema, object_record.relname, api_role);
      END IF;
    END LOOP;
  END LOOP;

  FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    -- 普通 Postgres 不一定有 Supabase 角色；D1 不套用此 migration。
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', target_schema, api_role);
      EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', target_schema, api_role);
      EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', target_schema, api_role);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON TABLES FROM %I', target_schema, api_role);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM %I', target_schema, api_role);
    END IF;
  END LOOP;

  -- Existing trigger functions only use built-ins and NEW/OLD; pin their search path.
  FOREACH api_role IN ARRAY ARRAY['d5_immutable', 'bets_guard', 'bankroll_account_guard',
                                  'decision_snapshot_guard', 'paper_ledger_immutable',
                                  'paper_bet_settlement_guard'] LOOP
    EXECUTE format('ALTER FUNCTION %I.%I() SET search_path = pg_catalog', target_schema, api_role);
  END LOOP;
END $$;
