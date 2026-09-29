from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from archive.migrations.runner import run_migrations
from src.build_archive import import_file as import_discord_export
from src.document_parser import SUPPORTED_EXTENSIONS, import_document


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_SOURCE_EXTENSIONS = frozenset({".json", *SUPPORTED_EXTENSIONS})


@dataclass
class SyncItem:
    path: Path
    status: str
    detail: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class StartupSyncReport:
    raw_directory: Path
    scanned: int = 0
    items: list[SyncItem] = field(default_factory=list)
    database_health: str = "not checked"
    reindexed: bool = False

    @property
    def imported(self) -> int:
        return sum(item.status == "imported" for item in self.items)

    @property
    def unchanged(self) -> int:
        return sum(item.status == "unchanged" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)


ProgressCallback = Callable[[int, int, Path, str], None]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_path(path: str | Path, *, project_root: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return str(candidate.resolve()).casefold()


def _import_history(connection: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, list[str]]]:
    history: dict[tuple[str, str], tuple[str, list[str]]] = {}
    rows = connection.execute(
        """SELECT s.source_path, s.sha256, i.status, i.warnings_json, i.error_summary
           FROM sources s JOIN imports i ON i.source_id = s.source_id
           ORDER BY i.import_id DESC"""
    )
    for row in rows:
        key = (_normalized_path(row["source_path"], project_root=PROJECT_ROOT), row["sha256"])
        if key in history:
            continue
        try:
            warnings = json.loads(row["warnings_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            warnings = []
        if not isinstance(warnings, list):
            warnings = []
        if row["error_summary"]:
            warnings.append(row["error_summary"])
        history[key] = (row["status"], [str(warning) for warning in warnings])
    return history


def synchronize_archive(
    connection: sqlite3.Connection,
    raw_directory: Path,
    *,
    progress: ProgressCallback | None = None,
) -> StartupSyncReport:
    raw_directory = raw_directory.resolve()
    if not raw_directory.is_dir():
        raise FileNotFoundError(f"Raw archive directory does not exist: {raw_directory}")

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    run_migrations(connection)

    paths = sorted(
        path
        for path in raw_directory.rglob("*")
        if path.is_file() and path.suffix.casefold() in SUPPORTED_SOURCE_EXTENSIONS
    )
    report = StartupSyncReport(raw_directory=raw_directory, scanned=len(paths))
    history = _import_history(connection)
    imported_any = False

    for index, path in enumerate(paths, start=1):
        if progress is not None:
            progress(index - 1, len(paths), path, "Scanning")
        try:
            source_hash = _sha256(path)
            if progress is not None:
                progress(index - 1, len(paths), path, "Comparing")
            key = (_normalized_path(path, project_root=PROJECT_ROOT), source_hash)
            previous = history.get(key)
            if previous is not None:
                status, warnings = previous
                detail = f"Previously {status}; unchanged capture."
                report.items.append(SyncItem(path, "unchanged", detail, warnings))
                continue

            if progress is not None:
                progress(index - 1, len(paths), path, "Importing")
            if path.suffix.casefold() == ".json":
                result = import_discord_export(connection, path)
                status = result["status"]
                warnings = list(result.get("warnings", []))
            else:
                result = import_document(connection, path, input_root=raw_directory)
                status = result.import_status or "failed"
                warnings = list(result.warnings)

            if status in {"succeeded", "partial"}:
                imported_any = True
                report.items.append(SyncItem(path, "imported", status, warnings))
            else:
                report.items.append(SyncItem(path, "failed", status, warnings))
        except Exception as exc:
            report.items.append(SyncItem(path, "failed", str(exc), [str(exc)]))
        finally:
            if progress is not None:
                outcome = report.items[-1].status.capitalize()
                progress(index, len(paths), path, outcome)

    if progress is not None:
        progress(len(paths), len(paths), raw_directory, "Validating")
    integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    report.database_health = "ok" if integrity == "ok" and not foreign_key_errors else "failed"
    if report.database_health != "ok":
        raise sqlite3.DatabaseError(
            f"Archive validation failed: quick_check={integrity}; "
            f"foreign_key_errors={len(foreign_key_errors)}"
        )
    if progress is not None:
        progress(len(paths), len(paths), raw_directory, "Validated")

    if imported_any:
        if progress is not None:
            progress(len(paths), len(paths), raw_directory, "Reindexing")
        connection.execute("REINDEX")
        connection.execute("PRAGMA optimize")
        connection.commit()
        report.reindexed = True
    return report