from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


MIGRATIONS_PATH = Path(__file__).parent
HISTORICAL_CHECKSUMS = {
    1: frozenset({"25439f9cbd3bb2e571d275b8a7121a2e47e998d1b4abb1a61e93723eaf3b3aa5"}),
    3: frozenset({"428335c8c0ea0453885a4aaf15bc8e346bb55413b6daa7058785ae0d23b21902"}),
    4: frozenset({"b4a7e0e43226246f222e9f4b4961f5b8cca0a7096629f24101fd33be0ff5fda1"}),
    5: frozenset({"38df8a147bef21404ca954bc1d8531457434bd56005970c3c0eb1c9d75099cc8"}),
    6: frozenset({"eb6224738e7624bf7093fb4647ce53f832a0631f6fdf433415b7095260356d0f"}),
    7: frozenset({"bf53b779fd55a97e03980999213b567b129a9a76853b200e562b88d58d20d3ac"}),
    8: frozenset({"02e167915678775bd436dcf1859de2986fe37cefc2aa2a71e3a64f25a5bd0165"}),
    9: frozenset({"379aa40e53ac9723a5f68f339f54c58bbfaa390148a4269fde69e0c0914b8e23"}),
    11: frozenset({"01cfdf5d0b5629a6df276452f7e4baffffac35fb7334fe6df425a556452da4f3"}),
}


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
            accepted_checksums = {checksum, *HISTORICAL_CHECKSUMS.get(version, ())}
            if row["name"] != name or row["checksum"] not in accepted_checksums:
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