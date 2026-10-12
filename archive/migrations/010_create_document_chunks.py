from __future__ import annotations

import sqlite3

from archive.migrations.common import migration, table_exists


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "010_create_document_chunks.sql")
    if not table_exists(connection, "legacy_document_chunks"):
        return
    connection.execute(
        """INSERT INTO document_chunks(
               chunk_id, document_id, chunk_index, page_start, page_end, paragraph_start,
               paragraph_end, section_title, content_text, content_search, source_location_json)
           SELECT chunk_id, document_id, chunk_index, page_start, page_end, paragraph_start,
                  paragraph_end, section_title, content_text, content_search, source_location_json
           FROM legacy_document_chunks ORDER BY chunk_id"""
    )