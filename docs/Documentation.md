# Discord RP Archive Documentation

This document records the archive's implemented data model, import and analysis
workflows, review boundaries, migrations, and operational limitations. Historical
live-database measurements are explicitly dated/contextual and should not be
read as the result of later inference pipeline runs. The target schema is
specified in [Implementation Schema.md](Implementation%20Schema.md).

## Purpose

The migration system preserves useful existing records and provenance while
introducing shared source/import tables and strongly typed content tables. The
Discord and document importers now write to the migration-backed schema; legacy
table families remain under `legacy_*` names where their shapes conflict with
the target schema.

## Attribution Workflow

Run migrations before using the attribution API. The Python functions in
`archive.attribution` create players and characters, register character aliases,
record time-bounded identity/player and player/character links, generate
pending proposals, list the review queue, and accept or reject claims. Database
transactions remain caller-controlled; commit the mapping and review changes
when they are ready to persist.

`propose_message_attributions()` applies the exact-marker and known-identity
rules to one message observation or all imported observations. These rules
only create pending proposals. Model integrations should call
`propose_attribution(..., method="model", evidence=...)`; that API also creates
a pending claim and cannot auto-accept model output. `review_attribution()`
records the reviewer, decision, optional note, and an append-only audit event.
Accepting a different claim for the same message observation supersedes the
previous accepted claim. A new import capture creates new message observations;
attributions are not automatically copied to those observations.

```python
import sqlite3

from archive.attribution import (
    create_character,
    create_player,
    link_identity_to_player,
    link_player_to_character,
    list_pending_attributions,
    propose_message_attributions,
    review_attribution,
)

connection = sqlite3.connect("02_Processed/archive.sqlite3")
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA foreign_keys = ON")

player_id = create_player(connection, "Player One")
character_id = create_character(connection, "Winter")
link_identity_to_player(connection, identity_id=1, player_id=player_id)
link_player_to_character(connection, player_id, character_id)
propose_message_attributions(connection)

for claim in list_pending_attributions(connection):
    print(claim["attribution_id"], claim["player_name"], claim["character_name"])

review_attribution(connection, 1, decision="accepted", actor="reviewer")
connection.commit()
connection.close()
```

## Scene Curation

Scene support is available after running the migrations (schema version 15,
migration `015_create_scenes`). The Python functions in `archive.scenes` create
arcs and scenes, explicitly add or remove message observations, and record
scene participants. The caller controls database transactions and should
commit the curated changes when ready.

Scene membership is not inferred from timestamps. Add only the message
observations that constitute the scene, using `message_observation_id` rather
than a Discord ID: separate imports can retain multiple observations of the
same Discord message. The membership's `position` preserves a curator-defined
order. Discord channels are derived from member messages through the
`scene_channels` view, avoiding a second channel assignment that could disagree
with the source message. `list_scene_messages()` includes source author and
channel fields and any accepted character/player attribution; pending
attribution proposals are not presented as accepted identities.

```python
import sqlite3

from archive.scenes import (
    add_message_to_scene,
    add_scene_participant,
    create_arc,
    create_scene,
    list_scene_messages,
)

connection = sqlite3.connect("02_Processed/archive.sqlite3")
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA foreign_keys = ON")

arc_id = create_arc(connection, "The Arrival")
scene_id = create_scene(
    connection,
    "The meeting",
    arc_id=arc_id,
    fictional_time="Year 4, evening",
)
add_message_to_scene(connection, scene_id, message_observation_id=123, added_by="curator")
add_scene_participant(connection, scene_id, character_id=1, player_id=1, added_by="curator")

for message in list_scene_messages(connection, scene_id):
    print(message["discord_message_id"], message["channel_name"], message["character_name"])

connection.commit()
connection.close()
```

## Inference, Attribution Review, and Pipeline Orchestration (Phases B–G)

The analysis workflow is deliberately separate from curated scene data and is
implemented as distinct, ordered responsibilities:

```text
B: signal extraction
   -> C: boundary evidence and scoring
   -> D: provisional temporal spans
   -> E: identity candidate inference
   -> F1: attribution confidence
   -> F2: human review actions and durable audit
   -> F3: review GUI
   -> G: orchestration and reporting for B–F1
   -> H: event and arc curation over candidate spans
```

This sequence does not collapse the concepts into one detector. Segmentation
provides evidence about where a potential scene may begin or end; reconstruction
decides which messages belong to which candidate scenes; attribution evidence
concerns represented characters and players; and confidence/review distinguish
automatically triageable results from conclusions that require a person.
Phase G runs only through F1. It does not apply F2 review actions, create
curated scenes, or claim to solve later reconstruction. Phase H is an
explicit human review and curation layer over deterministic candidate spans:
reviewers can inspect a group of provisional spans, decide whether it constitutes
a coherent event, and optionally accept it into the authoritative scene/arc
model.

### B — Deterministic signal extraction

`archive/signals.py` operates on normalized message-observation records and is
independent of persistence, scene curation, and GUI behavior. A signal includes
its type, source message-observation IDs, observed value, evidence, and an
explicit-versus-heuristic evidence kind. Extraction reports evidence; it does
not score scene boundaries or calculate attribution confidence.

Implemented signals include timestamp gaps and timestamp availability or
validity, channel changes, raw-author changes, reply relationships, source and
import changes, exact recognized scene-control marker lines, leading bracketed
speaker labels, leading OOC markers, labeled `Location:`, `Setting:`, and
`Time:` context lines, and thread-related changes only where thread metadata is
actually present in the preserved source payload. Contradictory evidence is
retained. Speaker labels are observations, not proof of registered character
identity or player identity.

The normalized `messages` schema has no `thread_id` column, and
`thread_name` is not persisted as a normalized field. Thread metadata must
therefore be read from preserved `source_json` when the supported key is
present; it must never be assumed for every message. Signals cannot currently
establish natural-language scene transitions, full character participation,
arbitrary references not represented by the normalized reply field, or reply
targets missing from the supplied observations.

### C — Boundary evidence and scoring

`archive/boundaries.py` scores adjacent-message transitions after the caller
has selected and ordered the input. It neither sorts global messages nor
creates scene membership. Its versioned configuration and results preserve
each contribution, source observations, supporting and contradictory evidence,
raw score, and classification.

The current ruleset is `event_boundaries_v1`. Recognized hard scene markers
and a source-capture change create hard boundary candidates; an import-run
change alone does not. Soft evidence includes timestamp gaps, channel changes,
narrative-context changes, author and speaker-label changes, and thread changes
when available. A direct reply is continuity evidence and can contradict a
boundary. Timestamp gaps are normalized against the configured reference.
Weights and thresholds are initial operational values, not empirically
calibrated probabilities. `strong_candidate` means there is evidence at the
transition edge; it is not proof that the messages on either side belong to
different scenes.

Migration 016 stores boundary-detection runs, the ordered observations
examined, and non-`none` boundary candidates. These records are inference
outputs, separate from curated `scenes` and `scene_messages`.

### D — Provisional spans, not curated scenes

`archive/segmentation.py` and `archive/segmentation_persistence.py` create
deterministic candidate spans from scoped, ordered observations and C's
boundary evidence. The current scope is server, channel, and source capture.
Hard boundaries and configured inactivity gaps can split spans; a soft
boundary score alone does not. A direct in-scope reply can bridge an inactivity
gap. Reply links retain resolved, unresolved, ambiguous, and cross-scope
outcomes.

Migration 017 stores segmentation runs, candidate spans, candidate memberships,
reply links, and the boundary decisions used for the partition. These are
provisional inference records only. The first implementation is a non-overlap
partition per scope, not an assumption that all future reconstruction must be
linear: it does not separate simultaneous interactions in one channel or
reconstruct a scene that moves between channels. It does not create or update
`scenes` or `scene_messages`.

### E — Identity evidence and provisional candidates

`archive/identity_inference.py` and
`archive/identity_candidate_persistence.py` interpret supported header speaker
labels independently of segmentation. Exact normalized registered-alias
matches remain registered-character evidence; multiple matches remain
ambiguous; unmatched labels form stable provisional candidates within a known
server. When server scope is missing, observations are not merged across
unknown contexts. Discord author identity is contextual evidence only.

Migration 018 stores versioned inference runs, provisional candidates,
per-message evidence, and identity-review history/overrides. This phase does
not create characters or players, infer players, attribute messages, or make
candidate identities depend on D spans.

### F1 — Confidence snapshots

`archive/attribution_confidence.py` and
`archive/attribution_confidence_persistence.py` aggregate E evidence into
per-message candidate and alternative snapshots. Scores are evidence units,
not probabilities. High-confidence is a review-priority classification, never
automatic acceptance. Ambiguity, unresolved evidence, conflicts, and linked
provisional identities remain subject to human review. Existing authoritative
attributions and their audit events remain authoritative.

Migration 019 stores confidence runs, candidates, and evidence contributions.
F1 recomputation creates a new inference snapshot; it does not edit F2 decisions
or the curated scene tables.

### F2 — Durable human review API

`archive/attribution_review.py` provides a GUI-neutral review queue, read model,
and action API. Queue results expose priorities, evidence, current authoritative
attributions, review state, and stale status. Supported human operations
include confirming or rejecting an attribution, assigning a different
registered character, leaving evidence unresolved, linking a provisional
identity to a registered character, splitting/reassigning/merging provisional
evidence, excluding/restoring evidence, and attaching human notes.

Migration 020 adds review state, evidence overrides, input-change tracking,
and immutable review events; migration 021 hardens F2 review semantics. Every
human action has durable audit history containing the actor, time, note,
relevant source candidate/evidence, prior-state details, and linked review or
attribution event where applicable. The current decision table is a projection
of that history, not a replacement for it. Rejections do not silently revoke a
different already-accepted attribution; conflicts remain visible. Exclusions
are explicit and reversible; original message observations are not altered.

Bulk review is allowed only where selected messages have the same candidate
and matching evidence. Each message still receives its own review event.
Per-message overrides are retained. New inference does not overwrite manual
decisions; stale snapshots are surfaced and stale actions are rejected, so a
reviewer must act on a current snapshot rather than accidentally accepting old
recommendations.

### F3 — Review GUI

`archive/gui.py` includes an Attribution Review tab. GUI-specific
presentation/service adaptation lives in `archive/attribution_review_gui.py`;
review queue, candidate evidence, history, and actions continue to use the F2
API. The UI provides bounded queue/evidence pages, queue filters, candidate
details, surrounding channel context labeled as context, review history,
confirmation for consequential actions, and stale-action feedback. It does not
write attribution/audit tables directly and does not edit curated scenes or
memberships.

### G — Orchestration, cache reuse, and reporting

`archive/pipeline.py` coordinates the existing B, C, D, E, and F1 services.
Migration 022 adds the `pipeline_runs` manifest. A manifest records request
scope, pipeline/schema/ruleset versions, configuration, fingerprints, stage
run references and statistics, warnings/errors, status, and timestamps. It is
updated while running and finalized once; finalized manifests cannot be
updated or deleted. Stage outputs stay in their existing persistence tables.

The CLI supports these operations:

```powershell
# Process the current accepted observations using safe incremental reuse
.\.venv\Scripts\python.exe -m archive pipeline run --mode incremental

# Recompute the selected scope, unless all full-run inputs already match
.\.venv\Scripts\python.exe -m archive pipeline run --mode full

# Persist a G manifest and plan without writing inference-stage records
.\.venv\Scripts\python.exe -m archive pipeline dry-run

# Restrict processing to an explicit accepted import and/or scope
.\.venv\Scripts\python.exe -m archive pipeline run --import-id 123 --channel-id 10

# Inspect whether the latest completed result matches current inputs/config
.\.venv\Scripts\python.exe -m archive pipeline status --channel-id 10

# Read the latest report or select one by manifest UUID
.\.venv\Scripts\python.exe -m archive pipeline report --run-id <pipeline-run-id>
```

Each command accepts `--database` and `--format text|json`; run and dry-run
also accept `--server-id`, `--channel-id`, `--source-id`, `--import-id`, and
repeatable `--message-id`. Status supports the server/channel/source/import
scope filters. By default, input selection uses the latest accepted import of
each current source capture. An explicit accepted `--import-id` may select a
superseded historical capture; message-subset selection is intersected with
that accepted scope, and missing/out-of-scope observations are rejected.
Empty scopes are rejected. The caller must provide a connection without an
open transaction; the CLI creates and owns such a connection.

Incremental mode reuses each completed stage only when its input fingerprint
and relevant configuration match. Boundary configuration changes invalidate C
and dependent D, but need not invalidate E/F1. Alias changes invalidate E/F1
without requiring new C/D runs. Confidence reuse also checks F2 staleness and
review-relevant inputs. Full mode recomputes all inference stages when any
relevant stage input or review state differs; an identical completed full run
is a no-op reuse. Signal extraction itself is recalculated for current
fingerprinting and reported counts, rather than stored as a separate run.

Dry-run persists the G manifest only. Exact cached outputs may be identified
as reusable; uncached stages are marked planned, and uncached confidence work
is not calculated. A regular run commits the manifest at start and saves
progress between stages. If a later stage fails, the failed manifest records
the completed stage references and error. Already persisted upstream inference
outputs are not rolled back across the whole pipeline; an incremental retry
can safely reuse stages whose inputs still match.

Reports include scope and input fingerprints, stages and run IDs, counts,
candidate deltas, review-queue summary/staleness, warnings, and errors. Deltas
describe current inference support, not deletion of historical facts. F2 is
read for status/reporting only: G never confirms, rejects, links, excludes, or
otherwise changes a human review decision. G also never creates or changes
arcs, scenes, scene membership, participants, or attribution records. Test
with temporary/controlled archives or explicit small scopes; do not assume a
production-sized run is one atomic transaction.

### H — Event and arc curation

`archive/event_curation.py` adds a thin human-curation layer on top of the
existing deterministic segmentation pipeline. It does not replace the
authoritative `scenes`/`arcs` model or re-interpret the boundary algorithm. It
instead groups existing provisional candidate spans into reviewable event
candidates, records the reasoning and review lifecycle, and allows a curator to
accept a candidate into the authoritative scene archive or assign it to an arc
for later editorial work.

The generator is intentionally deterministic. Candidate spans are sorted by
server, channel, source, time, and span ID; adjacent spans are merged only when
they remain in scope and the observed gap stays within the configured threshold.
A run stores its input fingerprint, algorithm version, configuration hash, and the
candidate/span memberships it produced; duplicated inputs reuse the same run
identity rather than creating a second candidate set. This keeps event
candidates traceable to the same evidence used by D without mixing them with
curated scenes.

Migration 023 adds the durable storage for the H layer. The schema adds
`event_candidate_runs`, `event_candidates`, `event_candidate_members`,
`event_candidate_arc_memberships`, and `event_candidate_review_events`. The
candidate record keeps its provisional title/description/status, stale reason,
accepted scene reference, and review state; membership rows preserve the exact
message and candidate-span evidence used to form the candidate; review events
provide append-only audit history for accept / reject / defer / stale /
adjust-grouping / arc-assignment actions.

The supported lifecycle is explicit and human-mediated:

- `generate_event_candidates()` creates reviewable candidates from current
  candidate spans.
- `list_event_candidates()` and `get_event_candidate()` read the queue and detail
  model.
- `update_event_candidate()` edits the provisional metadata and review state.
- `reject_event_candidate()` and `defer_event_candidate()` record a human
  decision without touching authoritative scene records.
- `mark_event_candidate_stale()` marks a candidate as no longer current when the
  input evidence has changed.
- `assign_event_candidate_to_arc()` and `remove_event_candidate_from_arc()` add
  or remove arc-level grouping without modifying archived scenes.
- `adjust_event_candidate_grouping()` allows a curator to redefine the grouped
  candidate spans while retaining historical evidence.
- `accept_event_candidate()` creates a real `scene` record from the selected
  candidate and copies the candidate's member messages into it, leaving the
  candidate itself as a durable review artifact and audit trail.

The `Arc & Event Curation` GUI tab in `archive/gui.py` remains intentionally
lightweight. It presents the queue and detail surface but does not embed the
generation or acceptance logic; those remain in the service layer so the app can
present a review workflow without bypassing the core rules or creating a second
authoritative narrative model.

This phase is additive and safety-oriented. It makes the provisional event and
arc curation process visible and reviewable without replacing the authoritative
scene/arc pipeline or silently rewriting existing curated records.

## Target Archive Model

The target archive is not a generic polymorphic graph. It is organized around
five shared architectural boundaries:

```text
archive_sources
archive_imports
archive_content
archive_entities
archive_links
```

Within `archive_content`, content remains strongly typed:

```text
messages
documents
document_chunks
document_entries
```

Within `archive_entities`, semantically distinct concepts remain explicit:

```text
players
characters
scenes
arcs
locations
discord_identities
```

`archive_links` is reserved for content-to-content relationships only, such as:

```text
message -> message
message -> document
document -> document
```

Editorial relationships are modeled as explicit tables or explicit foreign keys,
not as generic content-to-entity links:

```text
CONTENT RELATIONSHIPS
    message -> message
    document -> document
    message -> document

EDITORIAL RELATIONSHIPS
    message -> character       message_attributions
    message -> player           message_attributions
    message -> scene            scene_messages
    scene -> arc                scenes.arc_id
    scene -> location           scenes.location_id
    scene -> character/player  scene_participants
```

This avoids a "god table" and keeps the database aligned with the actual
semantics of the records. A character is not merely an entity, and a scene is
not merely an entity; they carry different constraints, review workflows, and
queries.

## Archive Invariants

1. Raw files are never modified by the application.
2. A source file is identified by its archive-relative path and SHA-256 hash.
3. Discord IDs are always stored as `TEXT`.
4. Raw Discord message content is never replaced by normalized content.
5. Character attribution never modifies raw author information.
6. LLM output is never authoritative historical evidence.
7. Every derived record must be traceable to source evidence.
8. Every interpretation must be distinguishable from source evidence.
9. A schema change must occur through a migration.
10. An import must be transactional.
11. Search indexes are disposable and rebuildable.
12. SQLite can be rebuilt from the appropriate source plus mapping and
    annotation data.
13. The GUI does not own archive business logic.
14. Failed imports must remain observable.
15. Deleting derived data must never delete raw evidence.

## Running Migrations

From the repository root, run:

```powershell
.\.venv\Scripts\python.exe -m archive migrate
```

The default database is `02_Processed/archive.sqlite3`. To migrate a different
existing database, pass its path:

```powershell
.\.venv\Scripts\python.exe -m archive migrate --database path\to\archive.sqlite3
```

The command fails if the requested database does not exist. It enables SQLite
foreign-key enforcement, reports the current and target schema versions, and
returns a non-zero exit code if a migration fails.

## Migration Mechanism

The command-line entry point is `archive/__main__.py`; the migration runner is
`archive/migrations/runner.py`. Numbered Python migration files are discovered
in order. Each migration may have a matching SQL file. Its ledger checksum is
calculated from the Python file and, when present, its SQL file.

The runner creates `schema_migrations` with `version`, `name`, `checksum`, and
`applied_at`, along with the specified name and checksum indexes. It checks
that recorded versions are contiguous, that every recorded migration still
exists, and that its name and checksum match either the current migration or an
explicitly recognized historical checksum. The compatibility list covers
known earlier checksums for migrations 001, 003-009, and 011; arbitrary checksum
changes are still rejected. Each pending migration runs in its own SQLite
transaction; the ledger row is written only after that migration succeeds. A
failed step is rolled back, and already completed steps remain recorded so the
command can be run again after resolving the failure. Running the command
against an up-to-date database is a no-op. There is no automatic downgrade
mechanism.

## Migration History

The current migration chain has twenty-two steps:

| Version | Migration | Effect |
| ---: | --- | --- |
| 001 | `create_sources` | Stages conflicting legacy tables under `legacy_*` names and creates `sources`. |
| 002 | `create_imports` | Creates the target `imports` table and, when legacy Discord imports exist, converts them and registers their source files. |
| 003 | `create_servers` | Creates `servers` and carries over server observations from legacy Discord imports. |
| 004 | `create_categories` | Creates `categories` and carries over known category observations. |
| 005 | `create_channels` | Creates `channels` and carries over known channel observations. |
| 006 | `create_threads` | Initially creates the thread table; the later schema refinement removes it. |
| 007 | `create_discord_identities` | Creates normalized Discord identity records. |
| 008 | `migrate_messages` | Creates the target message-observation table and, when present, maps legacy messages to their source, import, channel, and identity records. |
| 009 | `create_documents` | Converts legacy document metadata and extracted content into the target `documents` shape, with a source and import for each document. |
| 010 | `create_document_chunks` | Creates target chunks and copies existing chunk IDs and content. |
| 011 | `remove_threads` | Rebuilds `messages` without `thread_id` and removes `threads`, matching the first-slice schema's deliberate exclusion. |
| 012 | `group_legacy_source_captures` | Groups legacy Discord captures by source path and declared server/category/channel scope. |
| 013 | `create_document_entries` | Creates the typed `document_entries` table, preserves retained legacy entries, and groups legacy document captures by source path. |
| 014 | `create_attribution` | Creates character/player attribution records and immutable attribution audit events. |
| 015 | `create_scenes` | Creates arcs, scenes, scene-message membership, participants, and the derived scene-channel view. |
| 016 | `create_boundary_candidates` | Creates boundary detection runs, ordered input provenance, and scored transition candidates. |
| 017 | `create_candidate_spans` | Creates provisional segmentation runs, spans, memberships, reply links, and boundary decisions, separately from curated scenes. |
| 018 | `create_identity_candidates` | Creates provisional identity candidates, per-message evidence, and identity review history/overrides. |
| 019 | `create_attribution_confidence` | Creates attribution confidence snapshots, candidates, and evidence contributions. |
| 020 | `create_attribution_review` | Creates F2 review decisions, append-only review events, evidence overrides, and review input-change tracking. |
| 021 | `harden_attribution_review` | Adds F2 integrity and stale-review protections for durable human decisions. |
| 022 | `create_pipeline_runs` | Creates durable Phase G orchestration manifests with run status, scope/configuration, stage results, reports, warnings, and errors. |

When present, incompatible legacy `imports`, `messages`, `documents`,
`document_chunks`, `document_entries`, and the document FTS table are renamed
to `legacy_*` before target tables are created. Migration 013 copies retained
legacy entries into the target `document_entries` table; legacy FTS data remains
under `legacy_*` names and is not written by either importer. Migration 009
accepts TXT, PDF, and DOCX document formats and fails explicitly for an
unsupported legacy format rather than silently relabeling it.

## Migration of the Existing Archive

Before migration, the live database contained 114 documents, 9,030 document
chunks, and 4,080 document entries. It did not contain the legacy Discord
`imports` or `messages` tables. A SQLite backup was created and checked before
the live migration:

`05_Backups/archive.pre-migration-20260925-204736-206317.sqlite3`

The backup's `PRAGMA integrity_check` returned `ok`; it retained all 114
documents and 9,030 chunks.

The live database was migrated with the default command and, at the time of that
migration, reached schema version 11. The resulting record counts were:

| Table | Rows |
| --- | ---: |
| `schema_migrations` | 11 |
| `sources` | 114 |
| `imports` | 114 |
| `servers`, `categories`, `channels`, `discord_identities`, `messages` | 0 each |
| `documents` | 114 |
| `document_chunks` | 9,030 |
| `legacy_document_entries` | 4,080 |

The legacy document FTS table was retained as `legacy_documents_fts` and
remains searchable. The original document and chunk tables are retained as
`legacy_documents` and `legacy_document_chunks`; the legacy entries and FTS
auxiliary tables are retained under corresponding `legacy_*` names.

## Discord JSON Importer

`src/build_archive.py` is the Discord JSON ingestion entry point. It applies the
schema migrations before writing and records source provenance independently
from message observations:

1. Discover JSON exports and calculate each file's SHA-256 and byte size.
2. Read enough export metadata to identify the declared server/category/channel
    scope, then register an immutable `sources` capture. Its provisional logical
    key is derived from the source path and declared scope; registering the same
    bytes for that identity is idempotent.
3. Validate export metadata and declared message count.
4. Create a new `imports` run for every parse attempt, including retries of the
    same capture.
5. Normalize message fields and upsert normalized server, category, channel,
    and Discord identity records. Each message is inserted as an observation
    linked to both `source_id` and `import_id`.
6. Accept only a clean, successful run. Acceptance records `accepted_at` and
    links a newer capture to the previous accepted capture with
    `supersedes_source_id`.

Message uniqueness is scoped to `(import_id, discord_message_id)`. Reimporting
the same message ID with changed content in a later capture therefore preserves
both payload observations. Missing IDs, duplicate IDs within one capture,
invalid fields, or normalization warnings make the run partial and prevent it
from becoming current. Unexpected materialization failures roll back that
attempt's observations, finalize the run as failed, and retain its already
registered source capture. The raw JSON file is never modified.

The importer stores Discord identities and exported author fields only. It does
not assign character/player meanings, select observations for the Reader, or
create FTS indexes. Those are separate interpretation and read/search concerns.

Run the importer with an optional JSON file or directory; by default it scans
`01_Raw`:

```powershell
.\.venv\Scripts\python.exe src\build_archive.py [path] --database 02_Processed\archive.sqlite3
```

The default database writes `02_Processed/import-manifest.json`. When
`--database` points to a different database, the manifest is written beside it
with the database name and `-import-manifest.json` suffix. Existing manifest
results are retained by source path and hash; reimporting the same capture
refreshes its result instead of duplicating it.

## Document Importer

`src/document_parser.py` handles TXT, PDF, and DOCX files independently from the
Discord message importer. It applies the shared migrations and writes a typed
`documents` row linked to its source capture and import run. Extracted text is
kept in `documents`; derived `document_chunks` and detected
`document_entries` reference that document directly. Documents are never
inserted into `messages`.

Each invocation registers the source file by path and SHA-256, creates a fresh
import attempt, extracts and normalizes the document, then stores its typed
record and child rows. A clean extraction is accepted and may supersede the
prior capture at that path. Partial or failed extraction remains in provenance
but does not advance the accepted capture. `document_entries.speaker_raw`
preserves detected labels without assigning them to characters or players.
FTS creation and current-document selection are not part of the importer.

For documents, the logical source key is derived from the resolved file path and
empty declared scope; `source_kind` comes from the supported file format. Thus
identical bytes at the same path reuse the capture but still create a fresh
import run and document materialization. A parser exception or storage failure
leaves the source and failed import recorded without partial child rows. A
returned partial/error extraction can retain its typed document row for review,
but is not accepted as the current capture. A completed `no_text` extraction is
recorded as a successful run with its extraction status preserved.

The wrapper CLI scans recursively by default:

```powershell
.\.venv\Scripts\python.exe src\alt_archive_builder.py 01_Raw --db 02_Processed\archive.sqlite3
```

Every run is recorded, including reprocessing identical bytes; the legacy
`--reimport` option no longer controls duplicate suppression.

## Search and Reader Services

The application-facing read services accept an existing SQLite connection.
They keep query and message-context logic out of GUI code:

```python
from archive.reader_service import ReaderService
from archive.search_service import SearchService

search = SearchService(connection)
reader = ReaderService(connection)

results = search.search("silver compass", limit=50, offset=0)
context = reader.get_message_context(results[0]["message_observation_id"])
```

`SearchService.search(query, limit=50, offset=0)` performs a case-insensitive
literal substring search over message `content_search` and returns a list of
dictionaries. Each result includes the observation and Discord message IDs,
timestamp, original content and author fields, channel/category/server names,
source path, and any accepted character/player attribution. SQL wildcard
characters in the query are treated literally. Blank queries return no results;
invalid pagination values raise `ValueError`.

`ReaderService.get_message_context(message_id, before=10, after=10)` takes a
`message_observation_id` and returns `{"message": ..., "before": [...],
"after": [...]}`. Neighboring messages are limited to the same channel and
ordered chronologically. An observation outside the current accepted captures
or an unknown observation returns `None`.

Both services select observations from the latest accepted capture of each
logical source by default. `search()` performs literal substring matching;
`search_relevant()` ranks Discord messages by matching up to ten distinct
question terms. Search currently covers Discord message text only; it does not
yet parse the modeled boolean/field-filter query language, search documents, or
use FTS. Focused coverage is in
`tests/test_search_reader_services.py`.

### Question-Aware Ask Retrieval

The Ask service classifies each question with local text heuristics before
retrieval. The categories are extraction, normal QA, chronology, synthesis,
research, and attribution. This classification is deterministic and does not
make a model call.

Extraction, normal QA, and attribution use narrow retrieval: rank relevant
messages, choose the channel with the strongest cluster of matches, and expand
the best match with one preceding and up to five following messages. Search
results include accepted character/player attribution metadata when available.
Chronology, synthesis, and research retrieve a larger candidate pool, bounded
to 32-256 messages (eight times the requested limit within those bounds). They
select the strongest hit per channel and calendar date, spread selected hits
across the available timeline when necessary, preserve each selected hit as an
event anchor, and return the evidence in chronological order. Surrounding
messages are included as context and duplicate observations are removed.

Channel/date groups are only event proxies; they are not confirmed scene
boundaries. Adjacent messages establish that a message is earlier or later than
the retrieved match, not that the event in the question has begun or ended.
Automatic retrieval does not yet use curated `scene_messages` to identify event
boundaries or the first message after an event. The system prompt therefore
instructs the model not to equate proximity with event completion, to state
when evidence does not establish an ending, and to require multiple temporally
distinct states before describing change over time. An uncertain sequence must
be reported as uncertain rather than inferred from adjacency.

The Ollama `num_predict` output budget is selected by question type:

| Question type | Output-token budget |
| --- | ---: |
| Extraction | 384 |
| Attribution | 512 |
| Normal QA | 768 |
| Chronology | 1,024 |
| Synthesis | 1,024 |
| Research | 1,536 (3,072 for GPT-OSS) |

The budget controls the maximum generated output, not the prompt or context
window. Requests to `gpt-oss` models set `num_ctx` to 8,192; other models use
their Ollama-configured context default and retain the 1,536-token research
budget. To reserve space for the larger GPT-OSS answer, its serialized evidence
payload is capped at 12,000 characters; other models are capped at 6,000
characters. Each message's content is limited to 1,200 characters. Explicit
requests for all arguments, debates, or disputes retrieve up to 32 matching
anchors, balanced across channel/day groups, before adding nearby context. The
model is asked to group those positions by channel/day and not claim archive-wide
completeness unless the evidence supports it. Thinking remains disabled and
temperature is `0.2`. If no evidence is retrieved, the model is not called. The
model is instructed to cite evidence references, but citations are not
independently validated. Retrieval and classification tests are in
`tests/test_search_reader_services.py` and `tests/test_ollama_service.py`.

## Desktop GUI and Startup Synchronization

Open the desktop application from the repository root:

```powershell
.\.venv\Scripts\python.exe -m archive gui
```

The default database is `02_Processed/archive.sqlite3` and the default raw
archive is `01_Raw`. Override either path when needed:

```powershell
.\.venv\Scripts\python.exe -m archive gui --database path\to\archive.sqlite3 --raw-directory path\to\raw
```

At launch, the application opens a progress window, runs pending schema
migrations, scans the raw directory recursively for JSON, TXT, PDF, and DOCX
files, and compares each file's resolved path and SHA-256 hash with import
history. Previously recorded unchanged captures are skipped. New or changed
captures are sent to the existing Discord or document importer; raw files are
not modified. Per-file failures and warnings are retained and shown on Home.

After imports, startup validates the database with `PRAGMA quick_check` and
`PRAGMA foreign_key_check`. A validation failure stops launch and displays an
error. If imports occurred, the application runs SQLite `REINDEX` and
`PRAGMA optimize`; then Home opens with database health, schema version,
source/message counts, and the sync summary. With no imports, index rebuilding
is skipped. The current schema has no FTS virtual table, so this rebuilds only
indexes that already exist; FTS remains deferred.

Home, Search, Reader, and Ask are the current GUI surfaces. Search uses
`SearchService`; double-clicking a result opens its surrounding channel context
through `ReaderService`. Ask uses the question-aware retrieval and answer
generation pipeline described above, then sends retrieved messages and source
metadata to the local Ollama chat API. The Answer view displays the response
and retrieved evidence. The LLM Diagnostics view shows retrieval results, the
exact user-message context and system instructions sent to Ollama, the selected
model, request settings, and the answer. The context includes the question and
JSON evidence payload, limited to 12,000 serialized characters for GPT-OSS or
6,000 for other models, with a maximum of 1,200 characters per message.
**Export test** saves the diagnostics and answer as a `.txt` file and becomes
available after an answer is generated.

The GUI theme selector supports **System**, **Light**, and **Dark** modes. System
mode follows the OS appearance preference where it can be detected and checks
for changes while the app is open. The selected mode is saved in the user's
application settings directory (`%APPDATA%\Discord RP Archive` on Windows,
`~/Library/Application Support/Discord RP Archive` on macOS, or
`$XDG_CONFIG_HOME/discord-rp-archive` on Linux, defaulting to `~/.config`).
If the system preference cannot be detected, System mode uses the light theme.
Retrieval is keyword
based, not semantic or document retrieval.

Ask questions may include a Pacific time window in the form
`9/18/2025 from 1:31 AM to 3:02 PM` (or an ISO date). The retrieval layer
constrains candidate messages and surrounding context to that interval; the
end minute is inclusive. The times are interpreted as `America/Los_Angeles`
and converted to UTC for querying, including daylight-saving time. Archive
message timestamps are stored and displayed as UTC. Retrieval also searches
author and accepted character/player names, normalizing compatibility Unicode
used in stylized Discord names.

Ollama Q&A uses the local endpoint `http://127.0.0.1:11434` by default and adds
no Python package dependencies. Ensure Ollama is running and a model is
available (`ollama list`), open the Ask tab, select **Refresh models**, choose a
model, and submit a question. `OLLAMA_HOST` overrides the endpoint and
`OLLAMA_MODEL` prefills the model field. Retrieved message text is sent to the
configured Ollama endpoint, so keep `OLLAMA_HOST` local when local-only
processing is required. PyInstaller packaging remains a later step, after the
application behavior is stable.

## Verification

Run the full test suite, including importer, migration, search, and database
integrity tests, with:

```powershell
\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`tests/test_integrity.py` imports a one-channel Discord export and verifies the
raw file remains unchanged, large Discord IDs remain text, original message
content and source provenance are preserved, and foreign keys remain valid. It
also verifies failed materialization leaves no partial derived rows and that
SQLite rejects a message observation pairing a capture with another capture's
import run.
`tests/test_discord_importer.py` imports two versions of one export containing
the same Discord message ID with different content, then confirms both
observations remain and a malformed later capture is not accepted.
`tests/test_document_importer.py` verifies capture supersession, typed document
chunks and entries, and retention of failed extraction provenance without
writing documents into `messages`.
`tests/test_migrations.py` checks preservation of legacy Discord/document rows,
foreign keys, repeatable migrations, legacy capture grouping, acceptance of
recognized historical checksums, and rejection of unrecognized checksum
changes. The full test suite completed all 27 tests successfully on
2026-09-29.

Search-index rebuilding is not yet covered: FTS indexing remains deferred and
neither importer creates or updates an FTS index.

### Previously Acknowledged Server Pilot

After the one-channel integrity test passed, the updated JSON corpus was
preflighted against `00_Admin/server-map (The Previously Acknowledged).yaml`
and imported into the isolated
`02_Processed/Pilots/archive-pilot-previously-acknowledged.sqlite3` database. All 46
JSON exports covered one server and 46 channels, with 4,502 messages; all
imports succeeded. The pilot reached schema version 13, `PRAGMA integrity_check`
returned `ok`, `PRAGMA foreign_key_check` returned no violations, all 4,502
messages had matching source/import provenance, and the JSON source hashes were
unchanged.

The 46 HTML exports have the same channel-ID set as the JSON exports but were
not imported. The Discord importer consumes JSON, and the document importer does
not support HTML. The HTML source files remain in `01_Raw` unchanged.

### Official Server Corpus Imports

The three exported server JSON corpora were imported independently into
`02_Processed/Official`, using their corresponding server maps:

| Corpus | JSON exports / channels | Messages | Database |
| --- | ---: | ---: | --- |
| Previously Acknowledged | 46 | 4,502 | `previously-acknowledged.sqlite3` |
| The Acknowledgment Cove | 128 | 17,503 | `acknowledgment-cove.sqlite3` |
| The Acknowledgment Server (Original) | 405 | 147,026 | `acknowledgment-original.sqlite3` |

Every export succeeded with no importer warnings. Each database passed
`PRAGMA integrity_check` and `PRAGMA foreign_key_check`, and every recorded
source hash matches its unchanged JSON export. Per-corpus import manifests are
stored beside the databases. HTML exports remain in `01_Raw` and were not
imported; the Discord importer consumes JSON.

The actual archive was also migrated on a temporary copy before the live
database was changed. After the live migration, `PRAGMA integrity_check`
returned `ok`, `PRAGMA foreign_key_check` returned no violations, all listed
document and chunk counts were preserved, and the migration ledger contained
versions 1 through 11 at that point. The verification then covered migrations
through version 15; later migration coverage is recorded below and in the
current test suite.

## Current Limitations

The database counts and schema versions documented in "Migration of the
Existing Archive" and the earlier verification section are historical
observations from the migration work, not a claim that the live archive was
processed by Phase G. Phase G was tested against controlled in-memory test
databases; it was not run against the live archive as part of that phase.
Before an operational run, check the target database's migration status and
make a backup according to the archive's normal operational procedure.

The migration code can convert legacy Discord rows when those tables exist;
its regression fixtures cover that path. The live archive was document-only
when the historical migration counts below were captured.

Both `src/build_archive.py` and `src/document_parser.py` use shared
`sources`/`imports` provenance with typed message or document records. Document
chunks and detected entries remain children of `documents`; neither is stored
in `messages`. Neither importer builds FTS.

The live database was at schema version 11 when the startup checksum issue was
investigated. Its ledger contains recognized historical checksums for
migrations 001, 003–009, and 011. The runner now accepts those exact values while
continuing to reject unknown checksum changes. At that earlier verification, a
temporary copy upgraded to schema version 15 with `PRAGMA quick_check` returning
`ok` and no foreign-key violations. That check predates migrations 016–022 and
does not establish the current schema version of any live database.

Migration 011 uses SQLite's `ALTER TABLE ... RENAME TO` syntax rather than SQL
Server's `EXEC sp_rename`; the SQL Server statement prevented fresh SQLite
databases from applying the migration. The migration tests pass with the
SQLite statement. Migration `.sql` files are SQLite-specific, so a SQL Server
language-service warning about `RENAME` is not a SQLite runtime error. The
isolated pilot was created after that correction. Historical migration
checksums are handled by the explicit compatibility list described above.
FTS indexes and normalized thread membership remain deferred. Thread evidence
can be extracted only from metadata actually preserved in a message's source
payload. Scene reconstruction beyond deterministic provisional spans,
overlapping interactions, and cross-channel scene continuity remain future
work; no candidate span is implicitly promoted to a curated scene.

The Phase G regression and targeted migration/scope tests passed during
implementation. The full Python unit-test suite then passed with 145 tests.
The Phase G tests cover stage reuse and invalidation, changed accepted inputs,
dry-run behavior, failed-stage recovery, F2 decision/audit preservation, and
curated-scene preservation. This result verifies the tested code paths and
fixtures, not a live-archive pipeline execution.
