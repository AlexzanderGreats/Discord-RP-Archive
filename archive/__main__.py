from __future__ import annotations

import argparse
import json
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
    gui_parser = commands.add_parser("gui", help="Open the archive desktop interface")
    gui_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    gui_parser.add_argument("--raw-directory", type=Path, default=Path(__file__).resolve().parent.parent / "01_Raw")
    pipeline_parser = commands.add_parser("pipeline", help="Run or inspect B–F1 inference processing")
    pipeline_commands = pipeline_parser.add_subparsers(dest="pipeline_command", required=True)
    pipeline_run = pipeline_commands.add_parser("run", help="Process the selected accepted-source scope")
    pipeline_dry_run = pipeline_commands.add_parser("dry-run", help="Plan processing without inference writes")
    for command_parser in (pipeline_run, pipeline_dry_run):
        command_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
        command_parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
        command_parser.add_argument("--server-id", type=int)
        command_parser.add_argument("--channel-id", type=int)
        command_parser.add_argument("--source-id", type=int)
        command_parser.add_argument("--import-id", type=int)
        command_parser.add_argument("--message-id", type=int, action="append", dest="message_ids")
        command_parser.add_argument("--format", choices=("text", "json"), default="text")
    pipeline_status = pipeline_commands.add_parser("status", help="Compare current inputs with the latest run")
    pipeline_report = pipeline_commands.add_parser("report", help="Show a persisted pipeline run report")
    for command_parser in (pipeline_status, pipeline_report):
        command_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
        command_parser.add_argument("--format", choices=("text", "json"), default="text")
    pipeline_status.add_argument("--server-id", type=int)
    pipeline_status.add_argument("--channel-id", type=int)
    pipeline_status.add_argument("--source-id", type=int)
    pipeline_status.add_argument("--import-id", type=int)
    pipeline_report.add_argument("--run-id")
    args = parser.parse_args()

    database_path = args.database.resolve()
    if not database_path.exists():
        parser.error(f"Database does not exist: {database_path}")

    if args.command == "gui":
        from archive.gui import run

        return run(database_path, args.raw_directory.resolve())

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        if args.command == "migrate":
            print("Opening database...")
            run_migrations(connection)
            return 0

        if args.command == "pipeline":
            from archive.pipeline import (
                format_pipeline_report,
                get_pipeline_report,
                get_pipeline_status,
                run_pipeline,
            )

            if args.pipeline_command in {"run", "dry-run"}:
                report = run_pipeline(
                    connection,
                    server_id=args.server_id,
                    channel_id=args.channel_id,
                    source_id=args.source_id,
                    import_id=args.import_id,
                    message_observation_ids=args.message_ids,
                    mode=args.mode,
                    dry_run=args.pipeline_command == "dry-run",
                )
                if args.format == "json":
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                else:
                    print(format_pipeline_report(report))
                return 0 if report["status"] == "completed" else 1

            if args.pipeline_command == "status":
                status = get_pipeline_status(
                    connection,
                    server_id=args.server_id,
                    channel_id=args.channel_id,
                    source_id=args.source_id,
                    import_id=args.import_id,
                )
                if args.format == "json":
                    print(json.dumps(status, ensure_ascii=False, indent=2))
                else:
                    print(
                        f"Current for requested scope: {status['current_for_scope']}\n"
                        f"Messages examined now: {status['messages_examined']}\n"
                        f"Latest run: {json.dumps(status['latest_run'], ensure_ascii=False, default=str)}"
                    )
                return 0

            report = get_pipeline_report(connection, args.run_id)
            if report is None:
                print("No matching pipeline run was found.", file=sys.stderr)
                return 1
            if args.format == "json":
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(format_pipeline_report(report))
            return 0 if report["status"] == "completed" else 1
    except Exception as exc:
        print(f"Command failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())