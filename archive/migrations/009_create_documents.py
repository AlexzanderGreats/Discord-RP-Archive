from __future__ import annotations

import sqlite3

from archive.migrations.common import migration, table_exists


DOCUMENT_TYPES = {
    "unknown", "character_profile", "character_sheet", "lore", "worldbuilding", "story",
    "chapter", "rp_transcript", "rp_scene", "powerscaling", "analysis", "reference", "notes", "other",
}
DOCUMENT_STATUSES = {"historical", "current", "reference", "draft", "unknown"}
EXTRACTION_STATUSES = {"success", "partial", "no_text", "error"}


def _format(row: sqlite3.Row) -> str:
    candidates = (row["source_format"], row["source_extension"].lstrip("."))
    for candidate in candidates:
        normalized = candidate.casefold()
        if normalized in {"txt", "pdf", "docx"}:
            return normalized
    raise RuntimeError(f"Unsupported legacy document format: {row['source_format']!r}")


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "009_create_documents.sql")
    if not table_exists(connection, "legacy_documents"):
        return

    for row in connection.execute("SELECT * FROM legacy_documents ORDER BY document_id"):
        source_format = _format(row)
        source_key = f"document:{row['source_sha256']}"
        source = connection.execute(
            """INSERT INTO sources(logical_source_key, source_kind, source_path, sha256,
                   byte_size, scope_json, registered_at)
               VALUES (?, ?, ?, ?, ?, '{}', ?)
               ON CONFLICT(logical_source_key, sha256) DO UPDATE SET source_id = source_id
               RETURNING source_id""",
            (source_key, source_format, row["source_path"], row["source_sha256"],
             row["file_size_bytes"], row["imported_at_utc"]),
        ).fetchone()
        cursor = connection.execute(
            """INSERT INTO imports(source_id, started_at, finished_at, status, importer_name,
                   importer_version, records_seen, records_written, warnings_json)
               VALUES (?, ?, ?, 'succeeded', 'legacy_document_importer', 'legacy', 1, 1, ?)""",
            (source[0], row["imported_at_utc"], row["imported_at_utc"], row["warnings_json"] or "{}"),
        )
        import_id = cursor.lastrowid
        document_type = row["document_type"] if row["document_type"] in DOCUMENT_TYPES else "unknown"
        document_status = row["status"] if row["status"] in DOCUMENT_STATUSES else "unknown"
        extraction_status = row["extraction_status"]
        if extraction_status not in EXTRACTION_STATUSES:
            extraction_status = "no_text" if not row["content_text"] else "success"

        connection.execute(
            """INSERT INTO documents(
                   document_id, source_id, import_id, source_filename, source_format, file_size_bytes,
                   title_original, document_type, document_status, created_at_source, modified_at_source,
                   imported_at, page_count, paragraph_count, character_count, word_count,
                   extraction_status, extraction_error, content_text, content_search, metadata_json,
                   pages_json, blocks_json, warnings_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["document_id"], source[0], import_id, row["source_filename"], source_format,
             row["file_size_bytes"], row["title"], document_type, document_status,
             row["created_at_source"], row["modified_at_source"], row["imported_at_utc"],
             row["page_count"], row["paragraph_count"] or 0, row["character_count"] or 0,
             row["word_count"] or 0, extraction_status, row["extraction_error"], row["content_text"],
             row["content_search"], row["metadata_json"] or "{}", row["pages_json"], row["blocks_json"],
             row["warnings_json"] or "{}"),
        )