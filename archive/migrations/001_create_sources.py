from __future__ import annotations

import sqlite3

from archive.migrations.common import migration


def _stage_legacy_tables(connection: sqlite3.Connection) -> None:
    objects = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    candidates = (
        ("documents_fts", "legacy_documents_fts"),
        ("document_entries", "legacy_document_entries"),
        ("document_chunks", "legacy_document_chunks"),
        ("documents", "legacy_documents"),
        ("messages", "legacy_messages"),
        ("imports", "legacy_imports"),
    )
    for old_name, new_name in candidates:
        if old_name not in objects:
            continue
        if new_name in objects:
            raise RuntimeError(f"Both {old_name} and {new_name} exist; refusing to guess")
        connection.execute(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"')
        objects.remove(old_name)
        objects.add(new_name)

def upgrade(connection: sqlite3.Connection) -> None:
    _stage_legacy_tables(connection)
    migration(connection, "001_create_sources.sql")