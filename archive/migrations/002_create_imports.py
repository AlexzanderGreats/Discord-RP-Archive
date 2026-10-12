from __future__ import annotations

import sqlite3

from archive.migrations.common import migration, table_exists


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "002_create_imports.sql")
    if not table_exists(connection, "legacy_imports"):
        return
    rows = connection.execute("SELECT * FROM legacy_imports ORDER BY import_id").fetchall()
    for row in rows:
        source = connection.execute(
            """INSERT INTO sources(logical_source_key, source_kind, source_path, sha256,
                   byte_size, scope_json, registered_at)
               VALUES (?, 'discord_json', ?, ?, 0, '{}', ?)
               ON CONFLICT(logical_source_key, sha256) DO UPDATE SET source_id = source_id
               RETURNING source_id""",
            (f"discord:{row['source_sha256']}", row["source_file"], row["source_sha256"], row["imported_at_utc"]),
        ).fetchone()
        connection.execute(
            """INSERT INTO imports(import_id, source_id, started_at, finished_at, status,
                   importer_name, importer_version, records_seen, records_written, warnings_json)
               VALUES (?, ?, ?, ?, 'succeeded', 'legacy_json_importer', 'legacy', ?, ?, '{}')""",
            (row["import_id"], source[0], row["imported_at_utc"], row["imported_at_utc"],
             row["source_message_count"], row["imported_message_count"]),
        )