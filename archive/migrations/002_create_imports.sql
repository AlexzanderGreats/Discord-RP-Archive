CREATE TABLE imports (
    import_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'partial', 'failed')),
    accepted_at TEXT,
    importer_name TEXT NOT NULL,
    importer_version TEXT NOT NULL,
    config_sha256 TEXT,
    records_seen INTEGER CHECK(records_seen IS NULL OR records_seen >= 0),
    records_written INTEGER CHECK(records_written IS NULL OR records_written >= 0),
    warnings_json TEXT NOT NULL,
    error_summary TEXT,
    CHECK(accepted_at IS NULL OR status = 'succeeded'),
    FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX ux_imports_id_source ON imports(import_id, source_id);
CREATE INDEX ix_imports_source ON imports(source_id);
CREATE INDEX ix_imports_status ON imports(status);
CREATE INDEX ix_imports_started ON imports(started_at);
CREATE INDEX ix_imports_config_hash ON imports(config_sha256);
CREATE INDEX ix_imports_accepted ON imports(source_id, accepted_at);