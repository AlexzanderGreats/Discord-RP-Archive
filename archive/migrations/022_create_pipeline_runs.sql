CREATE TABLE pipeline_runs (
    pipeline_run_id TEXT PRIMARY KEY,
    parent_pipeline_run_id TEXT,
    pipeline_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
    processing_mode TEXT NOT NULL CHECK(processing_mode IN ('full', 'incremental', 'dry_run')),
    scope_fingerprint TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    ruleset_versions_json TEXT NOT NULL,
    stage_results_json TEXT NOT NULL DEFAULT '{}',
    report_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    failed_stage TEXT,
    FOREIGN KEY(parent_pipeline_run_id)
        REFERENCES pipeline_runs(pipeline_run_id) ON DELETE NO ACTION,
    CHECK(
        (status = 'running' AND completed_at IS NULL AND failed_stage IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL AND failed_stage IS NULL)
        OR (status = 'failed' AND completed_at IS NOT NULL AND failed_stage IS NOT NULL)
    )
);
CREATE INDEX ix_pipeline_runs_started
    ON pipeline_runs(started_at DESC, pipeline_run_id);
CREATE INDEX ix_pipeline_runs_status
    ON pipeline_runs(status, started_at DESC);
CREATE INDEX ix_pipeline_runs_scope
    ON pipeline_runs(scope_fingerprint, started_at DESC);

CREATE TRIGGER pipeline_runs_no_delete
BEFORE DELETE ON pipeline_runs
BEGIN
    SELECT RAISE(ABORT, 'pipeline run manifests are immutable');
END;

CREATE TRIGGER pipeline_runs_finalize_once
BEFORE UPDATE ON pipeline_runs
WHEN OLD.status != 'running'
BEGIN
    SELECT RAISE(ABORT, 'completed pipeline run manifests are immutable');
END;
