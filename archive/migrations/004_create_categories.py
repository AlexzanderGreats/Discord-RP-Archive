from __future__ import annotations

import sqlite3

from archive.migrations.common import migration, table_exists


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "004_create_categories.sql")
    if not table_exists(connection, "legacy_imports"):
        return
    connection.execute(
        """INSERT INTO categories(server_id, discord_category_id, name_current,
                   first_seen_import_id, last_seen_import_id)
           SELECT s.server_id, li.category_id, MAX(li.category_name), MIN(li.import_id), MAX(li.import_id)
           FROM legacy_imports li
           JOIN servers s ON s.discord_server_id = li.server_id
           WHERE li.category_id IS NOT NULL AND li.category_id <> ''
           GROUP BY s.server_id, li.category_id"""
    )