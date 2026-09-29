from __future__ import annotations

import sqlite3

from archive.migrations.common import migration


def upgrade(connection: sqlite3.Connection) -> None:
    migration(connection, "011_remove_threads.sql")
