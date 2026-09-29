from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_arc(
    connection: sqlite3.Connection,
    name: str,
    *,
    description: str | None = None,
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Arc name must not be empty")
    now = _now()
    cursor = connection.execute(
        "INSERT INTO arcs(arc_name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, description, now, now),
    )
    return int(cursor.lastrowid)


def create_scene(
    connection: sqlite3.Connection,
    name: str,
    *,
    arc_id: int | None = None,
    description: str | None = None,
    fictional_time: str | None = None,
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Scene name must not be empty")
    now = _now()
    cursor = connection.execute(
        """INSERT INTO scenes(arc_id, scene_name, description, fictional_time, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (arc_id, name, description, fictional_time, now, now),
    )
    return int(cursor.lastrowid)


def add_message_to_scene(
    connection: sqlite3.Connection,
    scene_id: int,
    message_observation_id: int,
    *,
    position: int | None = None,
    added_by: str | None = None,
    notes: str | None = None,
) -> int:
    existing = connection.execute(
        "SELECT scene_message_id FROM scene_messages WHERE scene_id = ? AND message_observation_id = ?",
        (scene_id, message_observation_id),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    if position is None:
        position = int(connection.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM scene_messages WHERE scene_id = ?",
            (scene_id,),
        ).fetchone()[0])
    if position < 0:
        raise ValueError("Scene message position must not be negative")
    cursor = connection.execute(
        """INSERT INTO scene_messages(
               scene_id, message_observation_id, position, added_at, added_by, notes
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (scene_id, message_observation_id, position, _now(), added_by, notes),
    )
    return int(cursor.lastrowid)


def remove_message_from_scene(
    connection: sqlite3.Connection,
    scene_id: int,
    message_observation_id: int,
) -> bool:
    cursor = connection.execute(
        "DELETE FROM scene_messages WHERE scene_id = ? AND message_observation_id = ?",
        (scene_id, message_observation_id),
    )
    return cursor.rowcount > 0


def add_scene_participant(
    connection: sqlite3.Connection,
    scene_id: int,
    *,
    character_id: int | None = None,
    player_id: int | None = None,
    added_by: str | None = None,
    notes: str | None = None,
) -> int:
    if character_id is None and player_id is None:
        raise ValueError("A scene participant must identify a character or player")
    existing = connection.execute(
        """SELECT scene_participant_id FROM scene_participants
           WHERE scene_id = ? AND character_id IS ? AND player_id IS ?""",
        (scene_id, character_id, player_id),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    cursor = connection.execute(
        """INSERT INTO scene_participants(
               scene_id, character_id, player_id, added_at, added_by, notes
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (scene_id, character_id, player_id, _now(), added_by, notes),
    )
    return int(cursor.lastrowid)


def list_scene_messages(connection: sqlite3.Connection, scene_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT sm.position, sm.notes AS membership_notes,
                  m.message_observation_id, m.discord_message_id, m.timestamp_utc,
                  m.author_identity_id, m.author_discord_user_id_original,
                  m.author_discord_name_original, m.author_display_name_original,
                  m.channel_id, c.discord_channel_id, c.name_current AS channel_name,
                  accepted.character_id, ch.character_name,
                  accepted.player_id, p.player_name
           FROM scene_messages sm
           JOIN messages m ON m.message_observation_id = sm.message_observation_id
           LEFT JOIN channels c ON c.channel_id = m.channel_id
           LEFT JOIN message_attributions accepted
                  ON accepted.message_observation_id = m.message_observation_id
                 AND accepted.status = 'accepted'
           LEFT JOIN characters ch ON ch.character_id = accepted.character_id
           LEFT JOIN players p ON p.player_id = accepted.player_id
           WHERE sm.scene_id = ?
           ORDER BY sm.position""",
        (scene_id,),
    ).fetchall()