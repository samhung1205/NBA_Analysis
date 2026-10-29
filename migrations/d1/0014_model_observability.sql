-- D1 mirrors the public report contract; Python owns all model / experiment calculations.
CREATE TABLE IF NOT EXISTS model_shadow_experiments (
  version TEXT PRIMARY KEY, activated_at TEXT NOT NULL, spec_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_shadow_states (
  id TEXT PRIMARY KEY, experiment_version TEXT NOT NULL REFERENCES model_shadow_experiments(version),
  period_key TEXT NOT NULL, effective_at TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(experiment_version, period_key)
);
CREATE TABLE IF NOT EXISTS model_shadow_predictions (
  id TEXT PRIMARY KEY, experiment_version TEXT NOT NULL REFERENCES model_shadow_experiments(version),
  game_id INTEGER NOT NULL REFERENCES games(id), baseline_prediction_id INTEGER NOT NULL REFERENCES predictions(id),
  state_id TEXT NOT NULL REFERENCES model_shadow_states(id), scheduled_tipoff TEXT NOT NULL,
  captured_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(experiment_version, game_id)
);
CREATE TABLE IF NOT EXISTS model_shadow_results (
  id TEXT PRIMARY KEY, shadow_prediction_id TEXT NOT NULL REFERENCES model_shadow_predictions(id),
  observed_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_results_asof ON model_shadow_results(shadow_prediction_id, observed_at);
CREATE TABLE IF NOT EXISTS model_evaluation_reports (
  report_key TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS shadow_experiments_no_update BEFORE UPDATE ON model_shadow_experiments BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_experiments_no_delete BEFORE DELETE ON model_shadow_experiments BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_states_no_update BEFORE UPDATE ON model_shadow_states BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_states_no_delete BEFORE DELETE ON model_shadow_states BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_predictions_no_update BEFORE UPDATE ON model_shadow_predictions BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_predictions_no_delete BEFORE DELETE ON model_shadow_predictions BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_results_no_update BEFORE UPDATE ON model_shadow_results BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_results_no_delete BEFORE DELETE ON model_shadow_results BEGIN SELECT RAISE(ABORT,'model shadow ledger is append-only'); END;
