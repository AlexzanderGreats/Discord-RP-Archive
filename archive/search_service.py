from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from typing import Any


_QUESTION_STOP_WORDS = {
    "about", "according", "after", "am", "an", "and", "archive", "are", "as", "at", "during",
    "be", "been", "but", "by",
    "can", "did", "do", "does", "for", "from", "had", "happened", "has", "have",
    "he", "her", "immediately",
    "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on",
    "or", "our", "she", "that", "the", "their", "them", "there", "they", "this",
    "to", "up", "us", "was", "we", "were", "what", "when", "where", "which", "who",
    "whom", "why", "will", "with", "would", "you", "your",
}

_RELEVANCE_SEARCH_FIELDS = (
    "m.content_search",
    "normalize_search_text(m.author_discord_name_original)",
    "normalize_search_text(m.author_display_name_original)",
    "normalize_search_text(m.author_nickname_original)",
    "normalize_search_text(character.character_name)",
    "normalize_search_text(player.player_name)",
)


def _normalize_search_text(value: str | None) -> str:
    return "" if value is None else unicodedata.normalize("NFKC", value).casefold()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _term_match_sql(pattern_count: int) -> str:
    return "(" + " OR ".join(
        f"{field} LIKE ? ESCAPE '\\'"
        for _ in range(pattern_count)
        for field in _RELEVANCE_SEARCH_FIELDS
    ) + ")"


class SearchService:
    """Search current accepted observations by message text and identity metadata."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.create_function(
            "normalize_search_text", 1, _normalize_search_text, deterministic=True
        )

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

    def search_relevant(
        self,
        query: str,
        *,
        limit: int = 8,
        start_timestamp_utc: str | None = None,
        end_timestamp_utc: str | None = None,
    ) -> list[dict[str, Any]]:
        """Rank current messages by matching content, author, and attribution terms."""
        if limit < 1:
            raise ValueError("limit must be greater than zero")

        terms = list(dict.fromkeys(
            term for term in re.findall(r"[^\W_]{2,}", query.casefold())
            if term not in _QUESTION_STOP_WORDS
        ))[:10]
        if not terms:
            return []

        patterns = [f"%{_escape_like(term)}%" for term in terms]
        current_sources = """WITH current_sources AS (
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
           )"""
        term_counts_sql = ", ".join(
            "SUM(CASE WHEN m.content_search LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"
            for _ in terms
        )
        time_conditions: list[str] = []
        time_parameters: list[str] = []
        if start_timestamp_utc is not None:
            time_conditions.append("m.timestamp_utc >= ?")
            time_parameters.append(start_timestamp_utc)
        if end_timestamp_utc is not None:
            time_conditions.append("m.timestamp_utc < ?")
            time_parameters.append(end_timestamp_utc)
        time_predicate = " AND ".join(time_conditions)
        time_where = f"WHERE {time_predicate}" if time_predicate else ""
        term_stats = self.connection.execute(
            f"""{current_sources}
                SELECT COUNT(*), {term_counts_sql}
                FROM messages m
                JOIN current_sources current ON current.source_id = m.source_id
                {time_where}""",
            (*patterns, *time_parameters),
        ).fetchone()
        document_count = term_stats[0] or 0
        term_weights = [
            math.log((document_count + 1) / ((count or 0) + 1)) + 1
            for count in term_stats[1:]
        ]
        named_terms = {
            term.casefold()
            for term in re.findall(r"[^\W_]{2,}", query)
            if term[0].isupper() and term.casefold() not in _QUESTION_STOP_WORDS
        }
        term_patterns = [
            [pattern, f"%{_escape_like(term[:-1])}%"]
            if term in named_terms and len(term) >= 7
            else [pattern]
            for term, pattern in zip(terms, patterns)
        ]
        term_weights = [
            weight + (5 if term in named_terms else 0)
            for term, weight in zip(terms, term_weights)
        ]
        score_sql = " + ".join(
            f"CASE WHEN {_term_match_sql(len(patterns_for_term))} THEN ? ELSE 0 END"
            for patterns_for_term in term_patterns
        )
        match_sql = " OR ".join(
            _term_match_sql(len(patterns_for_term))
            for patterns_for_term in term_patterns
        )
        score_parameters: list[str | float] = []
        for patterns_for_term, weight in zip(term_patterns, term_weights):
            for pattern in patterns_for_term:
                score_parameters.extend([pattern] * len(_RELEVANCE_SEARCH_FIELDS))
            score_parameters.append(weight)
        match_parameters = [
            pattern
            for patterns_for_term in term_patterns
            for pattern in patterns_for_term
            for _field in _RELEVANCE_SEARCH_FIELDS
        ]
        cursor = self.connection.execute(
            f"""{current_sources}
               SELECT m.message_observation_id, m.discord_message_id, m.timestamp_utc,
                      m.content_original, m.author_discord_name_original,
                      m.author_display_name_original, m.author_nickname_original,
                      m.webhook_id_original, c.channel_id, c.name_current AS channel_name,
                      cat.category_id, cat.name_current AS category_name,
                      server.server_id, server.name_current AS server_name,
                      s.source_id, s.source_path,
                      a.character_id, character.character_name,
                      a.player_id, player.player_name,
                      ROUND(({score_sql}), 3) AS relevance_score
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
               WHERE {f'({time_predicate}) AND ' if time_predicate else ''}({match_sql})
               ORDER BY relevance_score DESC, m.timestamp_utc, m.message_observation_id
               LIMIT ?""",
                (*score_parameters, *time_parameters, *match_parameters, limit),
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]