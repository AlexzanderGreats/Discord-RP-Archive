from __future__ import annotations

import sqlite3
from pathlib import Path


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def columns(connection: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}


def execute_script(connection: sqlite3.Connection, path: Path) -> None:
    statement = ""
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError(f"Incomplete SQL statement in {path.name}")


def migration(connection: sqlite3.Connection, filename: str) -> None:
    execute_script(connection, Path(__file__).with_name(filename))