```markdown
# Discord RP Archive

A local archiving and research application for long-running **Discord roleplay** servers. It ingests immutable raw exports (Discord JSON plus TXT/PDF/DOCX documents) into a single authoritative **SQLite** index, then layers on deterministic analysis — scene-boundary detection, provisional segmentation, character-identity inference, and attribution confidence — with a desktop GUI for reading, searching, curation, and human review. An optional local **Ollama** model powers an evidence-grounded Q&A view that answers only from retrieved archive records.

The design principles: the raw corpus is immutable; SQLite is the single source of truth; automatic analysis produces *evidence and candidates* (never silent conclusions); and anything consequential — accepting a character attribution, curating a scene — is human-reviewed with durable audit history.

## Features

- **Two ingestion paths** writing to one migration-backed schema:
  - DiscordChatExporter-style JSON exports (`src/build_archive.py`)
  - TXT / PDF / DOCX documents (`src/document_parser.py`, wrapper `src/alt_archive_builder.py`)
- **Versioned SQLite migrations** (001–022) with full provenance — source files, hashes, import runs, and normalized observations are preserved rather than overwritten.
- **Deterministic analysis pipeline (phases B–G):** signal extraction → boundary evidence/scoring → provisional spans → identity candidates → attribution confidence → durable human review → orchestration/reporting. Operational scores are *evidence units*, not probabilities, and never auto-accept anything.
- **Desktop GUI** (Tkinter) with tabs for Home, Search, Reader, Ask (Ollama), and Attribution Review, plus light/dark/system theming.
- **Local-only Q&A:** the Ask view retrieves accepted messages by term overlap and asks a locally running Ollama model to answer strictly from that evidence with citations — no cloud calls, no request made when there is no matching evidence.
- **Test suite** (`tests/`) covering importers, segmentation, signals, attribution, review, pipeline, sync, search, reader, GUI export/theming, and migrations.

## Repository layout
```

Items committed to Git:

```text
archive/            Core library (importers' analysis layer), Tkinter GUI, Ollama client
  migrations/       Versioned schema migrations (001–022) + runner
src/                Standalone importers/builders and their CLIs
tests/              pytest suite
docs/               Architecture, data model, implementation schema, roadmap, docs
```

Local-only (gitignored — see *Privacy & local data*):

```text
00_Admin/           Server maps and admin artifacts
01_Raw/             Immutable raw exports (JSON / TXT / PDF / DOCX)
02_Processed/       Generated SQLite database(s), e.g. archive.sqlite3
03_Reader/          Reader outputs
04_Search/          Search index/work artifacts
05_Backups/         Timestamped backup databases
```

## Requirements

- **Python 3** with the standard-library `tkinter` available (required for the desktop app).
- Dependencies (`src/requirements.txt`):

| Package | Purpose |
| --- | --- |
| `PyMuPDF` | PDF parsing |
| `python-docx` | DOCX parsing |
| `PyYAML` | Server-map / metadata YAML files |
| `pytest` | Test suite |
| `tzdata` | Timezone data for timestamp normalization |

- **Ollama** (optional) — only needed for the Ask tab. Runs locally, default endpoint `http://127.0.0.1:11434`, overridable with the `OLLAMA_HOST` environment variable.

## Setup

```bash
git clone <your-repo-url> Discord-RP-Archive
cd Discord-RP-Archive

python -m venv .venv
# Activate your virtual environment first (.venv\Scripts\activate on Windows, source .venv/bin/activate elsewhere)
pip install -r `requirements.txt`
```

Create and migrate the archive database (defaults to `02_Processed/archive.sqlite3`):

```bash
python -m archive migrate --database `archive.sqlite3`
```

## Usage

### Import Discord JSON exports

Imports one file or recursively scans a directory of DiscordChatExporter JSON files. Defaults to `01_Raw/`.

```bash
python `build_archive.py` [path] [--database PATH] [--server-map PATH]
```

- `[path]` — optional JSON file or directory (defaults to `01_Raw/`)
- `--database` — SQLite output path (defaults to the processed archive DB)
- `--server-map` — optional YAML server map used for ID expectation checks (warnings only; never rejects source metadata)

A run is **accepted** only when it completes cleanly. Partial or failed runs are recorded as such and do not advance the current accepted capture, so re-importing a corrected export is safe.

### Import documents (TXT / PDF / DOCX)

```bash
python `alt_archive_builder.py` [input_directory] [--db PATH] [--metadata FILE] [--recursive] [--dry-run] [--verbose]
```

- `[input_directory]` — directory containing raw documents (defaults to `01_Raw/`)
- `--db` — SQLite database path
- `--metadata` — optional YAML metadata override file
- `--recursive` — scan subdirectories recursively
- `--dry-run` — parse without writing to the database
- `--verbose` — extra progress output

### Desktop GUI

```bash
python -m archive gui --database `archive.sqlite3` --raw-directory 01_Raw
```

Tabs: **Home** (startup sync status), **Search**, **Reader** (channel context around a result), **Ask** (local Ollama Q&A with citations), and **Attribution Review** (the human-in-the-loop queue). Theme choice is persisted locally.

### Inference pipeline (phases B–F1)

Runs the analysis stages over an accepted-source scope, with incremental reuse of unchanged stages:

```bash
python -m archive pipeline run      [--mode full|incremental] [--server-id N] [--channel-id N] \
                                    [--source-id N] [--import-id N] [--message-id ID ...] [--format text|json]
python -m archive pipeline dry-run  # plan without persisting inference stages
python -m archive pipeline status   # compare current inputs against the latest run
python -m archive pipeline report --run-id <id>
```

- `--mode incremental` (default) reuses each stage when its exact inputs match; `full` recomputes all stages.
- With no scope filters, the loader selects the current accepted imports. An explicit import selects that capture only — it does not widen to others.
- A run writes a durable manifest before doing work and records progress between stages, so a failed request is visible as incomplete rather than masquerading as complete.

### Run tests

```bash
pytest
```

## How the analysis pipeline works

The archive deliberately keeps ingestion separate from analysis, and each stage has one responsibility:

```text
RAW FILE → DISCOVERY → VALIDATION → PARSING → NORMALIZATION → DATABASE → FTS INDEX
                                                                        │
                                                    ACCEPTED MESSAGE OBSERVATIONS
                                                                        ▼
        B SIGNAL EXTRACTION → C BOUNDARY EVIDENCE → D PROVISIONAL SPANS
             → E IDENTITY CANDIDATES → F1 ATTRIBUTION CONFIDENCE
                  → F2 HUMAN REVIEW  →  F3 REVIEW GUI   (G orchestrates B–F1)
```

| Phase | Module(s) | Produces |
| --- | --- | --- |
| **B** Signals | `archive/signals.py` | Deterministic evidence: timestamp gaps, channel/author changes, replies, scene-control markers, speaker labels, OOC and context lines. Pure analysis — no DB writes, no scoring. |
| **C** Boundaries | `archive/boundaries.py`, `boundary_persistence.py` | Scores each adjacent pair; classifies hard / strong/soft candidates with full contribution provenance. |
| **D** Segmentation | `archive/segmentation.py`, `segmentation_persistence.py` | Non-overlapping provisional spans per `(server, channel, source)` scope. Drafts only — never writes curated scenes. |
| **E** Identity | `archive/identity_inference.py`, `identity_candidate_persistence.py` | Matches speaker labels to registered aliases; unmatched labels become stable provisional candidates (no auto-acceptance). |
| **F1** Confidence | `archive/attribution_confidence.py`, `_persistence.py` | Per-message candidate snapshots as integer evidence units. `high_confidence` is a triage class, not automatic acceptance. |
| **F2** Review API | `archive/attribution_review.py` | GUI-neutral queue + action API with append-only audit events and durable decisions. |
| **F3** Review GUI | `archive/gui.py`, `attribution_review_gui.py` | The Attribution Review tab; consequence-bearing actions require confirmation. |
| **G** Orchestration | `archive/pipeline.py` | Coordinates B–F1, records runs in the `pipeline_runs` manifest, supports incremental reuse and reporting. Does not touch curated scenes or F2 decisions. |

Scene curation (`archive/scenes.py`) is separate from analysis: membership is explicit and ordered by curator choice (never inferred from timestamps), with participants tracked independently. Attribution claims (`archive/attribution.py`) are the reviewable system for registered identities — rules create *pending* proposals only; models may| Phase | Module(s) | Produces |
| --- | --- | --- |
| **B** Signals | `archive/signals.py` | Deterministic evidence: timestamp gaps, channel/author changes, replies, scene-control markers, speaker labels, OOC and context lines. Pure analysis — no DB writes, no scoring. |
| **C** Boundaries | `archive/boundaries.py`, `boundary_persistence.py` | Scores each adjacent pair; classifies hard / strong/soft candidates with full contribution provenance. |
| **D** Segmentation | `archive/segmentation.py`, `segmentation_persistence.py` | Non-overlapping provisional spans per `(server, channel, source)` scope. Drafts only — never writes curated scenes. |
| **E** Identity | `archive/identity_inference.py`, `identity_candidate_persistence.py` | Matches speaker labels to registered aliases; unmatched labels become stable provisional candidates (no auto-acceptance). |
| **F1** Confidence | `archive/attribution_confidence.py`, `_persistence.py` | Per-message candidate snapshots as integer evidence units. `high_confidence` is a triage class, not automatic acceptance. |
| **F2** Review API | `archive/attribution_review.py` | GUI-neutral queue + action API with append-only audit events and durable decisions. |
| **F3** Review GUI | `archive/gui.py`, `attribution_review_gui.py` | The Attribution Review tab; consequence-bearing actions require confirmation. |
| **G** Orchestration | `archive/pipeline.py` | Coordinates B–F1, records runs in the `pipeline_runs` manifest, supports incremental reuse and reporting. Does not touch curated scenes or F2 decisions. |

Scene curation (`archive/scenes.py`) is separate from analysis: membership is explicit and ordered by curator choice (never inferred from timestamps), with participants tracked independently. Attribution claims (`archive/attribution.py`) are the reviewable system for registered identities — rules create *pending* proposals only; models may
