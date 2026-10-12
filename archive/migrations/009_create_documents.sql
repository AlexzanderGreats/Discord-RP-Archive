CREATE TABLE documents (
    document_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    import_id INTEGER NOT NULL,
    source_filename TEXT NOT NULL,
    source_format TEXT NOT NULL CHECK(source_format IN ('txt', 'pdf', 'docx')),
    file_size_bytes INTEGER NOT NULL CHECK(file_size_bytes >= 0),
    title_original TEXT,
    title_override TEXT,
    document_type TEXT NOT NULL CHECK(document_type IN ('unknown', 'character_profile', 'character_sheet', 'lore', 'worldbuilding', 'story', 'chapter', 'rp_transcript', 'rp_scene', 'powerscaling', 'analysis', 'reference', 'notes', 'other')),
    document_status TEXT NOT NULL CHECK(document_status IN ('historical', 'current', 'reference', 'draft', 'unknown')),
    created_at_source TEXT,
    modified_at_source TEXT,
    imported_at TEXT NOT NULL,
    page_count INTEGER CHECK(page_count IS NULL OR page_count >= 0),
    paragraph_count INTEGER NOT NULL CHECK(paragraph_count >= 0),
    character_count INTEGER NOT NULL CHECK(character_count >= 0),
    word_count INTEGER NOT NULL CHECK(word_count >= 0),
    extraction_status TEXT NOT NULL CHECK(extraction_status IN ('success', 'partial', 'no_text', 'error')),
    extraction_error TEXT,
    content_text TEXT NOT NULL,
    content_search TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    pages_json TEXT,
    blocks_json TEXT,
    warnings_json TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE NO ACTION,
    FOREIGN KEY(import_id, source_id) REFERENCES imports(import_id, source_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_documents_import ON documents(import_id);
CREATE INDEX ix_documents_source ON documents(source_id);
CREATE INDEX ix_documents_import ON documents(import_id);
CREATE INDEX ix_documents_filename ON documents(source_filename);
CREATE INDEX ix_documents_format ON documents(source_format);
CREATE INDEX ix_documents_title_original ON documents(title_original);
CREATE INDEX ix_documents_title_override ON documents(title_override);
CREATE INDEX ix_documents_type ON documents(document_type);
CREATE INDEX ix_documents_status ON documents(document_status);
CREATE INDEX ix_documents_extraction_status ON documents(extraction_status);