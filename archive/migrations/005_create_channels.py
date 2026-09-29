from __future__ import annotations

import sqlite3

from archive.migrations.common import migration, table_exists


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "005_create_channels.sql")
    if not table_exists(connection, "legacy_imports"):
        return
    connection.execute(
        """INSERT INTO channels(server_id, discord_channel_id, category_id, name_current,
                   first_seen_import_id, last_seen_import_id)
           SELECT s.server_id, li.channel_id, c.category_id, MAX(li.channel_name),
                  MIN(li.import_id), MAX(li.import_id)
           FROM legacy_imports li
           JOIN servers s ON s.discord_server_id = li.server_id
           LEFT JOIN categories c ON c.server_id = s.server_id
                                AND c.discord_category_id = li.category_id
           WHERE li.channel_id IS NOT NULL AND li.channel_id <> ''
           GROUP BY s.server_id, li.channel_id"""
    )