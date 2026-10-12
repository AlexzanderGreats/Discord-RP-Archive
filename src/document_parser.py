#!/usr/bin/env python3
"""Parse source documents into typed archive records with shared provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - optional dependency
    from importlib import import_module

    yaml = import_module("yaml")
except ImportError:  # pragma: no cover - optional dependency
    yaml = None

try:  # pragma: no cover - optional dependency
    import fitz  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    fitz = None

try:  # pragma: no cover - optional dependency
    from importlib import import_module

    DocxDocument = import_module("docx").Document
except ImportError:  # pragma: no cover - optional dependency
    DocxDocument = None


ARCHIVE_ROOT = Path(__file__).resolve().parent.parent
if str(ARCHIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(ARCHIVE_ROOT))

from archive.migrations.runner import run_migrations
from archive.provenance import accept_import_run, finish_import_run, register_source_capture, start_import_run


def _resolve_existing_path(*candidates: str) -> Path:
    for candidate in candidates:
        path = ARCHIVE_ROOT / candidate
        if path.exists():
            return path
    return ARCHIVE_ROOT / candidates[0]


RAW_ROOT = _resolve_existing_path("01_Raw")
PROCESSED_ROOT = _resolve_existing_path("02_Processed")
DEFAULT_DATABASE_PATH = PROCESSED_ROOT / "archive.sqlite3"

SUPPORTED_EXTENSIONS = {
    ".txt": "txt",
    ".pdf": "pdf",
    ".docx": "docx",
}

VALID_DOCUMENT_TYPES = {
    "unknown",
    "character_profile",
    "character_sheet",
    "lore",
    "worldbuilding",
    "story",
    "chapter",
    "rp_transcript",
    "rp_scene",
    "powerscaling",
    "analysis",
    "reference",
    "notes",
    "other",
}

VALID_STATUSES = {"historical", "current", "reference", "draft", "unknown"}


@dataclass
class ExtractionResult:
    source_path: Path
    source_format: str
    source_sha256: str
    file_size_bytes: int
    title: str | None
    document_type: str
    status: str
    created_at_source: str | None = None
    modified_at_source: str | None = None
    page_count: int | None = None
    paragraph_count: int = 0
    character_count: int = 0
    word_count: int = 0
    extraction_status: str = "success"
    extraction_error: str | None = None
    content_text: str = ""
    content_search: str = ""
    metadata_json: str = "{}"
    pages_json: str | None = None
    blocks_json: str | None = None
    warnings: list[str] = field(default_factory=list)
    source_location_json: str | None = None
    chunks: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DocumentImportResult:
    document_id: int | None
    source_path: str
    source_sha256: str
    source_format: str
    document_type: str
    status: str
    title: str | None
    duplicate: bool = False
    warnings: list[str] = field(default_factory=list)
    extraction_status: str = "success"
    extraction_error: str | None = None
    source_id: int | None = None
    import_id: int | None = None
    import_status: str | None = None


# ---------------------------------------------------------------------------
# SQLite database setup
# ---------------------------------------------------------------------------

def create_document_tables(connection: sqlite3.Connection) -> None:
    """Apply the shared source/import and typed-content schema migrations."""
    connection.execute("PRAGMA foreign_keys = ON")
    run_migrations(connection)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_search_text(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def normalize_document_type(value: str | None) -> str:
    if value is None:
        return "unknown"
    normalized = str(value).strip().casefold()
    if not normalized:
        return "unknown"
    if normalized in VALID_DOCUMENT_TYPES:
        return normalized
    normalized = normalized.replace(" ", "_")
    if normalized in VALID_DOCUMENT_TYPES:
        return normalized
    return "unknown"


def normalize_status(value: str | None) -> str:
    if value is None:
        return "unknown"
    normalized = str(value).strip().casefold()
    if normalized in VALID_STATUSES:
        return normalized
    return "unknown"


def parse_iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return None


def detect_title_from_path(path: Path, explicit_title: str | None = None) -> str | None:
    if explicit_title:
        return explicit_title.strip() or None
    stem = path.stem.strip()
    if not stem:
        return None
    if stem.lower() in {"desktop", "thumbs", "image"}:
        return None
    return stem


def detect_title(metadata: dict[str, Any], path: Path, content_text: str, explicit_title: str | None = None) -> str | None:
    if explicit_title:
        value = explicit_title.strip()
        if value:
            return value
    if metadata.get("title"):
        value = str(metadata["title"]).strip()
        if value:
            return value

    for line in content_text.splitlines():
        candidate = line.strip().strip("# ")
        if not candidate:
            continue
        if candidate.endswith((".", "!", "?", ":", ";")):
            continue
        if len(candidate) <= 2:
            continue
        word_count = len(re.findall(r"\S+", candidate))
        if word_count == 1:
            continue
        if word_count <= 8 and candidate[:1].isupper():
            return candidate

    return detect_title_from_path(path)


def classify_document(explicit_value: str | None, title: str | None, content_text: str) -> str:
    if explicit_value:
        return normalize_document_type(explicit_value)

    candidate_text = "\n".join(filter(None, [title or "", content_text[:2000]]))
    lower = candidate_text.casefold()

    if re.search(r"\bcharacter profile\b|\bcharacter sheet\b|\bprofile of\b", lower):
        return "character_profile"
    if re.search(r"\bcharacter sheet\b|\bability\b|\borigin\b|\bstats\b|\bpower\b", lower):
        return "character_sheet"
    if re.search(r"\blore\b|\bmyth\b|\bcanon\b|\bhistory\b|\bcosmology\b", lower):
        return "lore"
    if re.search(r"\bworldbuilding\b|\bsetting\b|\brealm\b|\bworld\b", lower):
        return "worldbuilding"
    if re.search(r"\bchapter\b|\bstory\b|\bscene\b|\bprologue\b", lower):
        return "story"
    if re.search(r"\brp transcript\b|\broleplay\b|\brole play\b|\bcurrent rp\b|\bhistorical rp\b", lower):
        return "rp_transcript"
    if re.search(r"\bpowerscaling\b|\bscaling\b|\bcombat analysis\b", lower):
        return "powerscaling"
    if re.search(r"\banalysis\b|\bcomparison\b|\bevaluation\b", lower):
        return "analysis"
    if re.search(r"\breference\b|\bnotes\b", lower):
        return "reference"
    return "unknown"


def metadata_file_to_map(path: Path, input_root: Path | None = None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError(f"YAML metadata support requires PyYAML. Install it to use {path.name}.")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    documents = data.get("documents", []) if isinstance(data, dict) else []
    mapping: dict[str, dict[str, Any]] = {}
    for item in documents:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not raw_path:
            continue
        candidate = Path(str(raw_path))
        if input_root is not None:
            relative = candidate.as_posix()
            normalized = relative.replace("\\", "/")
            mapping[normalized] = item
            if candidate.is_absolute():
                try:
                    mapping[str(candidate.relative_to(input_root)).replace("\\", "/")] = item
                except ValueError:
                    pass
        else:
            mapping[candidate.as_posix()] = item
    return mapping


def apply_metadata_overrides(
    source_path: Path,
    input_root: Path | None,
    metadata_overrides: dict[str, Any] | None,
    parsed: ExtractionResult,
) -> ExtractionResult:
    if not metadata_overrides:
        return parsed

    def apply_override_values(override: dict[str, Any]) -> None:
        if not isinstance(override, dict):
            return
        if "title" in override and override["title"]:
            parsed.title = str(override["title"]).strip() or parsed.title
        if "document_type" in override and override["document_type"]:
            parsed.document_type = normalize_document_type(str(override["document_type"]))
        if "status" in override and override["status"]:
            parsed.status = normalize_status(str(override["status"]))
        if "source_format" in override and override["source_format"]:
            parsed.source_format = str(override["source_format"]).strip().lower()

    top_level_keys = {"title", "document_type", "status", "source_format"}
    if top_level_keys.intersection(metadata_overrides.keys()):
        apply_override_values(metadata_overrides)

    if input_root is not None:
        relative = source_path.relative_to(input_root) if source_path.is_relative_to(input_root) else source_path
    else:
        relative = source_path

    candidate_keys = [
        relative.as_posix(),
        str(relative).replace("\\", "/"),
        source_path.name,
        source_path.as_posix(),
        str(source_path).replace("\\", "/"),
    ]

    for key in candidate_keys:
        item = metadata_overrides.get(key)
        if item is not None:
            apply_override_values(item)
            break

    return parsed


# ---------------------------------------------------------------------------
# Format-specific parsers
# ---------------------------------------------------------------------------


def parse_txt(path: Path) -> ExtractionResult:
    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    try:
        text = source_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Controlled fallback for UTF-16 if present. This avoids silently mangling text.
        try:
            text = source_bytes.decode("utf-16")
        except UnicodeDecodeError as exc:  # pragma: no cover - exact error path
            raise ValueError(f"Unable to decode {path.name} as UTF-8/UTF-16: {exc}") from exc

    content = text
    word_count = len(re.findall(r"\S+", content))
    paragraph_count = max(1, len(re.split(r"\n\s*\n", content)) if content.strip() else 0)
    character_count = len(content)

    result = ExtractionResult(
        source_path=path,
        source_format="txt",
        source_sha256=source_sha256,
        file_size_bytes=len(source_bytes),
        title=detect_title({}, path, content),
        document_type="unknown",
        status="unknown",
        page_count=None,
        paragraph_count=paragraph_count,
        character_count=character_count,
        word_count=word_count,
        extraction_status="success" if content else "no_text",
        extraction_error=None,
        content_text=content,
        content_search=clean_search_text(content),
        metadata_json=safe_json_dumps({"encoding": "utf-8-sig"}),
    )

    if not content.strip():
        result.warnings.append("TXT file contained no readable content.")

    return result


def parse_pdf(path: Path) -> ExtractionResult:
    if fitz is None:
        raise RuntimeError("PDF parsing requires PyMuPDF (fitz). Install the project requirements to enable PDF support.")

    document = fitz.open(path)
    pages: list[dict[str, Any]] = []
    page_text_parts: list[str] = []
    raw_metadata = document.metadata or {}

    for page_index in range(document.page_count):
        page = document[page_index]
        text = page.get_text("text")
        pages.append({"page_number": page_index + 1, "text": text})
        page_text_parts.append(text)

    full_text = "\n\n".join(part for part in page_text_parts if part)
    word_count = len(re.findall(r"\S+", full_text))
    character_count = len(full_text)
    extraction_status = "success" if full_text.strip() else "no_text"
    extraction_error = None if full_text.strip() else "PDF contains no extractable text; OCR required."

    title = detect_title({}, path, full_text)

    result = ExtractionResult(
        source_path=path,
        source_format="pdf",
        source_sha256=sha256_file(path),
        file_size_bytes=path.stat().st_size,
        title=title,
        document_type="unknown",
        status="unknown",
        created_at_source=parse_iso_datetime(raw_metadata.get("creationDate")) or parse_iso_datetime(raw_metadata.get("creation_date")),
        modified_at_source=parse_iso_datetime(raw_metadata.get("modDate")) or parse_iso_datetime(raw_metadata.get("modificationDate")),
        page_count=document.page_count,
        paragraph_count=max(1, len(re.split(r"\n\s*\n", full_text)) if full_text.strip() else 0),
        character_count=character_count,
        word_count=word_count,
        extraction_status=extraction_status,
        extraction_error=extraction_error,
        content_text=full_text,
        content_search=clean_search_text(full_text),
        metadata_json=safe_json_dumps(raw_metadata),
        pages_json=safe_json_dumps(pages),
    )

    if extraction_status == "no_text":
        result.warnings.append("PDF contains no extractable text; OCR required.")

    document.close()
    return result


def parse_docx(path: Path) -> ExtractionResult:
    if DocxDocument is None:
        raise RuntimeError("DOCX parsing requires python-docx. Install the project requirements to enable DOCX support.")

    document = DocxDocument(str(path))
    blocks: list[dict[str, Any]] = []
    paragraphs: list[str] = []
    first_heading: str | None = None

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        paragraphs.append(text)
        block: dict[str, Any] = {"type": "paragraph", "text": text}
        if paragraph.style and paragraph.style.name:
            style_name = paragraph.style.name.lower()
            if style_name.startswith("heading"):
                block = {"type": "heading", "level": int(re.search(r"(\d+)", style_name).group(1)) if re.search(r"(\d+)", style_name) else 1, "text": text}
                if first_heading is None:
                    first_heading = text
        blocks.append(block)

    table_text_blocks: list[str] = []
    for table in document.tables:
        rows: list[str] = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if cells:
                rows.append(" | ".join(cell for cell in cells if cell))
        table_text = "\n".join(rows)
        if table_text.strip():
            table_text_blocks.append(table_text)
            blocks.append({"type": "table", "text": table_text, "rows": rows})

    content_parts = paragraphs + table_text_blocks
    content_text = "\n\n".join(content_parts)
    word_count = len(re.findall(r"\S+", content_text))
    character_count = len(content_text)
    extraction_status = "success" if content_text.strip() else "no_text"

    core_properties = getattr(document, "core_properties", None)
    metadata = {
        "title": getattr(core_properties, "title", None),
        "author": getattr(core_properties, "author", None),
        "subject": getattr(core_properties, "subject", None),
        "created": parse_iso_datetime(getattr(core_properties, "created", None)),
        "modified": parse_iso_datetime(getattr(core_properties, "modified", None)),
    }

    title = detect_title({}, path, content_text, first_heading)

    result = ExtractionResult(
        source_path=path,
        source_format="docx",
        source_sha256=sha256_file(path),
        file_size_bytes=path.stat().st_size,
        title=title,
        document_type="unknown",
        status="unknown",
        created_at_source=metadata.get("created"),
        modified_at_source=metadata.get("modified"),
        page_count=None,
        paragraph_count=len(paragraphs),
        character_count=character_count,
        word_count=word_count,
        extraction_status=extraction_status,
        extraction_error=None,
        content_text=content_text,
        content_search=clean_search_text(content_text),
        metadata_json=safe_json_dumps(metadata),
        blocks_json=safe_json_dumps(blocks),
    )

    if not content_text.strip():
        result.warnings.append("DOCX file contained no extractable text.")

    return result


def parse_supported_file(path: Path) -> ExtractionResult:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return parse_txt(path)
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    raise ValueError(f"Unsupported file type: {path.suffix}")


# ---------------------------------------------------------------------------
# RP entry and chunking helpers
# ---------------------------------------------------------------------------


def split_paragraphs(text: str) -> list[str]:
    if not text.strip():
        return []
    paragraph_candidates = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paragraph_candidates


def build_chunk_records(document_id: int, content_text: str, section_title: str | None = None) -> list[dict[str, Any]]:
    paragraphs = split_paragraphs(content_text)
    if not paragraphs:
        return []

    max_chunk_chars = 1200
    chunks: list[dict[str, Any]] = []
    chunk_index = 0

    for paragraph in paragraphs:
        sentences = [paragraph]
        if len(paragraph) > max_chunk_chars:
            parts: list[str] = []
            current = ""
            for part in re.split(r"(\s+)", paragraph):
                if len(current) + len(part) <= max_chunk_chars:
                    current += part
                else:
                    if current.strip():
                        parts.append(current.strip())
                    current = part.strip()
            if current.strip():
                parts.append(current.strip())
            sentences = parts if parts else [paragraph]

        for chunk_text in sentences:
            chunk_text = chunk_text.strip()
            if not chunk_text:
                continue
            chunks.append(
                {
                    "document_id": document_id,
                    "chunk_index": chunk_index,
                    "section_title": section_title,
                    "content_text": chunk_text,
                    "content_search": clean_search_text(chunk_text),
                    "source_location_json": safe_json_dumps({"source": "paragraph"}),
                }
            )
            chunk_index += 1

    return chunks


def detect_rp_entries(text: str, document_id: int) -> list[dict[str, Any]]:
    lines = text.splitlines()
    entries: list[dict[str, Any]] = []
    current_speaker: str | None = None
    current_lines: list[str] = []
    current_start = 0

    def flush() -> None:
        nonlocal current_speaker, current_lines, current_start
        if current_speaker is None:
            return
        content = "\n".join(current_lines).strip()
        if content:
            entries.append(
                {
                    "document_id": document_id,
                    "entry_index": len(entries),
                    "speaker_raw": current_speaker,
                    "character_id": None,
                    "player_id": None,
                    "content_text": content,
                    "source_location_json": safe_json_dumps({"line_start": current_start, "line_end": len(lines)}),
                }
            )
        current_speaker = None
        current_lines = []
        current_start = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if current_speaker is not None:
                current_lines.append("")
            continue

        speaker_match = re.match(r"^\[(?P<speaker>[^\]]+)\]\s*$", stripped)
        if speaker_match:
            flush()
            current_speaker = speaker_match.group("speaker").strip()
            current_start = index + 1
            continue

        speaker_match = re.match(r"^(?P<speaker>[A-Za-z0-9_.'’ -]+)\s*:\s*$", stripped)
        if speaker_match:
            flush()
            current_speaker = speaker_match.group("speaker").strip()
            current_start = index + 1
            continue

        speaker_match = re.match(r"^(?P<speaker>[A-Za-z0-9_.'’ -]+)\s*[—-]\s*(?P<body>.+)$", stripped)
        if speaker_match and len(stripped) < 140:
            flush()
            current_speaker = speaker_match.group("speaker").strip()
            current_start = index + 1
            current_lines = [speaker_match.group("body").strip()]
            continue

        if current_speaker is not None:
            current_lines.append(stripped)

    flush()
    return entries


# ---------------------------------------------------------------------------
# Import lifecycle
# ---------------------------------------------------------------------------


def insert_target_document(
    connection: sqlite3.Connection,
    parsed: ExtractionResult,
    *,
    source_id: int,
    import_id: int,
    imported_at: str,
) -> int:
    cursor = connection.execute(
        """INSERT INTO documents(
               source_id, import_id, source_filename, source_format, file_size_bytes,
               title_original, title_override, document_type, document_status,
               created_at_source, modified_at_source, imported_at, page_count,
               paragraph_count, character_count, word_count, extraction_status,
               extraction_error, content_text, content_search, metadata_json,
               pages_json, blocks_json, warnings_json)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_id,
            import_id,
            parsed.source_path.name,
            parsed.source_format,
            parsed.file_size_bytes,
            parsed.title,
            normalize_document_type(parsed.document_type),
            normalize_status(parsed.status),
            parsed.created_at_source,
            parsed.modified_at_source,
            imported_at,
            parsed.page_count,
            parsed.paragraph_count,
            parsed.character_count,
            parsed.word_count,
            parsed.extraction_status,
            parsed.extraction_error,
            parsed.content_text,
            parsed.content_search,
            parsed.metadata_json,
            parsed.pages_json,
            parsed.blocks_json,
            safe_json_dumps(parsed.warnings),
        ),
    )
    return int(cursor.lastrowid)


def insert_chunks(connection: sqlite3.Connection, document_id: int, chunks: Iterable[dict[str, Any]]) -> None:
    rows = list(chunks)
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO document_chunks (
            document_id,
            chunk_index,
            page_start,
            page_end,
            paragraph_start,
            paragraph_end,
            section_title,
            content_text,
            content_search,
            source_location_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id,
                row.get("chunk_index"),
                row.get("page_start"),
                row.get("page_end"),
                row.get("paragraph_start"),
                row.get("paragraph_end"),
                row.get("section_title"),
                row.get("content_text"),
                row.get("content_search"),
                row.get("source_location_json"),
            )
            for row in rows
        ],
    )


def insert_entries(connection: sqlite3.Connection, document_id: int, entries: Iterable[dict[str, Any]]) -> None:
    rows = list(entries)
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO document_entries (
            document_id,
            entry_index,
            speaker_raw,
            character_id,
            player_id,
            content_text,
            source_location_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id,
                row.get("entry_index"),
                row.get("speaker_raw"),
                row.get("character_id"),
                row.get("player_id"),
                row.get("content_text"),
                row.get("source_location_json"),
            )
            for row in rows
        ],
    )


def _document_logical_source_key(source_path: str) -> str:
    identity = json.dumps(
        {"source_path": source_path, "scope": {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "document:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def import_document(
    connection: sqlite3.Connection,
    source_path: Path | str,
    *,
    input_root: Path | None = None,
    metadata_overrides: dict[str, Any] | None = None,
    reimport: bool = False,
) -> DocumentImportResult:
    """Import one file as a source capture, import run, document, chunks, and entries."""
    del reimport
    source_path = Path(source_path).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Source file does not exist: {source_path}")

    source_format = SUPPORTED_EXTENSIONS.get(source_path.suffix.lower())
    if source_format is None:
        return DocumentImportResult(
            document_id=None,
            source_path=str(source_path),
            source_sha256="",
            source_format="unsupported",
            document_type="unknown",
            status="unknown",
            title=source_path.stem,
            warnings=[f"Unsupported extension for {source_path.name}."],
            extraction_status="unsupported",
            extraction_error=f"Unsupported extension: {source_path.suffix}",
        )

    source_hash = sha256_file(source_path)
    registered_at = datetime.now(timezone.utc).isoformat()
    source_path_text = str(source_path)
    source_id = register_source_capture(
        connection,
        logical_source_key=_document_logical_source_key(source_path_text),
        source_kind=source_format,
        source_path=source_path_text,
        sha256=source_hash,
        byte_size=source_path.stat().st_size,
        scope_json="{}",
        registered_at=registered_at,
    )
    connection.commit()

    import_id = start_import_run(
        connection,
        source_id=source_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        importer_name="document_parser",
        importer_version="2",
    )
    connection.commit()
    connection.execute("SAVEPOINT document_import_materialization")

    try:
        parsed = parse_supported_file(source_path)
        if parsed.source_sha256 != source_hash:
            raise RuntimeError("Source file changed while it was being imported")

        parsed.title = detect_title({"title": parsed.title}, source_path, parsed.content_text, None)
        parsed.status = "unknown"
        parsed.document_type = classify_document(None, parsed.title, parsed.content_text)
        if metadata_overrides:
            parsed = apply_metadata_overrides(source_path, input_root, metadata_overrides, parsed)
            parsed.title = detect_title({"title": parsed.title}, source_path, parsed.content_text, parsed.title)
            if parsed.document_type == "unknown":
                parsed.document_type = classify_document(None, parsed.title, parsed.content_text)
        if parsed.title is None:
            parsed.title = detect_title_from_path(source_path)

        parsed.document_type = normalize_document_type(parsed.document_type)
        parsed.status = normalize_status(parsed.status)
        imported_at = datetime.now(timezone.utc).isoformat()
        document_id = insert_target_document(
            connection,
            parsed,
            source_id=source_id,
            import_id=import_id,
            imported_at=imported_at,
        )
        parsed.chunks = build_chunk_records(document_id, parsed.content_text)
        parsed.entries = detect_rp_entries(parsed.content_text, document_id)
        insert_chunks(connection, document_id, parsed.chunks)
        insert_entries(connection, document_id, parsed.entries)

        if parsed.extraction_status == "error":
            import_status = "failed"
        elif parsed.extraction_status == "partial" or (
            parsed.warnings and parsed.extraction_status != "no_text"
        ):
            import_status = "partial"
        else:
            import_status = "succeeded"

        finished_at = datetime.now(timezone.utc).isoformat()
        finish_import_run(
            connection,
            import_id=import_id,
            status=import_status,
            finished_at=finished_at,
            records_seen=1,
            records_written=1,
            warnings_json=safe_json_dumps(parsed.warnings),
            error_summary=parsed.extraction_error,
        )
        if import_status == "succeeded":
            accept_import_run(connection, import_id=import_id, accepted_at=finished_at)
        connection.execute("RELEASE SAVEPOINT document_import_materialization")
        connection.commit()
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT document_import_materialization")
        connection.execute("RELEASE SAVEPOINT document_import_materialization")
        finish_import_run(
            connection,
            import_id=import_id,
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            records_seen=1,
            records_written=0,
            warnings_json=safe_json_dumps([str(exc)]),
            error_summary=str(exc),
        )
        connection.commit()
        return DocumentImportResult(
            document_id=None,
            source_path=source_path_text,
            source_sha256=source_hash,
            source_format=source_format,
            document_type="unknown",
            status="unknown",
            title=source_path.stem,
            warnings=[f"Import failed: {exc}"],
            extraction_status="error",
            extraction_error=str(exc),
            source_id=source_id,
            import_id=import_id,
            import_status="failed",
        )

    return DocumentImportResult(
        document_id=document_id,
        source_path=source_path_text,
        source_sha256=source_hash,
        source_format=source_format,
        document_type=parsed.document_type,
        status=parsed.status,
        title=parsed.title,
        warnings=parsed.warnings,
        extraction_status=parsed.extraction_status,
        extraction_error=parsed.extraction_error,
        source_id=source_id,
        import_id=import_id,
        import_status=import_status,
    )


# ---------------------------------------------------------------------------
# CLI and reporting
# ---------------------------------------------------------------------------


def discover_files(input_directory: Path, recursive: bool = True) -> list[Path]:
    if not input_directory.exists():
        raise FileNotFoundError(f"Input directory not found: {input_directory}")

    if recursive:
        paths = sorted(p for p in input_directory.rglob("*") if p.is_file())
    else:
        paths = sorted(p for p in input_directory.iterdir() if p.is_file())
    return [path for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS]


def build_report(import_results: list[DocumentImportResult]) -> dict[str, Any]:
    counts = {fmt: 0 for fmt in ("txt", "pdf", "docx")}
    status_counts = {status: 0 for status in ["success", "partial", "no_text", "unsupported", "error", "duplicate"]}
    total_documents = 0
    total_chunks = 0
    total_entries = 0
    total_characters = 0
    total_words = 0
    total_pages = 0

    for result in import_results:
        if result.source_format in counts:
            counts[result.source_format] += 1
        if result.extraction_status in status_counts:
            status_counts[result.extraction_status] += 1
        total_documents += 1 if result.document_id is not None else 0
        total_chunks += 0
        total_entries += 0

    return {
        "files_discovered": len(import_results),
        "files_supported": sum(1 for item in import_results if item.source_format in counts),
        "files_imported": sum(1 for item in import_results if item.document_id is not None and not item.duplicate),
        "files_skipped": sum(1 for item in import_results if item.duplicate),
        "duplicates": sum(1 for item in import_results if item.duplicate),
        "failures": sum(1 for item in import_results if item.extraction_status == "error"),
        "warnings": sum(1 for item in import_results if item.warnings),
        "by_format": counts,
        "status_counts": status_counts,
        "total_documents": total_documents,
        "total_chunks": total_chunks,
        "total_entries": total_entries,
        "total_characters": total_characters,
        "total_words": total_words,
        "total_pages": total_pages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import TXT/PDF/DOCX documents into the RP archive database.")
    parser.add_argument("input_directory", type=Path, help="Root directory containing raw documents.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE_PATH, help="SQLite database path to create or update.")
    parser.add_argument("--metadata", type=Path, help="Optional document metadata override YAML file.")
    parser.add_argument("--recursive", action="store_true", help="Recursively discover files under the input directory.")
    parser.add_argument("--dry-run", action="store_true", help="Discover and parse without writing records.")
    parser.add_argument("--reimport", action="store_true", help="Deprecated compatibility option; every import attempt is recorded.")
    parser.add_argument("--verbose", action="store_true", help="Print extra status output.")
    args = parser.parse_args(argv)

    input_root = args.input_directory.resolve()
    db_path = args.db.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    create_document_tables(connection)

    metadata_overrides = {}
    if args.metadata and args.metadata.exists():
        metadata_overrides = metadata_file_to_map(args.metadata, input_root)

    discovered = discover_files(input_root, recursive=args.recursive or True)
    report: list[DocumentImportResult] = []

    print("Document Archive Import")
    print("======================")
    print(f"Input: {input_root}")
    print(f"Database: {db_path}")
    print(f"Discovered: {len(discovered)} files")
    print(f"  TXT: {sum(1 for item in discovered if item.suffix.lower() == '.txt')}")
    print(f"  PDF: {sum(1 for item in discovered if item.suffix.lower() == '.pdf')}")
    print(f"  DOCX: {sum(1 for item in discovered if item.suffix.lower() == '.docx')}")

    for file_path in discovered:
        try:
            if args.dry_run:
                parsed = parse_supported_file(file_path)
                parsed.title = detect_title({}, file_path, parsed.content_text, parsed.title)
                parsed.document_type = classify_document(None, parsed.title, parsed.content_text)
                report.append(
                    DocumentImportResult(
                        document_id=None,
                        source_path=str(file_path),
                        source_sha256=parsed.source_sha256,
                        source_format=parsed.source_format,
                        document_type=parsed.document_type,
                        status=parsed.status,
                        title=parsed.title,
                        warnings=parsed.warnings,
                        extraction_status=parsed.extraction_status,
                        extraction_error=parsed.extraction_error,
                    )
                )
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
            report.append(result)
            if args.verbose:
                print(f"[IMPORT] {file_path.name} -> {result.document_id} ({result.extraction_status})")
        except Exception as exc:  # pragma: no cover - CLI-level failure reporting
            report.append(
                DocumentImportResult(
                    document_id=None,
                    source_path=str(file_path),
                    source_sha256=sha256_file(file_path),
                    source_format=SUPPORTED_EXTENSIONS.get(file_path.suffix.lower(), "unsupported"),
                    document_type="unknown",
                    status="unknown",
                    title=file_path.stem,
                    warnings=[str(exc)],
                    extraction_status="error",
                    extraction_error=str(exc),
                )
            )
            if args.verbose:
                print(f"[ERROR] {file_path.name}: {exc}")

    summary = build_report(report)
    print(f"Imported: {summary['files_imported']}")
    print(f"Skipped: {summary['files_skipped']}")
    print(f"Failed: {summary['failures']}")
    print(f"Warnings: {summary['warnings']}")
    print(f"Status: {summary['status_counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
