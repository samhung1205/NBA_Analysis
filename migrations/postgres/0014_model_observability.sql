-- Independent prospective experiment. No changes to production predictions, odds, bets or risk.
CREATE TABLE IF NOT EXISTS model_shadow_experiments (
  version TEXT PRIMARY KEY, activated_at TIMESTAMPTZ NOT NULL, spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_shadow_states (
  id TEXT PRIMARY KEY, experiment_version TEXT NOT NULL REFERENCES model_shadow_experiments(version),
  period_key TEXT NOT NULL, effective_at TIMESTAMPTZ NOT NULL, payload TEXT NOT NULL,
  UNIQUE(experiment_version, period_key)
);
CREATE TABLE IF NOT EXISTS model_shadow_predictions (
  id TEXT PRIMARY KEY, experiment_version TEXT NOT NULL REFERENCES model_shadow_experiments(version),
  game_id INTEGER NOT NULL REFERENCES games(id), baseline_prediction_id INTEGER NOT NULL REFERENCES predictions(id),
  state_id TEXT NOT NULL REFERENCES model_shadow_states(id), scheduled_tipoff TIMESTAMPTZ NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL, payload TEXT NOT NULL,
  UNIQUE(experiment_version, game_id)
);
CREATE TABLE IF NOT EXISTS model_shadow_results (
  id TEXT PRIMARY KEY, shadow_prediction_id TEXT NOT NULL REFERENCES model_shadow_predictions(id),
  observed_at TIMESTAMPTZ NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_results_asof ON model_shadow_results(shadow_prediction_id, observed_at);
CREATE TABLE IF NOT EXISTS model_evaluation_reports (
  report_key TEXT PRIMARY KEY, generated_at TIMESTAMPTZ NOT NULL, payload TEXT NOT NULL
);
-- Experiment ledger is append-only, including score corrections and state updates.
CREATE OR REPLACE FUNCTION protect_model_shadow_ledger() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN RAISE EXCEPTION 'model shadow ledger is append-only'; END;
$$;
REVOKE ALL ON FUNCTION protect_model_shadow_ledger() FROM PUBLIC;
DO $$
DECLARE table_name text; api_role text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['model_shadow_experiments','model_shadow_states','model_shadow_predictions','model_shadow_results','model_evaluation_reports'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('REVOKE ALL ON TABLE %I FROM PUBLIC', table_name);
    IF table_name <> 'model_evaluation_reports' THEN
      EXECUTE format('DROP TRIGGER IF EXISTS protect_shadow_ledger ON %I', table_name);
      EXECUTE format('CREATE TRIGGER protect_shadow_ledger BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION protect_model_shadow_ledger()', table_name);
    END IF;
    FOREACH api_role IN ARRAY ARRAY['anon','authenticated'] LOOP
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=api_role) THEN
        EXECUTE format('REVOKE ALL ON TABLE %I FROM %I', table_name, api_role);
        EXECUTE format('REVOKE ALL ON FUNCTION protect_model_shadow_ledger() FROM %I', api_role);
      END IF;
    END LOOP;
  END LOOP;
END $$;
