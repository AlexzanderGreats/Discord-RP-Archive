from __future__ import annotations

import sqlite3

from archive.migrations.common import migration, table_exists


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "008_migrate_messages.sql")
    if not table_exists(connection, "legacy_messages"):
        return

    author_rows = connection.execute("SELECT * FROM legacy_messages").fetchall()
    for row in author_rows:
        imported = connection.execute(
            "SELECT import_id, source_id FROM legacy_imports li JOIN imports i USING(import_id) WHERE li.source_sha256 = ?",
            (row["source_sha256"],),
        ).fetchone()
        if imported is None:
            raise RuntimeError(f"Legacy message {row['message_id']} has no matching import")
        identity_id = None
        if row["webhook_id"]:
            identity_type = "webhook"
            connection.execute(
                """INSERT OR IGNORE INTO discord_identities(identity_type, webhook_id, username_current,
                       display_name_current, first_seen_import_id, last_seen_import_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (identity_type, row["webhook_id"], row["author_discord_name"], row["author_display_name"],
                 imported["import_id"], imported["import_id"]),
            )
            identity_id = connection.execute(
                "SELECT identity_id FROM discord_identities WHERE webhook_id = ?", (row["webhook_id"],)
            ).fetchone()[0]
        elif row["author_discord_id"]:
            is_bot = row["author_is_bot"]
            identity_type = "bot" if is_bot else "user"
            connection.execute(
                """INSERT OR IGNORE INTO discord_identities(identity_type, discord_user_id, username_current,
                       display_name_current, is_bot, first_seen_import_id, last_seen_import_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (identity_type, row["author_discord_id"], row["author_discord_name"], row["author_display_name"],
                 is_bot, imported["import_id"], imported["import_id"]),
            )
            identity_id = connection.execute(
                "SELECT identity_id FROM discord_identities WHERE discord_user_id = ?",
                (row["author_discord_id"],),
            ).fetchone()[0]

        channel = None
        if row["channel_id"]:
            channel = connection.execute(
                """SELECT c.channel_id FROM channels c
                   JOIN servers s ON s.server_id = c.server_id
                   JOIN legacy_imports li ON li.server_id = s.discord_server_id
                   WHERE li.source_sha256 = ? AND c.discord_channel_id = ? LIMIT 1""",
                (row["source_sha256"], row["channel_id"]),
            ).fetchone()

        connection.execute(
            """INSERT INTO messages(
                   source_id, import_id, discord_message_id, channel_id, author_identity_id,
                   author_discord_user_id_original, author_discord_name_original,
                   author_display_name_original, author_nickname_original, author_is_bot_original,
                   webhook_id_original, message_type, timestamp_original, timestamp_utc,
                   timestamp_unix, timestamp_edited_original, content_original, content_search,
                   reply_to_discord_message_id, is_pinned, call_ended_timestamp_original,
                   attachments_json, embeds_json, stickers_json, reactions_json, mentions_json,
                   inline_emojis_json, source_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (imported["source_id"], imported["import_id"], row["message_id"], channel[0] if channel else None,
             identity_id, row["author_discord_id"], row["author_discord_name"], row["author_display_name"],
             row["author_nickname"], row["author_is_bot"], row["webhook_id"], row["message_type"],
             row["timestamp_original"], row["timestamp_utc"], row["timestamp_unix"], row["timestamp_edited"],
             row["content_original"], row["content_search"], row["reply_to_message_id"], row["is_pinned"],
             row["call_ended_timestamp"], row["attachments_json"], row["embeds_json"], row["stickers_json"],
             row["reactions_json"], row["mentions_json"], row["inline_emojis_json"], row["source_json"]),
        )