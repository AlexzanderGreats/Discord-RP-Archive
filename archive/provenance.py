from __future__ import annotations

import sqlite3
from collections.abc import Mapping


MESSAGE_FIELDS = frozenset(
    {
        "author_identity_id",
        "author_discord_user_id_original",
        "author_discord_name_original",
        "author_display_name_original",
        "author_nickname_original",
        "author_is_bot_original",
        "webhook_id_original",
        "message_type",
        "timestamp_original",
        "timestamp_utc",
        "timestamp_unix",
        "timestamp_edited_original",
        "timestamp_edited_utc",
        "channel_id",
        "reply_to_discord_message_id",
        "is_pinned",
        "call_ended_timestamp_original",
        "attachments_json",
        "embeds_json",
        "stickers_json",
        "reactions_json",
        "mentions_json",
        "inline_emojis_json",
    }
)
REQUIRED_MESSAGE_FIELDS = frozenset(
    {"discord_message_id", "content_original", "content_search", "source_json"}
)


def register_source_capture(
    connection: sqlite3.Connection,
    *,
    logical_source_key: str,
    source_kind: str,
    source_path: str,
    sha256: str,
    byte_size: int,
    scope_json: str,
    registered_at: str,
) -> int:
    """Register immutable capture bytes, returning an identical capture id on retry."""
    existing = connection.execute(
        """SELECT source_id, source_kind, source_path, byte_size, scope_json
           FROM sources WHERE logical_source_key = ? AND sha256 = ?""",
        (logical_source_key, sha256),
    ).fetchone()
    if existing is not None:
        expected = (source_kind, source_path, byte_size, scope_json)
        actual = tuple(existing[key] for key in ("source_kind", "source_path", "byte_size", "scope_json"))
        if actual != expected:
            raise ValueError("Capture hash is already registered with different immutable metadata")
        return existing["source_id"]

    cursor = connection.execute(
        """INSERT INTO sources(logical_source_key, source_kind, source_path, sha256,
               byte_size, scope_json, registered_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (logical_source_key, source_kind, source_path, sha256, byte_size, scope_json, registered_at),
    )
    return cursor.lastrowid


def start_import_run(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    started_at: str,
    importer_name: str,
    importer_version: str,
    config_sha256: str | None = None,
) -> int:
    cursor = connection.execute(
        """INSERT INTO imports(source_id, started_at, status, importer_name,
               importer_version, config_sha256, warnings_json)
           VALUES (?, ?, 'running', ?, ?, ?, '{}')""",
        (source_id, started_at, importer_name, importer_version, config_sha256),
    )
    return cursor.lastrowid


def record_message_observation(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    import_id: int,
    observation: Mapping[str, object],
) -> int:
    """Write one message payload observation belonging to an active import run."""
    unknown_fields = set(observation) - MESSAGE_FIELDS - REQUIRED_MESSAGE_FIELDS
    missing_fields = REQUIRED_MESSAGE_FIELDS - set(observation)
    if unknown_fields:
        raise ValueError(f"Unsupported message observation fields: {sorted(unknown_fields)}")
    if missing_fields:
        raise ValueError(f"Missing message observation fields: {sorted(missing_fields)}")

    import_row = connection.execute(
        "SELECT source_id, status FROM imports WHERE import_id = ?", (import_id,)
    ).fetchone()
    if import_row is None or import_row["source_id"] != source_id:
        raise ValueError("Import run does not belong to the supplied source capture")
    if import_row["status"] != "running":
        raise ValueError("Message observations can only be added to a running import")

    values = {"source_id": source_id, "import_id": import_id, **observation}
    columns = tuple(values)
    placeholders = ", ".join("?" for _ in columns)
    cursor = connection.execute(
        f"INSERT INTO messages({', '.join(columns)}) VALUES ({placeholders})",
        tuple(values[column] for column in columns),
    )
    return cursor.lastrowid


def finish_import_run(
    connection: sqlite3.Connection,
    *,
    import_id: int,
    status: str,
    finished_at: str,
    records_seen: int,
    records_written: int,
    warnings_json: str = "{}",
    error_summary: str | None = None,
) -> None:
    if status not in {"succeeded", "partial", "failed"}:
        raise ValueError("Import status must be succeeded, partial, or failed")
    updated = connection.execute(
        """UPDATE imports SET finished_at = ?, status = ?, records_seen = ?,
               records_written = ?, warnings_json = ?, error_summary = ?
           WHERE import_id = ? AND status = 'running' AND finished_at IS NULL""",
        (finished_at, status, records_seen, records_written, warnings_json, error_summary, import_id),
    )
    if updated.rowcount != 1:
        raise ValueError("Import run is missing or has already been finalized")


def _current_accepted_source_id(connection: sqlite3.Connection, logical_source_key: str) -> int | None:
    accepted_sources = {
        row["source_id"]: row["supersedes_source_id"]
        for row in connection.execute(
            """SELECT DISTINCT s.source_id, s.supersedes_source_id
               FROM sources s JOIN imports i ON i.source_id = s.source_id
               WHERE s.logical_source_key = ? AND i.accepted_at IS NOT NULL""",
            (logical_source_key,),
        )
    }
    superseded_ids = {
        supersedes_source_id
        for supersedes_source_id in accepted_sources.values()
        if supersedes_source_id is not None
    }
    current_ids = set(accepted_sources) - superseded_ids
    if len(current_ids) > 1:
        raise RuntimeError("Logical source has multiple current accepted captures")
    if accepted_sources and not current_ids:
        raise RuntimeError("Logical source capture history has no current capture")
    return next(iter(current_ids), None)


def current_accepted_source_id(
    connection: sqlite3.Connection, logical_source_key: str
) -> int | None:
    """Return the accepted capture at the end of a logical source's history chain."""
    return _current_accepted_source_id(connection, logical_source_key)


def accept_import_run(
    connection: sqlite3.Connection,
    *,
    import_id: int,
    accepted_at: str,
) -> None:
    """Accept a successful import and link its capture to the prior accepted capture."""
    if not accepted_at:
        raise ValueError("accepted_at is required")

    connection.execute("SAVEPOINT accept_import_run")
    try:
        row = connection.execute(
            """SELECT i.status, i.finished_at, i.accepted_at, s.source_id,
                      s.logical_source_key, s.supersedes_source_id
               FROM imports i JOIN sources s ON s.source_id = i.source_id
               WHERE i.import_id = ?""",
            (import_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Import run does not exist")
        if row["status"] != "succeeded" or row["finished_at"] is None:
            raise ValueError("Only a finished successful import can be accepted")
        if row["accepted_at"] is not None:
            raise ValueError("Import acceptance is immutable")

        current_source_id = _current_accepted_source_id(connection, row["logical_source_key"])
        source_id = row["source_id"]
        if current_source_id != source_id:
            was_superseded = connection.execute(
                """SELECT 1 FROM sources s JOIN imports i ON i.source_id = s.source_id
                   WHERE s.supersedes_source_id = ? AND i.accepted_at IS NOT NULL LIMIT 1""",
                (source_id,),
            ).fetchone()
            if was_superseded is not None:
                raise ValueError("A superseded capture cannot become current again")
            if row["supersedes_source_id"] is not None:
                raise ValueError("A superseded capture cannot become current again")
            if current_source_id is not None:
                connection.execute(
                    "UPDATE sources SET supersedes_source_id = ? WHERE source_id = ?",
                    (current_source_id, source_id),
                )

        connection.execute(
            "UPDATE imports SET accepted_at = ? WHERE import_id = ?",
            (accepted_at, import_id),
        )
        connection.execute("RELEASE SAVEPOINT accept_import_run")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT accept_import_run")
        connection.execute("RELEASE SAVEPOINT accept_import_run")
        raise