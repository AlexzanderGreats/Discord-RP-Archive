from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from archive.migrations.runner import run_migrations


DEFAULT_DATABASE = Path(__file__).resolve().parent.parent / "02_Processed" / "archive.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m archive")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_parser = commands.add_parser("migrate", help="Migrate the archive database schema")
    migrate_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()

    database_path = args.database.resolve()
    if not database_path.exists():
        parser.error(f"Database does not exist: {database_path}")

    print("Opening database...")
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        run_migrations(connection)
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())