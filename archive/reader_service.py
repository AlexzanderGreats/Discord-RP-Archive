from __future__ import annotations

import sqlite3
from typing import Any


_CURRENT_SOURCES = """WITH current_sources AS (
    SELECT DISTINCT s.source_id
    FROM sources s
    JOIN imports i ON i.source_id = s.source_id
    WHERE i.accepted_at IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM sources newer
          JOIN imports newer_import ON newer_import.source_id = newer.source_id
          WHERE newer.supersedes_source_id = s.source_id
            AND newer_import.accepted_at IS NOT NULL
      )
)"""

_MESSAGE_FIELDS = """m.message_observation_id, m.discord_message_id, m.timestamp_utc,
    m.content_original, m.author_discord_name_original,
    m.author_display_name_original, m.author_nickname_original,
    m.webhook_id_original, c.channel_id, c.name_current AS channel_name,
    cat.category_id, cat.name_current AS category_name,
    server.server_id, server.name_current AS server_name,
    s.source_id, s.source_path, a.character_id, character.character_name,
    a.player_id, player.player_name"""

_MESSAGE_JOINS = """FROM messages m
    JOIN current_sources current ON current.source_id = m.source_id
    LEFT JOIN channels c ON c.channel_id = m.channel_id
    LEFT JOIN categories cat ON cat.category_id = c.category_id
    LEFT JOIN servers server ON server.server_id = c.server_id
    JOIN sources s ON s.source_id = m.source_id
    LEFT JOIN message_attributions a
      ON a.message_observation_id = m.message_observation_id
     AND a.status = 'accepted'
    LEFT JOIN characters character ON character.character_id = a.character_id
    LEFT JOIN players player ON player.player_id = a.player_id"""


class ReaderService:
    """Read a message with adjacent messages from its current channel capture."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_message_context(
        self,
        message_id: int,
        *,
        before: int = 10,
        after: int = 10,
    ) -> dict[str, Any] | None:
        if before < 0 or after < 0:
            raise ValueError("context sizes must not be negative")

        anchor = self._fetch_one(
            f"""{_CURRENT_SOURCES}
                SELECT {_MESSAGE_FIELDS}
                {_MESSAGE_JOINS}
                WHERE m.message_observation_id = ?""",
            (message_id,),
        )
        if anchor is None:
            return None

        earlier = self._fetch_many(
            f"""{_CURRENT_SOURCES}
                SELECT {_MESSAGE_FIELDS}
                {_MESSAGE_JOINS}
                WHERE m.channel_id = ?
                  AND (COALESCE(m.timestamp_utc, '') < COALESCE(?, '')
                       OR (COALESCE(m.timestamp_utc, '') = COALESCE(?, '')
                           AND m.message_observation_id < ?))
                ORDER BY COALESCE(m.timestamp_utc, '') DESC, m.message_observation_id DESC
                LIMIT ?""",
            (anchor["channel_id"], anchor["timestamp_utc"], anchor["timestamp_utc"], message_id, before),
        ) if before and anchor["channel_id"] is not None else []
        later = self._fetch_many(
            f"""{_CURRENT_SOURCES}
                SELECT {_MESSAGE_FIELDS}
                {_MESSAGE_JOINS}
                WHERE m.channel_id = ?
                  AND (COALESCE(m.timestamp_utc, '') > COALESCE(?, '')
                       OR (COALESCE(m.timestamp_utc, '') = COALESCE(?, '')
                           AND m.message_observation_id > ?))
                ORDER BY COALESCE(m.timestamp_utc, ''), m.message_observation_id
                LIMIT ?""",
            (anchor["channel_id"], anchor["timestamp_utc"], anchor["timestamp_utc"], message_id, after),
        ) if after and anchor["channel_id"] is not None else []

        return {"message": anchor, "before": list(reversed(earlier)), "after": later}

    def _fetch_one(self, sql: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        cursor = self.connection.execute(sql, parameters)
        row = cursor.fetchone()
        return None if row is None else dict(zip((column[0] for column in cursor.description), row))

    def _fetch_many(self, sql: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = self.connection.execute(sql, parameters)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]