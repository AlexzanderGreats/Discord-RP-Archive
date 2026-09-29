from __future__ import annotations

import hashlib
import json
import sqlite3

from archive.migrations.common import table_exists


def upgrade(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "legacy_imports"):
        return

    rows = connection.execute(
        """SELECT s.source_id, s.sha256, s.source_path, li.server_id,
                  li.category_id, li.channel_id
           FROM sources s JOIN legacy_imports li ON li.source_sha256 = s.sha256
           WHERE s.source_kind = 'discord_json' AND s.logical_source_key = 'discord:' || s.sha256
           ORDER BY s.source_id"""
    ).fetchall()
    for row in rows:
        scope = {
            field: row[field]
            for field in ("server_id", "category_id", "channel_id")
            if row[field] not in (None, "")
        }
        scope_json = json.dumps(scope, sort_keys=True, separators=(",", ":"))
        identity_json = json.dumps(
            {"source_file": row["source_path"], "scope": scope},
            sort_keys=True,
            separators=(",", ":"),
        )
        logical_source_key = "discord:" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE sources SET logical_source_key = ?, scope_json = ? WHERE source_id = ?",
            (logical_source_key, scope_json, row["source_id"]),
        )