#!/usr/bin/env python3
"""Document archive builder entrypoint.

This module wraps the parser in src/document_parser.py and exposes a small CLI for
scanning the raw TXT/PDF/DOCX corpus under 01_Raw and importing it into SQLite,
without modifying the core Discord importer in build_archive.py.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.document_parser import (
    DEFAULT_DATABASE_PATH,
    SUPPORTED_EXTENSIONS,
    create_document_tables,
    discover_files,
    import_document,
    metadata_file_to_map,
    parse_supported_file,
    sha256_file,
)

ARCHIVE_ROOT = REPO_ROOT
RAW_ROOT = ARCHIVE_ROOT / "01_Raw"

PROCESSED_ROOT = ARCHIVE_ROOT / "02_Processed"


def describe_scan(root: Path, recursive: bool = True) -> dict[str, Any]:
    discovered = discover_files(root, recursive=recursive)
    summary = {
        "root": str(root),
        "total": len(discovered),
        "by_format": {"txt": 0, "pdf": 0, "docx": 0},
    }
    for path in discovered:
        suffix = path.suffix.lower()
        if suffix == ".txt":
            summary["by_format"]["txt"] += 1
        elif suffix == ".pdf":
            summary["by_format"]["pdf"] += 1
        elif suffix == ".docx":
            summary["by_format"]["docx"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import TXT/PDF/DOCX files into the archive database.")
    parser.add_argument("input_directory", type=Path, nargs="?", default=RAW_ROOT, help="Directory containing raw documents.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE_PATH, help="SQLite database path.")
    parser.add_argument("--metadata", type=Path, help="Optional YAML metadata override file.")
    parser.add_argument("--recursive", action="store_true", help="Recursively scan the input directory.")
    parser.add_argument("--dry-run", action="store_true", help="Parse files without writing them to the database.")
    parser.add_argument("--reimport", action="store_true", help="Deprecated compatibility option; every import attempt is recorded.")
    parser.add_argument("--verbose", action="store_true", help="Print extra progress output.")
    args = parser.parse_args(argv)

    input_root = args.input_directory.resolve()
    db_path = args.db.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    create_document_tables(connection)

    metadata_overrides: dict[str, Any] = {}
    if args.metadata and args.metadata.exists():
        metadata_overrides = metadata_file_to_map(args.metadata, input_root)

    discovered = discover_files(input_root, recursive=args.recursive or True)
    scan_summary = describe_scan(input_root, recursive=args.recursive or True)

    print("Document Archive Builder")
    print("========================")
    print(f"Input: {input_root}")
    print(f"Database: {db_path}")
    print(f"Discovered: {scan_summary['total']} files")
    print(f"  TXT: {scan_summary['by_format']['txt']}")
    print(f"  PDF: {scan_summary['by_format']['pdf']}")
    print(f"  DOCX: {scan_summary['by_format']['docx']}")

    report: list[dict[str, Any]] = []

    for file_path in discovered:
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            if args.verbose:
                print(f"[SKIP] {file_path.name} ({file_path.suffix})")
            continue

        try:
            if args.dry_run:
                parsed = parse_supported_file(file_path)
                report.append({
                    "source_path": str(file_path),
                    "source_format": parsed.source_format,
                    "title": parsed.title,
                    "status": parsed.extraction_status,
                    "document_type": parsed.document_type,
                    "duplicate": False,
                    "document_id": None,
                })
                if args.verbose:
                    print(f"[DRY-RUN] {file_path.name} -> {parsed.extraction_status}")
                continue

            result = import_document(
                connection,
                file_path,
                input_root=input_root,
                metadata_overrides=metadata_overrides,
                reimport=args.reimport,
            )
            report.append({
                "source_path": result.source_path,
                "source_format": result.source_format,
                "title": result.title,
                "status": result.extraction_status,
                "document_type": result.document_type,
                "duplicate": result.duplicate,
                "document_id": result.document_id,
            })
            if args.verbose:
                print(f"[IMPORT] {file_path.name} -> document_id={result.document_id} duplicate={result.duplicate}")
        except Exception as exc:  # pragma: no cover - CLI safety
            report.append({
                "source_path": str(file_path),
                "source_format": SUPPORTED_EXTENSIONS.get(file_path.suffix.lower(), "unsupported"),
                "title": file_path.stem,
                "status": "error",
                "document_type": "unknown",
                "duplicate": False,
                "document_id": None,
                "error": str(exc),
            })
            if args.verbose:
                print(f"[ERROR] {file_path.name}: {exc}")

    imported = sum(1 for row in report if row.get("document_id") is not None and not row.get("duplicate"))
    duplicates = sum(1 for row in report if row.get("duplicate"))
    failures = sum(1 for row in report if row.get("status") == "error")

    print(f"Imported: {imported}")
    print(f"Duplicates: {duplicates}")
    print(f"Failures: {failures}")
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
