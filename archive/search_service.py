from __future__ import annotations

import re
import sqlite3
from typing import Any


_QUESTION_STOP_WORDS = {
    "about", "am", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "did", "do", "does", "for", "from", "had", "has", "have", "he", "her",
    "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on",
    "or", "our", "she", "that", "the", "their", "them", "there", "they", "this",
    "to", "up", "us", "was", "we", "were", "what", "when", "where", "which", "who",
    "whom", "why", "will", "with", "would", "you", "your",
}


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SearchService:
    """Search the current accepted observations in an archive database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be greater than zero")
        if offset < 0:
            raise ValueError("offset must not be negative")

        normalized_query = query.casefold().strip()
        if not normalized_query:
            return []

        escaped_query = _escape_like(normalized_query)
        cursor = self.connection.execute(
            """WITH current_sources AS (
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
               )
               SELECT m.message_observation_id, m.discord_message_id, m.timestamp_utc,
                      m.content_original, m.author_discord_name_original,
                      m.author_display_name_original, m.author_nickname_original,
                      m.webhook_id_original, c.channel_id, c.name_current AS channel_name,
                      cat.category_id, cat.name_current AS category_name,
                      server.server_id, server.name_current AS server_name,
                      s.source_id, s.source_path,
                      a.character_id, character.character_name,
                      a.player_id, player.player_name
               FROM messages m
               JOIN current_sources current ON current.source_id = m.source_id
               LEFT JOIN channels c ON c.channel_id = m.channel_id
               LEFT JOIN categories cat ON cat.category_id = c.category_id
               LEFT JOIN servers server ON server.server_id = c.server_id
               JOIN sources s ON s.source_id = m.source_id
               LEFT JOIN message_attributions a
                 ON a.message_observation_id = m.message_observation_id
                AND a.status = 'accepted'
               LEFT JOIN characters character ON character.character_id = a.character_id
               LEFT JOIN players player ON player.player_id = a.player_id
               WHERE m.content_search LIKE ? ESCAPE '\\'
               ORDER BY m.timestamp_utc, m.message_observation_id
               LIMIT ? OFFSET ?""",
            (f"%{escaped_query}%", limit, offset),
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def search_relevant(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Rank current messages by the number of question terms they contain."""
        if limit < 1:
            raise ValueError("limit must be greater than zero")

        terms = list(dict.fromkeys(
            term for term in re.findall(r"[^\W_]{2,}", query.casefold())
            if term not in _QUESTION_STOP_WORDS
        ))[:10]
        if not terms:
            return []

        patterns = [f"%{_escape_like(term)}%" for term in terms]
        score_sql = " + ".join(
            "CASE WHEN m.content_search LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
            for _ in terms
        )
        match_sql = " OR ".join("m.content_search LIKE ? ESCAPE '\\'" for _ in terms)
        cursor = self.connection.execute(
            f"""WITH current_sources AS (
                   SELECT DISTINCT s.source_id
                   FROM sources s
                   JOIN imports i ON i.source_id = s.source_id
                   WHERE i.accepted_at IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM sources newer
                         JOIN imports newer_import ON newer_import.source_id = newer.source_id
                         WHERE newer.supersedes_source_id = s.source_id
                           AND newer_import.accepted_at IS NOT NULL
                     )
               )
               SELECT m.message_observation_id, m.discord_message_id, m.timestamp_utc,
                      m.content_original, m.author_discord_name_original,
                      m.author_display_name_original, m.author_nickname_original,
                      m.webhook_id_original, c.channel_id, c.name_current AS channel_name,
                      cat.category_id, cat.name_current AS category_name,
                      server.server_id, server.name_current AS server_name,
                      s.source_id, s.source_path,
                      a.character_id, character.character_name,
                      a.player_id, player.player_name,
                      ({score_sql}) AS relevance_score
               FROM messages m
               JOIN current_sources current ON current.source_id = m.source_id
               LEFT JOIN channels c ON c.channel_id = m.channel_id
               LEFT JOIN categories cat ON cat.category_id = c.category_id
               LEFT JOIN servers server ON server.server_id = c.server_id
               JOIN sources s ON s.source_id = m.source_id
               LEFT JOIN message_attributions a
                 ON a.message_observation_id = m.message_observation_id
                AND a.status = 'accepted'
               LEFT JOIN characters character ON character.character_id = a.character_id
               LEFT JOIN players player ON player.player_id = a.player_id
               WHERE {match_sql}
               ORDER BY relevance_score DESC, m.timestamp_utc, m.message_observation_id
               LIMIT ?""",
            (*patterns, *patterns, limit),
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]