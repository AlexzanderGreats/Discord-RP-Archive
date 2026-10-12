from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


ATTRIBUTION_METHODS = {"explicit_marker", "identity_link", "rule", "manual", "model"}
_MARKER_PATTERNS = (
    re.compile(r"^\[([^\]\r\n]+)\]\s*$"),
    re.compile(r"^(.+?):\s*$"),
    re.compile(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alias_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_player(
    connection: sqlite3.Connection,
    name: str,
    *,
    notes: str | None = None,
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Player name must not be empty")
    now = _now()
    cursor = connection.execute(
        "INSERT INTO players(player_name, notes, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, notes, now, now),
    )
    return int(cursor.lastrowid)


def create_character(
    connection: sqlite3.Connection,
    name: str,
    *,
    notes: str | None = None,
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Character name must not be empty")
    now = _now()
    cursor = connection.execute(
        "INSERT INTO characters(character_name, notes, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, notes, now, now),
    )
    character_id = int(cursor.lastrowid)
    add_character_alias(connection, character_id, name)
    return character_id


def add_character_alias(
    connection: sqlite3.Connection,
    character_id: int,
    alias: str,
) -> int:
    alias = alias.strip()
    alias_key = _alias_key(alias)
    if not alias_key:
        raise ValueError("Character alias must not be empty")
    cursor = connection.execute(
        """INSERT INTO character_aliases(character_id, alias, alias_key, created_at)
           VALUES (?, ?, ?, ?)""",
        (character_id, alias, alias_key, _now()),
    )
    return int(cursor.lastrowid)


def link_identity_to_player(
    connection: sqlite3.Connection,
    identity_id: int,
    player_id: int,
    *,
    valid_from: str | None = None,
    valid_to: str | None = None,
    created_by: str | None = None,
    notes: str | None = None,
) -> int:
    cursor = connection.execute(
        """INSERT INTO player_discord_identities(
               player_id, identity_id, valid_from, valid_to, created_at, created_by, notes
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (player_id, identity_id, valid_from, valid_to, _now(), created_by, notes),
    )
    return int(cursor.lastrowid)


def link_player_to_character(
    connection: sqlite3.Connection,
    player_id: int,
    character_id: int,
    *,
    valid_from: str | None = None,
    valid_to: str | None = None,
    created_by: str | None = None,
    notes: str | None = None,
) -> int:
    cursor = connection.execute(
        """INSERT INTO player_characters(
               player_id, character_id, valid_from, valid_to, created_at, created_by, notes
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (player_id, character_id, valid_from, valid_to, _now(), created_by, notes),
    )
    return int(cursor.lastrowid)


def _insert_proposal(
    connection: sqlite3.Connection,
    *,
    message_observation_id: int,
    character_id: int | None,
    player_id: int | None,
    method: str,
    confidence: float | None,
    evidence: dict[str, Any],
    created_by: str | None,
) -> int:
    if method not in ATTRIBUTION_METHODS:
        raise ValueError(f"Unsupported attribution method: {method}")
    evidence_json = _canonical_json(evidence)
    existing = connection.execute(
        """SELECT attribution_id FROM message_attributions
              WHERE message_observation_id = ? AND method = ?
                 AND character_id IS ? AND player_id IS ? AND evidence_json = ?""",
          (message_observation_id, method, character_id, player_id, evidence_json),
    ).fetchone()
    if existing is not None:
        return int(existing[0])

    now = _now()
    cursor = connection.execute(
        """INSERT INTO message_attributions(
               message_observation_id, character_id, player_id, method, confidence,
               status, evidence_json, created_at, created_by
           ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (
            message_observation_id,
            character_id,
            player_id,
            method,
            confidence,
            evidence_json,
            now,
            created_by,
        ),
    )
    attribution_id = int(cursor.lastrowid)
    connection.execute(
        """INSERT INTO attribution_events(
               attribution_id, event_type, previous_status, new_status,
               happened_at, actor, detail_json
           ) VALUES (?, 'proposed', NULL, 'pending', ?, ?, '{}')""",
        (attribution_id, now, created_by),
    )
    return attribution_id


def propose_attribution(
    connection: sqlite3.Connection,
    message_observation_id: int,
    *,
    character_id: int | None = None,
    player_id: int | None = None,
    method: str = "manual",
    confidence: float | None = None,
    evidence: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> int:
    """Create a pending manual, rule, or model claim; never auto-accept it."""
    return _insert_proposal(
        connection,
        message_observation_id=message_observation_id,
        character_id=character_id,
        player_id=player_id,
        method=method,
        confidence=confidence,
        evidence=evidence or {},
        created_by=created_by,
    )


def _message_marker(content: str) -> str | None:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    for pattern_index, pattern in enumerate(_MARKER_PATTERNS):
        if pattern_index == 2 and len(first_line) >= 140:
            continue
        match = pattern.fullmatch(first_line)
        if match:
            return match.group(1).strip()
    return None


def _active_rows(
    connection: sqlite3.Connection,
    table: str,
    where_sql: str,
    values: tuple[Any, ...],
    timestamp: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        f"""SELECT * FROM {table}
            WHERE {where_sql}
              AND (valid_from IS NULL OR valid_from <= ?)
              AND (valid_to IS NULL OR valid_to > ?)
            ORDER BY link_id""",
        (*values, timestamp, timestamp),
    ).fetchall()


def propose_message_attributions(
    connection: sqlite3.Connection,
    message_observation_id: int | None = None,
    *,
    created_by: str = "attribution_rules",
) -> list[int]:
    """Create pending exact-marker or identity-link proposals for messages."""
    query = """SELECT message_observation_id, author_identity_id, content_original, timestamp_utc
               FROM messages"""
    parameters: tuple[Any, ...] = ()
    if message_observation_id is not None:
        query += " WHERE message_observation_id = ?"
        parameters = (message_observation_id,)
    query += " ORDER BY message_observation_id"
    messages = connection.execute(query, parameters).fetchall()
    proposal_ids: list[int] = []

    for message in messages:
        message_id = int(message["message_observation_id"])
        timestamp = message["timestamp_utc"] or _now()
        marker = _message_marker(message["content_original"])
        matched_characters: list[sqlite3.Row] = []
        if marker:
            matched_characters = connection.execute(
                "SELECT character_id, alias FROM character_aliases WHERE alias_key = ? ORDER BY character_id",
                (_alias_key(marker),),
            ).fetchall()

        if matched_characters:
            for matched in matched_characters:
                character_id = int(matched["character_id"])
                linked_players: list[int] = []
                if message["author_identity_id"] is not None:
                    identity_links = _active_rows(
                        connection,
                        "player_discord_identities",
                        "identity_id = ?",
                        (message["author_identity_id"],),
                        timestamp,
                    )
                    for identity_link in identity_links:
                        character_links = _active_rows(
                            connection,
                            "player_characters",
                            "player_id = ? AND character_id = ?",
                            (identity_link["player_id"], character_id),
                            timestamp,
                        )
                        if character_links:
                            linked_players.append(int(identity_link["player_id"]))
                player_id = linked_players[0] if len(set(linked_players)) == 1 else None
                proposal_ids.append(
                    _insert_proposal(
                        connection,
                        message_observation_id=message_id,
                        character_id=character_id,
                        player_id=player_id,
                        method="explicit_marker",
                        confidence=1.0,
                        evidence={
                            "rule": "leading_speaker_marker_exact_alias_v1",
                            "marker": marker,
                            "matched_alias": matched["alias"],
                        },
                        created_by=created_by,
                    )
                )
            continue

        if message["author_identity_id"] is None:
            continue
        identity_links = _active_rows(
            connection,
            "player_discord_identities",
            "identity_id = ?",
            (message["author_identity_id"],),
            timestamp,
        )
        for identity_link in identity_links:
            proposal_ids.append(
                _insert_proposal(
                    connection,
                    message_observation_id=message_id,
                    character_id=None,
                    player_id=int(identity_link["player_id"]),
                    method="identity_link",
                    confidence=1.0,
                    evidence={
                        "rule": "message_author_identity_link_v1",
                        "identity_id": int(message["author_identity_id"]),
                        "link_id": int(identity_link["link_id"]),
                    },
                    created_by=created_by,
                )
            )
    return proposal_ids


def list_pending_attributions(
    connection: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[sqlite3.Row]:
    if limit < 1:
        raise ValueError("Limit must be positive")
    return connection.execute(
        """SELECT a.*, m.discord_message_id, m.timestamp_utc, m.content_original,
                  m.author_discord_user_id_original, p.player_name, c.character_name
           FROM message_attributions a
           JOIN messages m ON m.message_observation_id = a.message_observation_id
           LEFT JOIN players p ON p.player_id = a.player_id
           LEFT JOIN characters c ON c.character_id = a.character_id
           WHERE a.status = 'pending'
           ORDER BY a.created_at, a.attribution_id
           LIMIT ?""",
        (limit,),
    ).fetchall()


def review_attribution(
    connection: sqlite3.Connection,
    attribution_id: int,
    *,
    decision: str,
    actor: str,
    note: str | None = None,
) -> None:
    """Accept or reject a pending claim and append immutable audit events."""
    if decision not in {"accepted", "rejected"}:
        raise ValueError("Decision must be 'accepted' or 'rejected'")
    if not actor.strip():
        raise ValueError("Reviewer identity must not be empty")
    attribution = connection.execute(
        "SELECT message_observation_id, status FROM message_attributions WHERE attribution_id = ?",
        (attribution_id,),
    ).fetchone()
    if attribution is None:
        raise ValueError(f"Attribution {attribution_id} does not exist")
    if attribution["status"] != "pending":
        raise ValueError(f"Attribution {attribution_id} is not pending")

    now = _now()
    detail_json = _canonical_json({"note": note} if note is not None else {})
    if decision == "accepted":
        accepted = connection.execute(
            """SELECT attribution_id FROM message_attributions
               WHERE message_observation_id = ? AND status = 'accepted'""",
            (attribution["message_observation_id"],),
        ).fetchone()
        if accepted is not None:
            prior_id = int(accepted["attribution_id"])
            connection.execute(
                "UPDATE message_attributions SET status = 'superseded' WHERE attribution_id = ?",
                (prior_id,),
            )
            connection.execute(
                """INSERT INTO attribution_events(
                       attribution_id, event_type, previous_status, new_status,
                       happened_at, actor, detail_json
                   ) VALUES (?, 'superseded', 'accepted', 'superseded', ?, ?, ?)""",
                (prior_id, now, actor, _canonical_json({"superseded_by": attribution_id})),
            )

    connection.execute(
        """UPDATE message_attributions
           SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
           WHERE attribution_id = ?""",
        (decision, actor, now, note, attribution_id),
    )
    connection.execute(
        """INSERT INTO attribution_events(
               attribution_id, event_type, previous_status, new_status,
               happened_at, actor, detail_json
           ) VALUES (?, ?, 'pending', ?, ?, ?, ?)""",
        (attribution_id, decision, decision, now, actor, detail_json),
    )