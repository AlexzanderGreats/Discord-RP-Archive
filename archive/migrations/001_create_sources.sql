CREATE TABLE sources (
    source_id INTEGER PRIMARY KEY,
    logical_source_key TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('discord_json', 'txt', 'pdf', 'docx', 'other')),
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    scope_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    supersedes_source_id INTEGER,
    UNIQUE(source_id, logical_source_key),
    CHECK(supersedes_source_id IS NULL OR supersedes_source_id <> source_id),
    FOREIGN KEY(supersedes_source_id, logical_source_key)
        REFERENCES sources(source_id, logical_source_key) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_sources_logical_hash ON sources(logical_source_key, sha256);
CREATE INDEX ix_sources_logical_key ON sources(logical_source_key);
CREATE INDEX ix_sources_sha256 ON sources(sha256);
CREATE INDEX ix_sources_kind ON sources(source_kind);
CREATE INDEX ix_sources_supersedes ON sources(supersedes_source_id);