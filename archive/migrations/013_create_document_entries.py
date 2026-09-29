from __future__ import annotations

import hashlib
import json
import sqlite3

from archive.migrations.common import columns, migration, table_exists


def _document_logical_key(source_path: str) -> str:
    identity = json.dumps(
        {"source_path": source_path, "scope": {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "document:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _migrate_legacy_entries(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "legacy_document_entries"):
        return

    legacy_columns = columns(connection, "legacy_document_entries")
    if "document_id" not in legacy_columns:
        return

    order_column = "entry_index" if "entry_index" in legacy_columns else "entry_id"
    order_clause = f' ORDER BY "{order_column}"' if order_column in legacy_columns else ""
    rows = connection.execute(f'SELECT * FROM legacy_document_entries{order_clause}').fetchall()
    fallback_index: dict[int, int] = {}

    for row in rows:
        document_id = row["document_id"]
        exists = connection.execute(
            "SELECT 1 FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if exists is None:
            continue

        entry_index = row["entry_index"] if "entry_index" in legacy_columns else fallback_index.get(document_id, 0)
        fallback_index[document_id] = entry_index + 1
        fields = (
            row["entry_id"] if "entry_id" in legacy_columns else None,
            document_id,
            entry_index,
            row["speaker_raw"] if "speaker_raw" in legacy_columns else None,
            row["character_id"] if "character_id" in legacy_columns else None,
            row["player_id"] if "player_id" in legacy_columns else None,
            row["content_text"] if "content_text" in legacy_columns else "",
            row["source_location_json"] if "source_location_json" in legacy_columns else None,
        )
        connection.execute(
            """INSERT INTO document_entries(
                   entry_id, document_id, entry_index, speaker_raw, character_id,
                   player_id, content_text, source_location_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            fields,
        )


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "013_create_document_entries.sql")

    for row in connection.execute(
        """SELECT source_id, source_path FROM sources
           WHERE source_kind IN ('txt', 'pdf', 'docx') ORDER BY source_id"""
    ).fetchall():
        connection.execute(
            "UPDATE sources SET logical_source_key = ?, scope_json = '{}' WHERE source_id = ?",
            (_document_logical_key(row["source_path"]), row["source_id"]),
        )

    _migrate_legacy_entries(connection)