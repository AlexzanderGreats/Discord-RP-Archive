# Discord RP Archive: Implemented Architecture

This document describes the behavior currently implemented in the project and
the target model that the implementation is converging toward. Current-state
sections are labeled by the code path they describe; target-model sections do
not imply that every table is implemented yet.

The project currently has two ingestion paths:

1. `src/build_archive.py` imports DiscordChatExporter-style JSON exports.
2. `src/document_parser.py` imports TXT, PDF, and DOCX documents. The command-line
   wrapper for this path is `src/alt_archive_builder.py`.

Both paths write to SQLite. They currently use separate table families, but the
target model keeps their content strongly typed and connects them through shared
provenance and relationship tables.

## Target Strongly Typed Content Model

The canonical content records are:

```text
messages
documents
document_chunks
document_entries
```

They remain separate because Discord messages, imported documents, document
segmentation, and detected speaker entries have different source structures and
lifecycles. They are unified through shared relationships rather than a generic
content table.

The shared relationship layer is:

```text
sources
imports
entities
links
attributions
scene_messages
document_tags
message_tags
...
```

At a conceptual level:

```text
sources ──< imports ──< messages
               └── documents ──< document_chunks
                          └── document_entries

messages / documents / chunks / entries
   ├── links
   ├── attributions
   └── typed tags and scene membership
```

`sources` identifies immutable files or artifacts. `imports` records the
ingestion event and parser version that produced derived records. `entities`
is a domain boundary containing explicit tables for characters, players,
locations, arcs, scenes, and other concepts with distinct semantics. `links` is
reserved for content-to-content relationships such as replies, references,
quotes, and citations. `attributions`, `scene_messages`, `scene_participants`,
`document_tags`, and `message_tags` are explicit editorial junction tables for
relationships that are not content-to-content links.

The distinction is intentional:

```text
CONTENT RELATIONSHIPS
   message -> message
   document -> document
   message -> document

EDITORIAL RELATIONSHIPS
   message -> character/player   attributions
   message -> scene               scene_messages
   scene -> arc/location          explicit foreign keys
   scene -> character/player      scene_participants
```

Do not make SQLite behave like a graph database by placing every relationship
in one generic link table. Use ordinary foreign keys and explicit junction
tables when the relationship has different semantics or review rules.

The target tables are introduced incrementally, with the existing `imports`,
`messages`, `documents`, `document_chunks`, and `document_entries` tables
serving as the initial migration surface.

## Signal Extraction (Implemented)

`archive/signals.py` extracts deterministic evidence from chronologically
ordered message-observation mappings. It is a pure analysis module: it does not
read or write SQLite, alter scene curation or attribution, assign scores, or
decide whether a boundary should be accepted. Each result identifies its
signal type, source observation IDs, observed value, evidence, and whether the
evidence is explicit or heuristic.

Supported observations include normalized timestamp gaps, timestamp
availability/validity, channel and raw-author changes, reply relationships,
source/import collection changes, exact scene-control marker lines
(`[[SCENE START]]`, `[[SCENE END]]`, `[[SCENE BREAK]]`, `[[TIME SKIP]]`,
`[[TRANSITION]]`), header bracketed speaker labels, line-start `OOC:` or
`(OOC)` markers, and labeled `Location:`, `Setting:`, or `Time:` lines.
The importer does not enforce a shared marker convention; these exact tokens
are recognized as text patterns, not as verified archive-wide semantics.
Speaker labels are not resolved to characters, and narrative labels are not
treated as scene boundaries. Contradictory evidence is returned separately
rather than suppressed.

Signal extraction cannot currently establish natural-language location or
action transitions, character participation/composition, Discord thread
membership, arbitrary message references outside the normalized reply field,
or reply targets absent from the supplied observations. The
messages schema has no `thread_id`; `thread_name` is not persisted as a
column. A thread change is emitted only when per-message `source_json`
explicitly includes `thread_id`/`threadId` or `thread_name`/`threadName`.
Channel IDs available on message observations are local normalized foreign
keys, not original channel IDs. Collection changes are reported only when
`source_id` or `import_id` is included in the supplied records.

## Boundary Candidates (Implemented)

`archive/boundaries.py` consumes Phase B signals and scores each adjacent pair
in an explicitly ordered observation sequence. It rejects backwards known
timestamps and requires ties to be ordered by observation ID; callers must
scope and order the input deliberately (for example, by one channel capture).
It does not sort records, create scenes, or assign message membership.

The initial `event_boundaries_v1` ruleset classifies `scene_start` before its
message and `scene_end` after its message as hard boundaries. `scene_break`,
`time_skip`, and `transition` markers are hard before the marked message.
A source-capture (`source_id`) change is also hard; an import-run (`import_id`)
change alone is not, since re-importing one capture is not itself an event
boundary. Hard status takes precedence over the numeric classification while
supporting and contradictory evidence still contributes to the recorded raw
score.

Soft weights are: timestamp gap 0.25, channel change 0.20, narrative context
transition 0.25, author change 0.10, speaker-label change 0.10, thread change
0.15, and a resolved direct reply across the pair -0.30. Hard marker and source
capture change weights are 1.0. Reply evidence is contradictory; positive
contributions are supporting evidence. Every contribution records strength,
weight, value, source observations, classification, and evidence. Raw score
is `sum(weight * normalized_strength)` and is an uncalibrated operational
score, not a probability.

Timestamp strength is `min(max(gap_seconds, 0) / 4443.177, 1)`. The reference
is the observed 95th percentile gap (4,443.177 seconds) among 170,847
non-negative adjacent timestamp gaps in current accepted channel captures;
the archive sample had 171,307 messages across 460 channels. Initial
classification thresholds are 0.35 for `soft_candidate` and 0.65 for
`strong_candidate`; hard evidence yields `hard_candidate` regardless of
counterevidence. These weights and thresholds are initial engineering values,
not empirically calibrated.

The read/write entry point `archive/boundary_persistence.py` stores one
versioned run and only non-`none` candidates. Migration 016 adds
`boundary_detection_runs`, `boundary_detection_run_messages`, and
`event_boundary_candidates`, separate from curated `scenes` and
`scene_messages`. Candidate rows retain supporting,
contradictory, and full contribution JSON and start with `pending` review
status; this phase adds no review workflow. The run records canonical config,
config hash, supplied input scope, input fingerprint, timestamps, status, and
summary counts. `boundary_detection_run_messages` retains every ordered input
observation, including transitions classified as `none`. Candidate persistence
is protected by a savepoint, and
analysis errors are recorded as failed runs and re-raised.

Archive observations relevant to the initial rules: 11,442 current accepted
messages (6.68%) contain a reply reference; the recognized explicit scene
marker patterns occurred zero times; two messages began with bracketed
speaker-like labels; leading OOC markers occurred zero times. Adjacent
author-identity changes within the 460 channel captures occurred 56,917 times.
One labeled narrative-context marker was present, with no differing labeled
context transitions.
There were no speaker-marker changes or differing labeled-context transitions.
The timestamp-gap percentiles were p25 11.266 s, p50
38.868 s, p75 138.427 s, p90 708.495 s, p95 4,443.177 s, and p99
345,047.941 s. A global timestamp ordering yielded 25,456 channel changes
across 171,306 adjacent pairs, but those pairs cross independent channel
captures; channel-change weights should therefore be considered only within a
caller-defined interaction scope.

## Candidate Segmentation (Implemented)

`archive/segmentation.py` generates deterministic draft spans within exactly
one `(server_id, channel_id, source_id)` scope. Messages missing any scope
field are retained in singleton spans rather than combined under an unknown
scope. Messages sort within scope by timestamp then observation ID; rows
without timestamps sort afterward by observation ID.
`archive/segmentation_persistence.py` loads current accepted source captures
and only the latest accepted import for each capture, with optional
server/channel/source filters.

Segmentation does not equate a Phase C `strong_candidate` with a split. A hard
Phase C boundary splits deterministically; otherwise a gap at least
`inactivity_gap_seconds` (defaulting to the measured Phase C p95 of 4,443.177
seconds) starts a new draft span unless the incoming message directly replies
to a prior in-scope member. Reply evidence cannot negate a hard boundary.
Cross-scope replies are persisted as links only and never combine
memberships. Every scoped adjacent pair records its split/no-split decision
and any Phase C evidence supplied for that transition.

Migration 017 stores segmentation runs, `candidate_spans`,
`candidate_span_messages`, `candidate_reply_links`, and
`segmentation_boundary_decisions`. Automatic assignments have `candidate`
status; none of these records creates or edits `scenes` or `scene_messages`.
The initial segmenter creates a non-overlapping time partition per scope. It
cannot infer parallel overlapping scenes from current evidence; uncertain
participants or interactions remain unassigned.

## Scene Curation (Implemented)

Scenes and arcs are curated in `scenes.py`. `scene_messages` is the explicit,
ordered set of message observations assigned to a scene; timestamps do not
determine membership. The `scene_channels` view reports the distinct Discord
channels represented by those selected messages. Message reads expose their
stored Discord author and channel alongside any accepted character/player
attribution. Scene participants are separately curated in `scene_participants`.

Fictional time is optional scene metadata and is independent of Discord time.
Unreviewed attribution proposals are not treated as accepted character/player
identity when reading scene messages.

## Identity Candidate Inference (Implemented)

The existing `message_attributions` workflow remains the reviewable claim
system for registered identities: exact registered character aliases and
time-valid Discord-identity/player links create pending claims, never accepted
claims. Phase E adds `archive/identity_inference.py` and
`archive/identity_candidate_persistence.py` as a separate evidence layer.

The new layer consumes Phase B header speaker-marker signals. An exact
normalized alias match is recorded against its registered character ID;
multiple alias matches remain an explicit ambiguous evidence record. A label
with no registered alias match becomes a provisional candidate, grouped by a
stable key derived from its case-folded, whitespace-normalized label within the
known server. Missing server scope yields separate candidates per observation
instead of merging across unknown contexts. Each observation retains the exact
captured label and source line. Author identity is contextual evidence only;
a speaker label never implies a player.

Provisional candidates are not inserted into `characters`, `players`, or
`message_attributions`, and have no confidence score or automatic acceptance.
Migration 018 stores inference runs and per-message evidence independently
from curated attribution and scene data. Reviewer actions are append-only;
message-level split, reassignment, and exclusion overrides remain effective
across subsequent detection runs. A reviewer can register a new character
through the existing character API, then explicitly link the provisional
candidate to that character. Candidates link to messages rather than spans, so
reconstruction can later join identity evidence to whichever candidate
memberships it reviews without making identity inference depend on
segmentation.

## Attribution Confidence (Implemented)

`archive/attribution_confidence.py` aggregates persisted Phase E
`identity_candidate_evidence` into per-message character candidates and
alternatives. The default ruleset is `attribution_confidence_v1`. Scores are
integer evidence units, not probabilities: each distinct matching
message-level speaker-label observation contributes one point. Same-label
evidence is combined only within a known server. An existing reviewed
rejection or a conflicting accepted attribution contributes a one-point
contradiction penalty. A provisional identity linked to a registered
character contributes human-reviewed evidence but remains `review_required`;
it does not become a message attribution.

Three evidence units and at least a two-point lead are required for
`high_confidence`. Multiple plausible character matches remain `ambiguous`;
unmatched provisional labels and messages without usable evidence remain
`unresolved`; a current human decision that conflicts with new evidence is
`conflicted`. `review_required`, `ambiguous`, `unresolved`, and `conflicted`
are mandatory-review states. `high_confidence` is only an operational
triage class and never auto-accepts a claim. Existing pending proposals are
retained as zero-weight context; accepted/rejected decisions and their audit
events remain authoritative in `message_attributions` and
`attribution_events`.

Migration 019 stores confidence snapshots and per-message/candidate
supporting, contradictory, and contextual contributions. Each contribution
retains message, Phase E run/evidence, and existing attribution/review-event
provenance. Runs record canonical configuration, input scope, fingerprint,
status, timestamps, and statistics. No player score is calculated: speaker
labels, raw Discord identity, character mentions, natural-language prose, and
boundary scores do not establish player attribution or character
participation.

The thresholds are conservative initial triage settings, not calibrated
against reviewed archive examples. In the read-only archive inspection,
171,711 current messages yielded two provisional speaker-marker observations,
neither repeated; there were no registered characters or aliases, attribution
claims or audit events, Discord-player identity links, or player-character
mappings. The archive therefore supplies no reviewed examples to calibrate
character confidence scores.

## Attribution Review Workflow (Implemented)

`archive/attribution_review.py` supplies a GUI-neutral read model and action
API over completed Phase F1 snapshots. The review queue prioritizes
high/normal/low work, includes current candidates, their evidence, and current
authoritative attributions, and distinguishes `review_required`,
`already_resolved`, `unresolved`, `conflicted`, `excluded`, and stale results.
A decision is not hidden when newer recommendations conflict with it.
Snapshots are marked stale after newer inference/confidence results,
alias/mapping changes, relevant human review, or newly imported messages in
scope. Actions against stale snapshots are rejected, except that an explicit
restoration action can reverse the current evidence exclusion.

Confirm and assign-character actions create or accept an audited manual
attribution. Reject records a rejected manual claim; if that character is
already accepted, F2 records a conflict without silently revoking it.
Leave-unresolved is an explicit durable review state. Linking a provisional
identity updates only the identity mapping and does not create a message
attribution. Split, reassign, and merge operations reuse Phase E's immutable
identity-review events and persistent per-message evidence overrides.
Exclusion is a separate reversible F2 message-level override, applicable to
registered or provisional marker evidence; raw observations remain untouched.

F2 records each action in append-only `attribution_review_events`, with
reviewer, timestamp, optional note, source F1 candidate/evidence fingerprint,
prior-state snapshot, and linked authoritative attribution or identity-review
event. The mutable
`attribution_review_decisions` table is a current-state projection; it does
not replace audit history. Bulk attribution review is allowed only when every
selected message has the same candidate and matching evidence fingerprint.
Each message still receives its own audit event. F1 recomputation does not
update F2 decisions; newly produced snapshots remain separate and expose the
preserved human decision state.

## Pipeline Overview

The implemented flow can be represented as:

```text
RAW FILE
   |
   v
DISCOVERY
   |
   v
VALIDATION
   |
   v
PARSING
   |
   v
NORMALIZATION
   |
   v
DATABASE
   |
   v
FTS INDEX
```

The Discord importer registers the byte-distinct capture, validates the export,
creates an import run, then records normalized observations. Only a complete,
clean run is accepted; partial or failed runs do not advance the current capture.
The document importer parses a file first, then performs classification,
chunking, entry detection, and database insertion inside a transaction.

## Ollama Q&A (Initial Slice)

The Ask view retrieves up to eight current accepted Discord messages by
question-term overlap, then sends those records and their source metadata to
Ollama's local `/api/chat` endpoint. The model is prompted to answer only from
the supplied evidence and cite references such as `[E1]`. Retrieved records
remain visible alongside the answer, and no model request is made when there is
no matching evidence. This first slice does not search document chunks, use
semantic embeddings, or validate citations produced by the model. LLM output
remains an interpretation, not authoritative archive evidence.

## `build_archive.py`: Discord JSON Pipeline

### Where a file enters the pipeline

The entry point is `main()` in `src/build_archive.py`.

The optional positional argument is a JSON file or directory. If it is omitted,
the code uses the module constant `RAW_ROOT`, which is defined as
`01_Raw` in the current source. `find_json_exports()` then:

- accepts one `.json` file, or
- recursively finds `*.json` under a directory;
- rejects a non-JSON file;
- raises an error if the path does not exist.

The command-line options are:

- `--database`: SQLite output path;
- `--server-map`: optional text/YAML-like server map used for ID warnings.

The current repository uses `01_Raw` and `02_Processed` with capital letters.
The importers use those same directory names as their defaults.

### Discovery to parsed JSON

`import_file()` is called once per discovered JSON file.

1. `sha256_file()` reads the file in 1 MiB blocks and calculates a SHA-256 hash.
2. `load_json()` opens the file as UTF-8 with BOM support and parses JSON.
3. `get_message_list()` accepts either:
   - a top-level object with a `messages` list, or
   - a top-level JSON list.
4. Each message must be a JSON object. A non-object message causes the file to
   fail.
5. `get_metadata()` extracts server, category, and channel metadata from the
   export where available.

The importer does not classify Discord files into document types. The JSON file
is treated as a message export, and its server/category/channel metadata is
stored with the import and each normalized message.

### Validation

`validate_server_map()` optionally compares the extracted category and channel
IDs with numeric IDs found in the supplied server-map file. A mismatch prints a
warning; it does not reject or alter the source metadata.

The `messageCount` field, when present, must be a non-negative integer.

`normalize_message()` also performs validation while converting each source
message:

- IDs are converted to strings;
- timestamps are converted to UTC ISO-8601 plus Unix time;
- booleans are restricted to Boolean or SQLite-compatible `0`/`1` values;
- attachment, embed, sticker, reaction, mention, and emoji fields are serialized
  as JSON strings;
- invalid timestamps or invalid boolean values raise errors.

`validate_messages()` produces a `ValidationReport` for missing IDs, duplicate
IDs within the source, timestamps, content, metadata signals, and embedded JSON.
The importer records clean runs as succeeded and accepts them. Validation or
normalization issues produce a partial, unaccepted run; processing failures are
recorded as failed runs.

### Normalization

The normalized representation is the `NormalizedMessage` dataclass. It contains:

- message and message-type identifiers;
- original, UTC, Unix, and edited timestamps;
- server/category/channel/thread fields;
- author and webhook fields;
- original content and lower-case whitespace-normalized search content;
- reply ID;
- pinned/call metadata;
- serialized attachments, embeds, stickers, reactions, mentions, and emojis;
- source path, source SHA-256, and compact original message JSON.

Character and player fields exist in the `messages` table but are inserted as
`NULL` by this stage. The source code explicitly leaves character/player
attribution for a later stage.

### Database insertion

`create_database()` applies the versioned migrations. `import_file()` hashes and
registers each capture using its path and declared export scope, validates the
export, creates a distinct import run, normalizes messages, and writes one
observation per Discord message ID in that run. The composite import/message
key permits the same Discord ID to retain different payloads across captures.
Only a clean successful run is accepted; incomplete observations remain
historical but cannot replace the current accepted capture.

### FTS (deferred)

The Discord importer does not create, update, or rebuild an FTS index. Search
indexing and selection of the current observation belong to a later reader/search
slice.

### Failure behavior

At the top level, `main()` catches exceptions separately for each JSON file,
prints an error, and continues with the next file. A failed file is omitted from
the successful `results` list in the manifest. The process still returns `0`
after processing the remaining files. Therefore, callers cannot rely on the
exit code alone to detect partial failure.

Capture registration and import-run creation are committed before message
materialization. If materialization fails, its observation/dimension changes are
rolled back to a savepoint and the run is finalized as failed; the source capture
and failed-run provenance remain. The connection is closed in `finally`.

If no JSON files are discovered, `main()` prints a message and returns `1`
before opening the database for import.

## `document_parser.py`: TXT/PDF/DOCX Pipeline

### Discovery

`discover_files()` receives an input directory and returns sorted files with
extensions in `SUPPORTED_EXTENSIONS`:

- `.txt` -> `txt`;
- `.pdf` -> `pdf`;
- `.docx` -> `docx`.

Recursive discovery uses `rglob("*")`; non-recursive discovery uses `iterdir()`.
The current CLIs call it with `args.recursive or True`, which always evaluates to
`True`. In practice, document scans are currently always recursive even when
`--recursive` is omitted.

`alt_archive_builder.py` is the simpler wrapper and defaults its input to the
repository's detected raw directory. `document_parser.py` has its own CLI and
requires the input directory as a positional argument.

### Format-specific parsing

`parse_supported_file()` dispatches by extension.

#### TXT

`parse_txt()`:

- reads the complete file as bytes;
- calculates SHA-256 from those bytes;
- decodes UTF-8 with BOM support;
- falls back to UTF-16 if UTF-8 decoding fails;
- stores the full text;
- calculates paragraph, character, and word counts;
- creates normalized search text;
- marks empty content as `no_text` and adds a warning.

#### PDF

`parse_pdf()` requires PyMuPDF. It opens every page and extracts text with
`page.get_text("text")`. It stores:

- the concatenated full text;
- one page object per page in `pages_json`;
- PDF metadata;
- page, character, and word counts;
- normalized search text;
- source creation/modification times when parseable.

A scanned or image-only PDF is still a document, but receives `no_text` and the
warning that OCR is required.

#### DOCX

`parse_docx()` requires `python-docx`. It extracts non-empty paragraphs and
tables. Paragraph styles beginning with `heading` are recorded as heading
blocks, and table rows are joined with ` | `.

It stores the combined paragraph/table text, structured blocks, core properties,
counts, and normalized search text. Empty extracted content receives `no_text`.

### Title and document classification

`detect_title()` chooses, in order:

1. an explicit metadata title;
2. a suitable short heading-like line from the content;
3. the filename stem.

`classify_document()` first honors an explicit valid type. Otherwise it searches
the title and first 2,000 characters for keywords. Possible results include
`character_sheet`, `lore`, `worldbuilding`, `story`, `rp_transcript`,
`powerscaling`, `analysis`, `reference`, and `unknown`.

This is heuristic classification, not semantic analysis. A metadata YAML file
can override title, type, status, or source format. Overrides are matched against
relative path, filename, and absolute/path forms.

### What is a document?

and provenance.
A `documents` row is one typed materialization of one source capture by one
import run. The capture hash and path belong to `sources`; every parse attempt
gets a separate `imports` row. Recaptures at the same path retain earlier
documents, chunks, and entries while advancing the accepted-capture chain only
after a successful extraction.

### How chunks are created

`build_chunk_records()` receives the document's complete extracted text.

1. `split_paragraphs()` splits on blank-line boundaries.
2. Each paragraph becomes one chunk if it is no longer than 1,200 characters.
3. Longer paragraphs are split by whitespace while keeping each piece at or
   below approximately 1,200 characters.
4. Each chunk receives a zero-based `chunk_index`, text, normalized search text,
   and a basic source-location marker identifying it as paragraph-derived.

Chunks are storage/search units. They are not necessarily Discord messages,
paragraphs, or roleplay turns. They exist to make long documents easier to
retrieve and process while preserving the complete document text separately.

### How entries are detected

`detect_rp_entries()` scans the document line by line. It recognizes speaker
markers in three forms:

```text
[Speaker]
Speaker:
Speaker - text
Speaker — text
```

The first two forms start an entry and collect following lines until another
speaker marker. The dash form starts an entry immediately with its body and is
only accepted when the whole line is shorter than 140 characters.

An entry is written only when it has a recognized speaker and non-empty content.
Entries store the raw speaker label, content, zero-based `entry_index`, and an
approximate line location. `character_id` and `player_id` are currently `NULL`.

### Why 9,030 chunks but only 4,080 entries?

They represent different detections with different rules:

- the 9,030 chunks come from splitting all extracted document text by paragraph
  and length;
- the 4,080 entries come only from text regions that match the speaker-marker
  patterns.

Therefore a document can contribute many chunks without contributing any
entries, and one detected entry can span multiple chunks. Chunks are generic
document segmentation; entries are roleplay-speaker segmentation. There is no
one-to-one relationship between them, and the current schema does not link a
chunk directly to an entry.

### Document database transaction

`import_document()` performs the following sequence:

1. verify the source exists and has a supported extension;
2. calculate its SHA-256;
3. return an existing record for a duplicate hash unless reimport is enabled;
4. parse the file;
5. detect title and classify the document;
6. apply metadata overrides;
7. insert the document row;
8. generate and insert chunks;
9. detect and insert entries;
10. insert the document's FTS row;
11. commit the transaction.

If steps 7 through 10 fail, the transaction is rolled back and the caller
receives an error. Format extraction failures are converted into an
`ExtractionResult`-like import result with `extraction_status="error"`, allowing
the CLI to continue to later files.

The current dry-run path still opens the database and calls
`create_document_tables()` before parsing, so it can create schema objects even
though it does not insert document records.

## SQLite Schema

### Discord table family

#### `imports`

One row per successfully recorded source JSON hash. It is import-level metadata:

- source path and SHA-256;
- server/category/channel metadata;
- import timestamp;
- source and imported message counts;
- oldest and newest timestamps.

`source_sha256` is unique, and `INSERT OR IGNORE` prevents a second import row
for the same file hash.

#### `messages`

The Discord message observation table. It stores one row per
`(import_id, discord_message_id)` and ties each payload to its capture and import
run with foreign keys. The same ID can therefore have distinct observations in
different captures.

#### `messages_fts`

Deferred. The target first-slice schema and importer do not create this table.

### Document table family

#### `documents`

The source-of-truth record for each imported TXT, PDF, or DOCX file. It stores
the complete extracted content plus provenance, metadata, classification,
status, counts, extraction state, warnings, and format-specific JSON.

#### `document_chunks`

Derived segmentation rows belonging to one document. Each row has a foreign key
to `documents(document_id)` with `ON DELETE CASCADE`.

#### `document_entries`

Derived roleplay-speaker detections belonging to one document. Each row also has
a foreign key to `documents(document_id)` with `ON DELETE CASCADE`.

#### `documents_fts`

Deferred. The document importer does not create or populate this table.

### Supporting SQLite objects

Legacy FTS5 tables may remain in migrated databases under `legacy_*` names;
they are not written by the current document importer.

### Current typed provenance relationships

```text
sources 1 ──< imports 1 ──< messages
   │             │
   │             └──────── documents 1 ──< document_chunks
   │                                  └───< document_entries
   └── supersedes_source_id -> prior capture
```

Both messages and documents reference the source capture and import run. Chunks
and entries reference their typed document parent. FTS indexes are deferred.

## `import-manifest.json`

The Discord importer writes `02_Processed/import-manifest.json` using
`MANIFEST_PATH` after the batch finishes. Its top-level fields are:

- `generated_at_utc`: time the manifest was written;
- `database`: database path, relative to the archive root when possible;
- `files_processed`: count of files that completed `import_file()` successfully;
- `results`: one result object per successfully completed source file.

Each successful or partial result remembers:

- relative source file path;
- source SHA-256;
- server, category, and channel metadata;
- channel ID;
- source and import IDs, run status, warnings, and the `ValidationReport`.

### Can it tell the program a file was already processed?

Not by itself. The manifest is a report, not the mechanism used to decide whether
to process a file.

Capture registration is idempotent on `(logical_source_key, sha256)`. Each
attempt creates a new import run, and observations are unique only on
`(import_id, discord_message_id)`. A repeated Discord message ID in a later
capture is retained as another observation rather than skipped.

The document importer registers captures by logical source path and SHA-256.
Each attempt creates a new import run and typed document, even when the same
bytes are parsed again; chunks and entries remain attached to that document.

The manifest can help a human or external tool compare paths and hashes, but the
program does not load the manifest before importing. It also does not include
failed files because failed files are not appended to `results`. Consequently it
cannot reliably serve as a complete processed/unprocessed ledger.

Changed files create new captures. A clean accepted import links the new capture
to the previous accepted capture; selecting the current document/message view
remains deferred to the Reader/search layer.

## Failure and Consistency Summary

| Area | Current behavior |
| --- | --- |
| Invalid JSON | The individual Discord file fails; later files continue. |
| Invalid message structure | The individual Discord file fails before commit. |
| Invalid timestamp/boolean | The individual Discord file fails before commit. |
| Missing message ID | Message is counted as invalid and skipped during insertion. |
| Duplicate Discord message ID | Message is skipped; the file itself can still succeed. |
| Duplicate Discord file hash | Import row is ignored; message IDs also prevent duplicates. |
| Unsupported document extension | Returned as unsupported and not imported. |
| PDF with no extractable text | Stored as a document with `no_text` and a warning. |
| Document extraction failure | Reported for that file; later files continue. |
| Document database failure | Current document transaction rolls back. |
| FTS failure | Discord FTS runs after imports; document FTS is inside its transaction. |
| CLI exit status after per-file failures | Both import CLIs generally return `0`. |

## Remaining Out-of-Scope Capabilities

There is no implemented reader UI, search CLI/API, or backup workflow.
Automated identity inference is limited to explicit header labels and
registered-alias lookup; confidence ranking uses only those Phase E records
and reviewed attribution outcomes. It does not infer identity from
natural-language content or infer players from speaker labels. The empty `database.py`,
`discord_parser.py`, and `validator.py` files do not own the importer logic;
Discord and document ingestion remain concentrated in `build_archive.py` and
`document_parser.py`.
