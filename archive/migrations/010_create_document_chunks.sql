CREATE TABLE document_chunks (
    chunk_id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    page_start INTEGER CHECK(page_start IS NULL OR page_start > 0),
    page_end INTEGER CHECK(page_end IS NULL OR page_end > 0),
    paragraph_start INTEGER CHECK(paragraph_start IS NULL OR paragraph_start > 0),
    paragraph_end INTEGER CHECK(paragraph_end IS NULL OR paragraph_end > 0),
    section_title TEXT,
    content_text TEXT NOT NULL,
    content_search TEXT NOT NULL,
    source_location_json TEXT,
    CHECK(page_start IS NULL OR page_end IS NULL OR page_end >= page_start),
    CHECK(paragraph_start IS NULL OR paragraph_end IS NULL OR paragraph_end >= paragraph_start),
    FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ux_chunks_document_index ON document_chunks(document_id, chunk_index);
CREATE INDEX ix_chunks_document ON document_chunks(document_id);