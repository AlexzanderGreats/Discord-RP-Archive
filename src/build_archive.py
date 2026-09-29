#!/usr/bin/env python3
"""Import DiscordChatExporter JSON as immutable source captures and observations."""

from __future__ import annotations
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from archive.migrations.runner import run_migrations
from archive.provenance import (
    accept_import_run,
    finish_import_run,
    record_message_observation,
    register_source_capture,
    start_import_run,
)

ARCHIVE_ROOT = PROJECT_ROOT
RAW_ROOT = ARCHIVE_ROOT / "01_Raw"
PROCESSED_ROOT = ARCHIVE_ROOT / "02_Processed"
DATABASE_PATH = PROCESSED_ROOT / "archive.sqlite3"
MANIFEST_PATH = PROCESSED_ROOT / "import-manifest.json"


@dataclass
class NormalizedMessage:
    message_id: str | None
    message_type: str | None
    timestamp_original: str | None
    timestamp_utc: str | None
    timestamp_unix: float | None
    timestamp_edited: str | None
    timestamp_edited_utc: str | None
    category_name: str | None
    category_id: str | None
    channel_name: str | None
    channel_id: str | None
    thread_name: str | None
    author_discord_id: str | None
    author_discord_name: str | None
    author_display_name: str | None
    author_nickname: str | None
    author_is_bot: int | None
    webhook_id: str | None
    content_original: str
    content_search: str
    reply_to_message_id: str | None
    is_pinned: int | None
    call_ended_timestamp: str | None
    attachments_json: str | None
    embeds_json: str | None
    stickers_json: str | None
    reactions_json: str | None
    mentions_json: str | None
    inline_emojis_json: str | None
    source_file: str
    source_sha256: str
    source_json: str


def ensure_directories() -> None:
    PROCESSED_ROOT.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def stringify_id(value: Any) -> str | None:
    return None if value is None else str(value)


def clean_search_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def normalize_timestamp(value: Any) -> tuple[str | None, float | None]:
    """Convert an exporter timestamp into UTC ISO-8601 and Unix time."""
    if value is None:
        return None, None

    if isinstance(value, bool):
        raise ValueError("timestamp must not be a boolean")
    if isinstance(value, (int, float)):
        timestamp = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return timestamp.isoformat(), float(value)
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string or number, got {type(value).__name__}")

    value = value.strip()
    if not value:
        return None, None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.isoformat(), timestamp.timestamp()
# ---------------------------------------------------------------------------
# DiscordChatExporter structure discovery
# ---------------------------------------------------------------------------

def get_message_list(document: Any) -> list[dict[str, Any]]:
    """
    Locate the message collection.

    DiscordChatExporter commonly places messages under a top-level
    'messages' property, but this function is intentionally defensive.
    """

    if isinstance(document, dict):

        messages = document.get("messages")

        if isinstance(messages, list):
            invalid_indexes = [
                index
                for index, item in enumerate(messages)
                if not isinstance(item, dict)
            ]

            if invalid_indexes:
                raise ValueError(
                    "Message collection contains non-object entries at "
                    f"indexes {invalid_indexes}"
                )

            return messages

    if isinstance(document, list):
        if not all(isinstance(item, dict) for item in document):
            raise ValueError(
                "JSON message array contains a non-object entry"
            )

        return document

    raise ValueError(
        "Could not locate a message list in the JSON export. "
        "Expected a top-level 'messages' array or a JSON array."
    )


def get_metadata(
    document: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Extract channel/server metadata where available.

    This deliberately does not invent metadata that is absent.
    """

    channel = document.get("channel")
    guild = document.get("guild")

    if not isinstance(channel, dict):
        channel = {}

    if not isinstance(guild, dict):
        guild = {}

    # Some exporter structures may place information elsewhere.
    return {
        "server_name": first_value(
            guild,
            "name",
            "guild_name",
        ),
        "server_id": stringify_id(
            first_value(
                guild,
                "id",
                "guild_id",
            )
        ),
        "category_name": first_value(
            channel,
            "category",
            "category_name",
        ),
        "category_id": stringify_id(
            first_value(
                channel,
                "categoryId",
                "category_id",
            )
        ),
        "channel_name": first_value(
            channel,
            "name",
            "channel_name",
        ),
        "channel_id": stringify_id(
            first_value(
                channel,
                "id",
                "channel_id",
            )
        ),
    }


def validate_server_map(
    metadata: dict[str, Any],
    server_map_path: Path | None,
) -> None:
    """Report ID mismatches without replacing JSON metadata."""

    if server_map_path is None:
        return

    if not server_map_path.exists():
        raise FileNotFoundError(
            f"Server map does not exist: {server_map_path}"
        )

    text = server_map_path.read_text(encoding="utf-8")
    expected_ids = {
        value
        for value in re.findall(
            r"(?:Category ID #|Channel ID #):\s*([0-9]+)",
            text,
        )
    }

    for label, value in (
        ("category", metadata["category_id"]),
        ("channel", metadata["channel_id"]),
    ):
        if value is not None and value not in expected_ids:
            print(
                f"  WARNING: {label} ID {value} is not present in "
                f"server map {server_map_path}"
            )


# ---------------------------------------------------------------------------
# Author extraction
# ---------------------------------------------------------------------------

def extract_author(
    message: dict[str, Any],
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
    str | None,
]:
    """
    Extract Discord author information.

    Returns:
        author_id,
        author_username,
        author_display_name,
        author_nickname,
        author_is_bot,
        webhook_id
    """

    author = message.get("author")

    if not isinstance(author, dict):
        author = {}

    author_id = stringify_id(
        first_value(
            author,
            "id",
            "author_id",
        )
    )

    username = first_value(
        author,
        "name",
        "username",
        "author_name",
    )

    nickname = author.get("nickname")

    display_name = first_value(
        author,
        "display_name",
        "global_name",
    )

    author_is_bot = normalize_boolean(author.get("isBot"))

    webhook_id = stringify_id(
        first_value(
            message,
            "webhookId",
            "webhook_id",
        )
    )

    return (
        author_id,
        str(username) if username is not None else None,
        str(display_name) if display_name is not None else None,
        str(nickname) if nickname is not None else None,
        author_is_bot,
        webhook_id,
    )


# ---------------------------------------------------------------------------
# Reply extraction
# ---------------------------------------------------------------------------

def extract_reply_id(message: dict[str, Any]) -> str | None:
    """Extract the referenced message ID when available."""

    # Direct fields.
    direct = first_value(
        message,
        "reply_to_message_id",
        "replyToMessageId",
    )

    if direct is not None:
        return stringify_id(direct)

    # Nested reply structures.
    for key in ("reference", "reply", "referencedMessage"):
        value = message.get(key)

        if isinstance(value, dict):
            reply_id = first_value(
                value,
                "messageId",
                "message_id",
                "id",
            )

            if reply_id is not None:
                return stringify_id(reply_id)

    return None


# ---------------------------------------------------------------------------
# Attachments / embeds
# ---------------------------------------------------------------------------

def extract_json_field(
    message: dict[str, Any],
    *keys: str,
) -> str | None:
    """Serialize a JSON field while preserving its structure."""

    present_key = next((key for key in keys if key in message), None)

    if present_key is None or message[present_key] is None:
        return None

    value = message[present_key]

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    except TypeError:
        return json.dumps(
            str(value),
            ensure_ascii=False,
        )


def normalize_boolean(value: Any) -> int | None:
    """Normalize an explicitly provided boolean to SQLite's 0/1 values."""

    if value is None:
        return None

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, int) and value in (0, 1):
        return value

    raise ValueError(
        f"expected a boolean or 0/1 integer, got {value!r}"
    )


def validate_json_value(value: str | None, field_name: str) -> None:
    """Ensure a populated JSON column contains valid JSON."""

    if value is None:
        return

    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {field_name}: {exc.msg}") from exc


# ---------------------------------------------------------------------------
# Message normalization
# ---------------------------------------------------------------------------

def normalize_message(
    message: dict[str, Any],
    metadata: dict[str, Any],
    source_file: str,
    source_sha256: str,
) -> NormalizedMessage:

    message_id = stringify_id(
        first_value(
            message,
            "id",
            "message_id",
        )
    )

    timestamp_value = first_value(
        message,
        "timestamp",
        "created_at",
        "createdAt",
    )

    timestamp_utc, timestamp_unix = normalize_timestamp(
        timestamp_value
    )

    timestamp_original = timestamp_value
    if timestamp_original is not None and not isinstance(timestamp_original, str):
        timestamp_original = str(timestamp_original)

    timestamp_edited = message.get("timestampEdited")
    timestamp_edited_utc = None
    if timestamp_edited is not None:
        if not isinstance(timestamp_edited, str):
            timestamp_edited = str(timestamp_edited)
        timestamp_edited_utc, _ = normalize_timestamp(timestamp_edited)

    (
        author_id,
        author_name,
        author_display_name,
        author_nickname,
        author_is_bot,
        webhook_id,
    ) = extract_author(message)

    content = first_value(
        message,
        "content",
        "text",
    )

    if content is None:
        content = ""

    content = str(content)

    thread_name = first_value(
        message,
        "thread_name",
        "threadName",
    )

    return NormalizedMessage(
        message_id=message_id,
        message_type=(
            str(message["type"])
            if message.get("type") is not None
            else None
        ),
        timestamp_original=timestamp_original,
        timestamp_utc=timestamp_utc,
        timestamp_unix=timestamp_unix,
        timestamp_edited=timestamp_edited,
        timestamp_edited_utc=timestamp_edited_utc,

        category_name=(
            str(metadata["category_name"])
            if metadata["category_name"] is not None
            else None
        ),

        category_id=metadata["category_id"],

        channel_name=(
            str(metadata["channel_name"])
            if metadata["channel_name"] is not None
            else None
        ),

        channel_id=metadata["channel_id"],

        thread_name=(
            str(thread_name)
            if thread_name is not None
            else None
        ),

        author_discord_id=author_id,
        author_discord_name=author_name,
        author_display_name=author_display_name,
        author_nickname=author_nickname,
        author_is_bot=author_is_bot,

        webhook_id=webhook_id,

        content_original=content,
        content_search=clean_search_text(content),

        reply_to_message_id=extract_reply_id(message),

        is_pinned=normalize_boolean(message.get("isPinned")),
        call_ended_timestamp=(
            str(message["callEndedTimestamp"])
            if message.get("callEndedTimestamp") is not None
            else None
        ),

        attachments_json=extract_json_field(
            message,
            "attachments",
        ),

        embeds_json=extract_json_field(
            message,
            "embeds",
        ),

        stickers_json=extract_json_field(message, "stickers"),
        reactions_json=extract_json_field(message, "reactions"),
        mentions_json=extract_json_field(message, "mentions"),
        inline_emojis_json=extract_json_field(message, "inlineEmojis"),

        source_file=source_file,
        source_sha256=source_sha256,

        source_json=json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


# ---------------------------------------------------------------------------
# SQLite database
# ---------------------------------------------------------------------------

def create_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    run_migrations(connection)


def _upsert_server(
    connection: sqlite3.Connection,
    server_discord_id: str | None,
    name: str | None,
    import_id: int,
) -> int | None:
    if server_discord_id is None:
        return None
    row = connection.execute(
        "SELECT server_id FROM servers WHERE discord_server_id = ?", (server_discord_id,)
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            """INSERT INTO servers(discord_server_id, name_current, first_seen_import_id, last_seen_import_id)
               VALUES (?, ?, ?, ?)""",
            (server_discord_id, name, import_id, import_id),
        )
        return cursor.lastrowid
    connection.execute(
        """UPDATE servers SET name_current = COALESCE(?, name_current), last_seen_import_id = ?
           WHERE server_id = ?""",
        (name, import_id, row["server_id"]),
    )
    return row["server_id"]


def _upsert_category(
    connection: sqlite3.Connection,
    server_id: int | None,
    discord_category_id: str | None,
    name: str | None,
    import_id: int,
) -> int | None:
    if server_id is None or discord_category_id is None:
        return None
    row = connection.execute(
        """SELECT category_id FROM categories
           WHERE server_id = ? AND discord_category_id = ?""",
        (server_id, discord_category_id),
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            """INSERT INTO categories(server_id, discord_category_id, name_current,
                   first_seen_import_id, last_seen_import_id)
               VALUES (?, ?, ?, ?, ?)""",
            (server_id, discord_category_id, name, import_id, import_id),
        )
        return cursor.lastrowid
    connection.execute(
        """UPDATE categories SET name_current = COALESCE(?, name_current), last_seen_import_id = ?
           WHERE category_id = ?""",
        (name, import_id, row["category_id"]),
    )
    return row["category_id"]


def _upsert_channel(
    connection: sqlite3.Connection,
    server_id: int | None,
    discord_channel_id: str | None,
    category_id: int | None,
    name: str | None,
    import_id: int,
) -> int | None:
    if server_id is None or discord_channel_id is None:
        return None
    row = connection.execute(
        """SELECT channel_id FROM channels
           WHERE server_id = ? AND discord_channel_id = ?""",
        (server_id, discord_channel_id),
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            """INSERT INTO channels(server_id, discord_channel_id, category_id, name_current,
                   first_seen_import_id, last_seen_import_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (server_id, discord_channel_id, category_id, name, import_id, import_id),
        )
        return cursor.lastrowid
    connection.execute(
        """UPDATE channels SET category_id = ?, name_current = COALESCE(?, name_current),
                  last_seen_import_id = ? WHERE channel_id = ?""",
        (category_id, name, import_id, row["channel_id"]),
    )
    return row["channel_id"]


def _upsert_identity(connection: sqlite3.Connection, message: NormalizedMessage, import_id: int) -> int | None:
    if message.webhook_id is not None:
        identity_type = "webhook"
        lookup_column = "webhook_id"
        lookup_value = message.webhook_id
    elif message.author_discord_id is not None:
        identity_type = {0: "user", 1: "bot"}.get(message.author_is_bot, "unknown")
        lookup_column = "discord_user_id"
        lookup_value = message.author_discord_id
    else:
        return None

    row = connection.execute(
        f"SELECT identity_id FROM discord_identities WHERE {lookup_column} = ?",
        (lookup_value,),
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            """INSERT INTO discord_identities(identity_type, discord_user_id, webhook_id,
                   username_current, display_name_current, is_bot, first_seen_import_id, last_seen_import_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                identity_type,
                message.author_discord_id if lookup_column == "discord_user_id" else None,
                message.webhook_id if lookup_column == "webhook_id" else None,
                message.author_discord_name,
                message.author_display_name,
                message.author_is_bot if lookup_column == "discord_user_id" else None,
                import_id,
                import_id,
            ),
        )
        return cursor.lastrowid

    identity_id = row["identity_id"]
    connection.execute(
        """UPDATE discord_identities SET identity_type = ?,
                  username_current = COALESCE(?, username_current),
                  display_name_current = COALESCE(?, display_name_current),
                  is_bot = COALESCE(?, is_bot), last_seen_import_id = ?
           WHERE identity_id = ?""",
        (
            identity_type,
            message.author_discord_name,
            message.author_display_name,
            message.author_is_bot if lookup_column == "discord_user_id" else None,
            import_id,
            identity_id,
        ),
    )
    return identity_id


def _insert_observation(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    import_id: int,
    channel_id: int | None,
    message: NormalizedMessage,
) -> int:
    identity_id = _upsert_identity(connection, message, import_id)
    return record_message_observation(
        connection,
        source_id=source_id,
        import_id=import_id,
        observation={
            "discord_message_id": message.message_id,
            "channel_id": channel_id,
            "author_identity_id": identity_id,
            "author_discord_user_id_original": message.author_discord_id,
            "author_discord_name_original": message.author_discord_name,
            "author_display_name_original": message.author_display_name,
            "author_nickname_original": message.author_nickname,
            "author_is_bot_original": message.author_is_bot,
            "webhook_id_original": message.webhook_id,
            "message_type": message.message_type,
            "timestamp_original": message.timestamp_original,
            "timestamp_utc": message.timestamp_utc,
            "timestamp_unix": message.timestamp_unix,
            "timestamp_edited_original": message.timestamp_edited,
            "timestamp_edited_utc": message.timestamp_edited_utc,
            "content_original": message.content_original,
            "content_search": message.content_search,
            "reply_to_discord_message_id": message.reply_to_message_id,
            "is_pinned": message.is_pinned,
            "call_ended_timestamp_original": message.call_ended_timestamp,
            "attachments_json": message.attachments_json,
            "embeds_json": message.embeds_json,
            "stickers_json": message.stickers_json,
            "reactions_json": message.reactions_json,
            "mentions_json": message.mentions_json,
            "inline_emojis_json": message.inline_emojis_json,
            "source_json": message.source_json,
        },
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    declared_source_messages: int | None
    source_messages: int
    imported_messages: int
    missing_ids: int
    duplicate_ids_in_source: int
    invalid_timestamps: int
    missing_timestamps: int
    missing_content: int
    messages_with_edits: int
    messages_with_reactions: int
    messages_with_mentions: int
    messages_with_inline_emojis: int
    messages_with_stickers: int
    pinned_messages: int
    bot_authored_messages: int
    messages_with_nicknames: int
    messages_with_call_ended_timestamps: int
    invalid_json_fields: int

    oldest_timestamp: str | None
    newest_timestamp: str | None


def validate_messages(
    normalized_messages: list[NormalizedMessage],
    declared_source_messages: int | None = None,
) -> ValidationReport:

    source_messages = len(normalized_messages)

    ids: list[str] = []

    missing_ids = 0
    invalid_timestamps = 0
    missing_timestamps = 0
    missing_content = 0
    invalid_json_fields = 0

    timestamps: list[str] = []

    for message in normalized_messages:

        if message.message_id is None:
            missing_ids += 1
        else:
            ids.append(message.message_id)

        if message.timestamp_original is None:
            missing_timestamps += 1
        elif message.timestamp_utc is None:
            invalid_timestamps += 1
        else:
            timestamps.append(message.timestamp_utc)

        if not message.content_original:
            missing_content += 1

        for field_name in (
            "attachments_json",
            "embeds_json",
            "stickers_json",
            "reactions_json",
            "mentions_json",
            "inline_emojis_json",
        ):
            try:
                validate_json_value(getattr(message, field_name), field_name)
            except ValueError:
                invalid_json_fields += 1

    duplicate_count = len(ids) - len(set(ids))

    timestamps.sort()

    return ValidationReport(
        declared_source_messages=declared_source_messages,
        source_messages=source_messages,
        imported_messages=0,
        missing_ids=missing_ids,
        duplicate_ids_in_source=duplicate_count,
        invalid_timestamps=invalid_timestamps,
        missing_timestamps=missing_timestamps,
        missing_content=missing_content,
        messages_with_edits=sum(
            message.timestamp_edited is not None
            for message in normalized_messages
        ),
        messages_with_reactions=sum(
            message.reactions_json is not None
            and json.loads(message.reactions_json) != []
            for message in normalized_messages
        ),
        messages_with_mentions=sum(
            message.mentions_json is not None
            and json.loads(message.mentions_json) != []
            for message in normalized_messages
        ),
        messages_with_inline_emojis=sum(
            message.inline_emojis_json is not None
            and json.loads(message.inline_emojis_json) != []
            for message in normalized_messages
        ),
        messages_with_stickers=sum(
            message.stickers_json is not None
            and json.loads(message.stickers_json) != []
            for message in normalized_messages
        ),
        pinned_messages=sum(
            message.is_pinned == 1 for message in normalized_messages
        ),
        bot_authored_messages=sum(
            message.author_is_bot == 1 for message in normalized_messages
        ),
        messages_with_nicknames=sum(
            message.author_nickname is not None
            for message in normalized_messages
        ),
        messages_with_call_ended_timestamps=sum(
            message.call_ended_timestamp is not None
            for message in normalized_messages
        ),
        invalid_json_fields=invalid_json_fields,

        oldest_timestamp=timestamps[0]
        if timestamps
        else None,

        newest_timestamp=timestamps[-1]
        if timestamps
        else None,
    )


# ---------------------------------------------------------------------------
# Import one JSON file
# ---------------------------------------------------------------------------

def import_file(
    connection: sqlite3.Connection,
    path: Path,
    server_map_path: Path | None = None,
) -> dict[str, Any]:

    print(f"\nProcessing: {path}")

    source_hash = sha256_file(path)
    document = load_json(path)
    messages = get_message_list(document)

    if not isinstance(document, dict):
        document = {}

    metadata = get_metadata(
        document,
        messages,
    )

    source_path = (
        path.resolve().relative_to(ARCHIVE_ROOT).as_posix()
        if path.resolve().is_relative_to(ARCHIVE_ROOT)
        else path.resolve().as_posix()
    )
    scope = {
        field: metadata[field]
        for field in ("server_id", "category_id", "channel_id")
        if metadata[field] not in (None, "")
    }
    scope_json = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    identity_json = json.dumps(
        {"source_file": source_path, "scope": scope},
        sort_keys=True,
        separators=(",", ":"),
    )
    logical_source_key = "discord:" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
    registered_at = datetime.now(timezone.utc).isoformat()
    source_id = register_source_capture(
        connection,
        logical_source_key=logical_source_key,
        source_kind="discord_json",
        source_path=source_path,
        sha256=source_hash,
        byte_size=path.stat().st_size,
        scope_json=scope_json,
        registered_at=registered_at,
    )
    connection.commit()

    validate_server_map(metadata, server_map_path)

    declared_count = document.get("messageCount")
    if declared_count is not None and (
        not isinstance(declared_count, int)
        or isinstance(declared_count, bool)
        or declared_count < 0
    ):
        raise ValueError(
            f"{path}: messageCount must be a non-negative integer"
        )

    started_at = datetime.now(timezone.utc).isoformat()
    import_id = start_import_run(
        connection,
        source_id=source_id,
        started_at=started_at,
        importer_name="discord_json_importer",
        importer_version="2",
    )
    connection.commit()
    connection.execute("SAVEPOINT discord_import_materialization")

    try:
        normalized_messages: list[NormalizedMessage] = []
        normalization_warnings: list[str] = []
        for index, raw_message in enumerate(messages):
            try:
                normalized_messages.append(
                    normalize_message(raw_message, metadata, source_path, source_hash)
                )
            except (TypeError, ValueError) as exc:
                normalization_warnings.append(f"message[{index}]: {exc}")

        validation = validate_messages(normalized_messages, declared_count)
        validation.source_messages = len(messages)

        server_id = _upsert_server(
            connection, metadata["server_id"], metadata["server_name"], import_id
        )
        category_id = _upsert_category(
            connection,
            server_id,
            metadata["category_id"],
            metadata["category_name"],
            import_id,
        )
        channel_id = _upsert_channel(
            connection,
            server_id,
            metadata["channel_id"],
            category_id,
            metadata["channel_name"],
            import_id,
        )

        seen_message_ids: set[str] = set()
        observations_written = 0
        for message in normalized_messages:
            if message.message_id is None or message.message_id in seen_message_ids:
                continue
            seen_message_ids.add(message.message_id)
            _insert_observation(
                connection,
                source_id=source_id,
                import_id=import_id,
                channel_id=channel_id,
                message=message,
            )
            observations_written += 1

        validation.imported_messages = observations_written
        warnings = list(normalization_warnings)
        if declared_count is not None and declared_count != len(messages):
            warnings.append(
                f"Declared message count {declared_count} differs from observed count {len(messages)}"
            )
        if validation.missing_ids:
            warnings.append(f"{validation.missing_ids} messages have no Discord ID")
        if validation.duplicate_ids_in_source:
            warnings.append(f"{validation.duplicate_ids_in_source} duplicate IDs occur within this capture")
        if validation.invalid_timestamps:
            warnings.append(f"{validation.invalid_timestamps} timestamps are invalid")
        if validation.invalid_json_fields:
            warnings.append(f"{validation.invalid_json_fields} JSON fields are invalid")

        status = "partial" if warnings else "succeeded"
        finished_at = datetime.now(timezone.utc).isoformat()
        finish_import_run(
            connection,
            import_id=import_id,
            status=status,
            finished_at=finished_at,
            records_seen=len(messages),
            records_written=observations_written,
            warnings_json=json.dumps(warnings, ensure_ascii=False),
            error_summary="; ".join(warnings) if warnings else None,
        )
        if status == "succeeded":
            accept_import_run(connection, import_id=import_id, accepted_at=finished_at)

        connection.execute("RELEASE SAVEPOINT discord_import_materialization")
        connection.commit()
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT discord_import_materialization")
        connection.execute("RELEASE SAVEPOINT discord_import_materialization")
        finish_import_run(
            connection,
            import_id=import_id,
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            records_seen=len(messages),
            records_written=0,
            warnings_json=json.dumps([str(exc)], ensure_ascii=False),
            error_summary=str(exc),
        )
        connection.commit()
        raise

    print_report(
        path,
        metadata,
        validation,
        warnings,
    )

    return {
        "file": source_path,
        "sha256": source_hash,
        "source_id": source_id,
        "import_id": import_id,
        "status": status,
        "server": metadata["server_name"],
        "category": metadata["category_name"],
        "channel": metadata["channel_name"],
        "channel_id": metadata["channel_id"],
        "warnings": warnings,
        "validation": validation.__dict__,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(
    path: Path,
    metadata: dict[str, Any],
    validation: ValidationReport,
    warnings: list[str],
) -> None:

    print("  Imported:")
    print(f"    Server:   {metadata['server_name'] or '[unknown]'}")
    print(f"    Category: {metadata['category_name'] or '[unknown]'}")
    print(f"    Channel:  {metadata['channel_name'] or '[unknown]'}")
    print(f"    Observations: {validation.imported_messages}")

    print("  Date range:")
    print(
        f"    Oldest:   "
        f"{validation.oldest_timestamp or '[unknown]'}"
    )

    print(
        f"    Newest:   "
        f"{validation.newest_timestamp or '[unknown]'}"
    )

    print("  Validation:")
    for warning in warnings:
        print(f"    Warning: {warning}")

    print(
        f"    Declared messages:      "
        f"{validation.declared_source_messages}"
    )

    print(
        f"    Source messages:       "
        f"{validation.source_messages}"
    )

    print(
        f"    Missing message IDs:   "
        f"{validation.missing_ids}"
    )

    print(
        f"    Duplicate IDs:         "
        f"{validation.duplicate_ids_in_source}"
    )

    print(
        f"    Invalid timestamps:    "
        f"{validation.invalid_timestamps}"
    )

    print(
        f"    Missing timestamps:     "
        f"{validation.missing_timestamps}"
    )

    print(
        f"    Invalid JSON fields:    "
        f"{validation.invalid_json_fields}"
    )

    print("  Metadata signals:")
    print(f"    Edited:                 {validation.messages_with_edits}")
    print(f"    Reactions:              {validation.messages_with_reactions}")
    print(f"    Mentions:               {validation.messages_with_mentions}")
    print(f"    Inline emojis:          {validation.messages_with_inline_emojis}")
    print(f"    Stickers:               {validation.messages_with_stickers}")
    print(f"    Pinned:                 {validation.pinned_messages}")
    print(f"    Bot-authored:           {validation.bot_authored_messages}")
    print(f"    Nicknames:              {validation.messages_with_nicknames}")
    print(
        f"    Call-ended timestamps:  "
        f"{validation.messages_with_call_ended_timestamps}"
    )

    print(
        f"    Empty content:         "
        f"{validation.missing_content}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def find_json_exports(target: Path) -> list[Path]:
    """Find JSON exports recursively."""

    if target.is_file():
        if target.suffix.lower() != ".json":
            raise ValueError(
                f"Expected a JSON file: {target}"
            )

        return [target]

    if not target.exists():
        raise FileNotFoundError(
            f"Path does not exist: {target}"
        )

    return sorted(
        target.rglob("*.json")
    )


def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Import DiscordChatExporter JSON files into "
            "the Discord RP archive SQLite database."
        )
    )

    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=RAW_ROOT,
        help=(
            "JSON file or directory to import. "
            "Defaults to 01_Raw/."
        ),
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=DATABASE_PATH,
        help="SQLite database output path.",
    )

    parser.add_argument(
        "--server-map",
        type=Path,
        default=None,
        help="Optional YAML server map used for ID expectation checks.",
    )

    args = parser.parse_args()

    ensure_directories()

    target = args.path

    if not target.is_absolute():
        target = ARCHIVE_ROOT / target

    database_path = args.database

    if not database_path.is_absolute():
        database_path = ARCHIVE_ROOT / database_path

    server_map_path = args.server_map
    if server_map_path is not None and not server_map_path.is_absolute():
        server_map_path = ARCHIVE_ROOT / server_map_path

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    json_files = find_json_exports(target)

    if not json_files:
        print(
            f"No JSON exports found under: {target}"
        )
        return 1

    print(
        f"Found {len(json_files)} JSON export(s)."
    )

    connection = sqlite3.connect(
        database_path
    )

    connection.row_factory = sqlite3.Row

    try:

        create_database(connection)

        results = []

        for json_file in json_files:
            try:
                result = import_file(
                    connection,
                    json_file,
                    server_map_path,
                )

                results.append(result)

            except Exception as exc:
                print(
                    f"\nERROR processing {json_file}:"
                )
                print(f"  {exc}")

        manifest = {
            "generated_at_utc":
                datetime.now(timezone.utc).isoformat(),

            "database":
                str(database_path.relative_to(ARCHIVE_ROOT))
                if database_path.is_relative_to(ARCHIVE_ROOT)
                else str(database_path),

            "files_processed": len(results),

            "results": results,
        }

        with MANIFEST_PATH.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                manifest,
                file,
                indent=2,
                ensure_ascii=False,
            )

        total_messages = connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

        total_sources = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE source_kind = 'discord_json'"
        ).fetchone()[0]
        total_imports = connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0]

        print("\n" + "=" * 60)
        print("ARCHIVE IMPORT COMPLETE")
        print("=" * 60)
        print(f"Source captures:       {total_sources}")
        print(f"Import runs:           {total_imports}")
        print(f"Observations:          {total_messages}")
        print(f"Database:              {database_path}")
        print(f"Manifest:              {MANIFEST_PATH}")

    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())