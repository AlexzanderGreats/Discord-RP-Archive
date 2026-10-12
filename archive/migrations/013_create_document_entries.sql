CREATE TABLE document_entries (
    entry_id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    entry_index INTEGER NOT NULL CHECK(entry_index >= 0),
    speaker_raw TEXT,
    character_id TEXT,
    player_id TEXT,
    content_text TEXT NOT NULL,
    source_location_json TEXT,
    FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ux_document_entries_document_index
    ON document_entries(document_id, entry_index);
CREATE INDEX ix_document_entries_document ON document_entries(document_id);