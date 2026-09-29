from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


MIGRATIONS_PATH = Path(__file__).parent


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"archive_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load migration: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_migrations(connection: sqlite3.Connection) -> int:
    connection.row_factory = sqlite3.Row
    paths = sorted(MIGRATIONS_PATH.glob("[0-9][0-9][0-9]_*.py"))
    if not paths:
        raise RuntimeError("No migration files were found")

    migrations = []
    for path in paths:
        checksum = hashlib.sha256(path.read_bytes())
        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            checksum.update(sql_path.read_bytes())
        migrations.append((path, _load_migration(path), checksum.hexdigest()))

    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_schema_migrations_name ON schema_migrations(name)")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_schema_migrations_checksum ON schema_migrations(checksum)")
    applied = {
        row["version"]: row
        for row in connection.execute("SELECT version, name, checksum FROM schema_migrations")
    }
    current = max(applied, default=0)
    print(f"Current schema: {'legacy' if current == 0 else current}")
    print(f"Target schema: {len(migrations)}")

    known_versions = {int(path.name[:3]) for path, _, _ in migrations}
    if any(version not in known_versions for version in applied):
        raise RuntimeError("Migration ledger contains a version unknown to this application")
    if set(applied) != set(range(1, current + 1)):
        raise RuntimeError("Migration ledger has a version gap; refusing to apply out of order")

    for path, _, checksum in migrations:
        version = int(path.name[:3])
        name = path.stem[4:]
        if version in applied:
            row = applied[version]
            if row["name"] != name or row["checksum"] != checksum:
                raise RuntimeError(f"Applied migration {version:03d} does not match its recorded checksum")

    pending = [
        (path, module, checksum)
        for path, module, checksum in migrations
        if int(path.name[:3]) not in applied
    ]
    if pending:
        print("\nApplying:")
    else:
        print("\nNo migrations to apply.")

    for path, module, checksum in pending:
        version = int(path.name[:3])
        name = path.stem[4:]
        print(f"  {version:03d}_{name}")
        try:
            connection.execute("BEGIN IMMEDIATE")
            module.upgrade(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (version, name, checksum, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    final_version = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    print(f"\nMigration complete.\nSchema version: {final_version}")
    return final_version