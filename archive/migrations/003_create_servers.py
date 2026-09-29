from __future__ import annotations

import sqlite3

from archive.migrations.common import migration, table_exists


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "003_create_servers.sql")
    if not table_exists(connection, "legacy_imports"):
        return
    connection.execute(
        """INSERT INTO servers(discord_server_id, name_current, first_seen_import_id, last_seen_import_id)
           SELECT server_id, MAX(server_name), MIN(import_id), MAX(import_id)
           FROM legacy_imports WHERE server_id IS NOT NULL AND server_id <> ''
           GROUP BY server_id"""
    )