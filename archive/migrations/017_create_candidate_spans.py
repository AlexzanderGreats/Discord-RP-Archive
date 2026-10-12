from __future__ import annotations

import sqlite3

from archive.migrations.common import migration


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "017_create_candidate_spans.sql")
