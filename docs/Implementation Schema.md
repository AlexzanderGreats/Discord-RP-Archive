# Implementation Schema: Current Schema

This document describes the migration-backed SQLite schema currently
implemented in the archive, including its concrete columns and constraints for:

```text
sources
imports
servers
categories
channels
messages
discord_identities
players
characters
character_aliases
player_discord_identities
player_characters
message_attributions
attribution_events
arcs
scenes
scene_messages
scene_participants
boundary_detection_runs
boundary_detection_run_messages
event_boundary_candidates
documents
document_chunks
document_entries
schema_migrations
```

This is the migration-backed schema used by both the Discord JSON and document
importers (version 016). The checked-in live database may still be at version
015 and requires an explicit migration run before it can use boundary persistence.

Migration 016 adds versioned boundary detection runs and candidate results.
These are separate from the curated `scenes` and `scene_messages` tables; no
candidate message membership is stored in curated scene data.

Scene curation is implemented in migration 015. Scene membership is explicit
and ordered through `scene_messages`; `scene_channels` is a view of the distinct
channels represented by those messages. `scenes.fictional_time` stores optional
in-world time, independently of message timestamps.

## Design Decisions

- A `sources` row is one immutable file capture. `logical_source_key` groups
  captures that are explicitly declared to represent the same export scope.
  The key is not inferred from a filename alone.
- An `imports` row is one attempt to parse one source capture with a specific
  importer version and configuration. Retrying creates another import row.
- `messages` stores source observations per import run, not a single mutable
  canonical message. The same Discord message ID can therefore have distinct
  payloads in different captures/runs.
- Names and hierarchy metadata on `servers`, `categories`, `channels`, and
  `discord_identities` are current normalized values. Exact historical
  values remain in source files and message/document raw snapshots.
- A source capture becomes the current accepted capture only after a successful
  import is explicitly accepted. A later accepted capture links to the previous
  accepted capture using `supersedes_source_id`. Older captures are retained.
- SQLite local primary keys are integers. Discord IDs and hashes are `TEXT`.
- Timestamps are `TEXT` in ISO-8601 UTC form. JSON payloads are `TEXT` containing
  UTF-8 JSON.
- Foreign keys use `ON DELETE RESTRICT` except document chunks, which use
  `ON DELETE CASCADE`. Application connections must enable
  `PRAGMA foreign_keys = ON`.

## Legend

| Mark | Meaning |
| --- | --- |
| `Y` | Yes |
| `N` | No |
| `—` | Not applicable / no constraint of that kind |
| `I` | Immutable after the relevant import is accepted/completed |
| `D` | Derived or reproducible from source/import data |
| `V` | Interpreted or assigned by archive policy/editorial judgment |

`Index` names refer to indexes specified after each table. A `UNIQUE` constraint
also creates an index in SQLite; those indexes are listed as named unique
indexes in the table's index section.

## `sources`

One row per byte-distinct source capture. A capture is never overwritten by a
new file at the same path.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | --- :| --- | --- :| ---: |---: |
| `source_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `logical_source_key` | `TEXT` | N | N | — | Composite with `sha256` | `ux_sources_logical_hash`, `ix_sources_logical_key` | Y | N | V |
| `source_kind` | `TEXT` | N | N | — | N | `ix_sources_kind` | Y | N | N |
| `source_path` | `TEXT` | N | N | — | N | — | Y | N | N |
| `sha256` | `TEXT` | N | N | — | Composite with `logical_source_key` | `ux_sources_logical_hash`, `ix_sources_sha256` | Y | D | N |
| `byte_size` | `INTEGER` | N | N | — | N | — | Y | D | N |
| `scope_json` | `TEXT` | N | N | — | N | — | Y | N | V |
| `registered_at` | `TEXT` | N | N | — | N | — | Y | D | N |
| `supersedes_source_id` | `INTEGER` | Y | N | Composite with `logical_source_key` -> `sources(source_id, logical_source_key)` | N | `ix_sources_supersedes` | Y after acceptance | N | V |

Constraints and indexes:

- `UNIQUE(logical_source_key, sha256)` prevents duplicate registration of the
  same bytes for the same declared logical source while allowing identical bytes
  to occur under distinct logical sources.
- Alternate key `UNIQUE(source_id, logical_source_key)` supports the composite
  self-foreign-key and ensures a capture only supersedes another capture from
  the same logical source.
- Composite self-foreign-key `(supersedes_source_id, logical_source_key)`
  references `sources(source_id, logical_source_key)`.
- `CHECK(byte_size >= 0)`.
- `CHECK(supersedes_source_id IS NULL OR supersedes_source_id <> source_id)`.
- `source_kind` is constrained to `discord_json`, `txt`, `pdf`, `docx`, or
  `other`.
- Index `ix_sources_logical_key` on `logical_source_key`.
- Index `ix_sources_sha256` on `sha256`.
- Index `ix_sources_kind` on `source_kind`.
- Index `ix_sources_supersedes` on `supersedes_source_id`.

The current capture is derived from the accepted-import and supersession chain;
there is deliberately no mutable `is_current` bit. `scope_json` and the
supersession link are explicit source-grouping decisions, not byte-level facts.

## `imports`

One row per parse/import attempt for exactly one source capture.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `import_id` | `INTEGER` | N | Y | — | — | PK | Y after completion | N | N |
| `source_id` | `INTEGER` | N | N | `sources.source_id` | Composite with `import_id` | `ux_imports_id_source`, `ix_imports_source` | Y | N | N |
| `started_at` | `TEXT` | N | N | — | N | — | Y | N | N |
| `finished_at` | `TEXT` | Y | N | — | N | — | Y after completion | N | N |
| `status` | `TEXT` | N | N | — | N | `ix_imports_status` | Y after completion | D | N |
| `accepted_at` | `TEXT` | Y | N | — | N | `ix_imports_accepted` | Y | N | V |
| `importer_name` | `TEXT` | N | N | — | N | — | Y | N | N |
| `importer_version` | `TEXT` | N | N | — | N | — | Y | N | N |
| `config_sha256` | `TEXT` | Y | N | — | N | `ix_imports_config_hash` | Y | D | N |
| `records_seen` | `INTEGER` | Y | N | — | N | — | Y after completion | D | N |
| `records_written` | `INTEGER` | Y | N | — | N | — | Y after completion | D | N |
| `warnings_json` | `TEXT` | N | N | — | N | — | Y after completion | D | N |
| `error_summary` | `TEXT` | Y | N | — | N | — | Y after completion | D | N |

Constraints and indexes:

- Alternate key `UNIQUE(import_id, source_id)` allows child rows to enforce that
  their `source_id` matches the source on the referenced import.
- `status` is constrained to `running`, `succeeded`, `partial`, or `failed`.
- `accepted_at` is set only for a successfully validated materialization;
  `CHECK(accepted_at IS NULL OR status = 'succeeded')`.
- `CHECK(records_seen IS NULL OR records_seen >= 0)` and equivalent for
  `records_written`.
- Index `ix_imports_source` on `source_id`.
- Index `ix_imports_status` on `status`.
- Index `ix_imports_started` on `started_at`.
- Index `ix_imports_config_hash` on `config_sha256`.
- Index `ix_imports_accepted` on `(source_id, accepted_at)`.

An import attempt is inserted as `running`, then finalized exactly once to a
terminal `status`. Successful attempts may receive an immutable `accepted_at`
when their output is approved for normal archive views. Multiple parser runs for
one capture can be accepted over time; the latest accepted materialization is
the one with the greatest `accepted_at`. A failed or partial attempt must not
supersede a previous accepted source capture.

## `servers`

One normalized row per Discord server ID.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `server_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `discord_server_id` | `TEXT` | N | N | — | Y | `ux_servers_discord_id` | Y | N | N |
| `name_current` | `TEXT` | Y | N | — | N | `ix_servers_name` | N | D | N |
| `first_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_servers_first_import` | N | D | N |
| `last_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_servers_last_import` | N | D | N |

`name_current` is refreshed from the latest accepted observation. Exact exported
names remain in raw source captures. Indexes are the named unique/index entries
above.

## `categories`

One normalized category per Discord category ID within a server.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `category_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `server_id` | `INTEGER` | N | N | `servers.server_id` | Composite with `discord_category_id` | `ux_categories_server_discord`, `ux_categories_id_server`, `ix_categories_server` | Y | N | N |
| `discord_category_id` | `TEXT` | N | N | — | Composite with `server_id` | `ux_categories_server_discord` | Y | N | N |
| `name_current` | `TEXT` | Y | N | — | N | `ix_categories_name` | N | D | N |
| `position_current` | `INTEGER` | Y | N | — | N | — | N | D | N |
| `first_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_categories_first_import` | N | D | N |
| `last_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_categories_last_import` | N | D | N |

Constraints and indexes:

- `UNIQUE(server_id, discord_category_id)`.
- Alternate key `UNIQUE(category_id, server_id)` supports a composite channel
  foreign key that prevents linking a category from another server.
- `CHECK(position_current IS NULL OR position_current >= 0)`.
- Index `ix_categories_server` on `server_id`; index
  `ix_categories_name` on `name_current`; indexes on both import references.

## `channels`

One normalized channel per Discord channel ID within a server. `category_id` is
nullable because Discord channels may not belong to a category.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `channel_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `server_id` | `INTEGER` | N | N | `servers.server_id` | Composite with `discord_channel_id`; paired with category | `ux_channels_server_discord`, `ux_channels_id_server`, `ix_channels_server` | Y | N | N |
| `discord_channel_id` | `TEXT` | N | N | — | Composite with `server_id` | `ux_channels_server_discord` | Y | N | N |
| `category_id` | `INTEGER` | Y | N | Composite with `server_id` -> `categories(category_id, server_id)` | N | `ix_channels_category_server` | Y | N | N |
| `name_current` | `TEXT` | Y | N | — | N | `ix_channels_name` | N | D | N |
| `channel_type` | `TEXT` | Y | N | — | N | `ix_channels_type` | N | D | N |
| `topic_current` | `TEXT` | Y | N | — | N | — | N | D | N |
| `position_current` | `INTEGER` | Y | N | — | N | — | N | D | N |
| `purpose` | `TEXT` | Y | N | — | N | `ix_channels_purpose` | N | N | V |
| `first_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_channels_first_import` | N | D | N |
| `last_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_channels_last_import` | N | D | N |

Constraints and indexes:

- `UNIQUE(server_id, discord_channel_id)`.
- Alternate key `UNIQUE(channel_id, server_id)` supports same-server
  relationships.
- Composite foreign key `(category_id, server_id)` references
  `categories(category_id, server_id)`; when category is null the relationship
  is absent.
- `purpose` is constrained to `rp`, `ooc`, `mixed`, or `unknown`; null means not
  reviewed/classified.
- `CHECK(position_current IS NULL OR position_current >= 0)`.
- Indexes listed in the matrix cover server, category/server, name, type,
  purpose, and first/last imports.

## `discord_identities`

One stable Discord user, bot, webhook, or system identity. Current names are
convenience values, not historical author evidence.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `identity_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `identity_type` | `TEXT` | N | N | — | N | `ix_identities_type` | Y | N | N |
| `discord_user_id` | `TEXT` | Y | N | — | Partial unique when not null | `ux_identities_user_id` | Y | N | N |
| `webhook_id` | `TEXT` | Y | N | — | Partial unique when not null | `ux_identities_webhook_id` | Y | N | N |
| `username_current` | `TEXT` | Y | N | — | N | `ix_identities_username` | N | D | N |
| `display_name_current` | `TEXT` | Y | N | — | N | `ix_identities_display_name` | N | D | N |
| `is_bot` | `INTEGER` | Y | N | — | N | — | N | D | N |
| `first_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_identities_first_import` | N | D | N |
| `last_seen_import_id` | `INTEGER` | Y | N | `imports.import_id` | N | `ix_identities_last_import` | N | D | N |

Constraints and indexes:

- `identity_type` is constrained to `user`, `bot`, `webhook`, `system`, or
  `unknown`.
- User/bot rows require `discord_user_id`; webhook rows require `webhook_id`.
  System/unknown rows may have neither.
- Partial unique indexes apply to `discord_user_id` and `webhook_id` separately,
  with `WHERE ... IS NOT NULL`.
- `CHECK(is_bot IS NULL OR is_bot IN (0, 1))`.
- The current username/display name may change. Historical author snapshots are
  preserved on each message in its source fields and `source_json`.

## `messages`

One source observation of a Discord message per import run. This is intentional:
two accepted captures can contain the same Discord message ID with different
payloads without overwriting one another.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `message_observation_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `source_id` | `INTEGER` | N | N | `sources.source_id` | Paired with import FK | `ix_messages_source` | Y | N | N |
| `import_id` | `INTEGER` | N | N | Composite with `source_id` -> `imports(import_id, source_id)` | Composite with `discord_message_id` | `ux_messages_import_discord`, `ix_messages_import` | Y | N | N |
| `discord_message_id` | `TEXT` | N | N | — | Composite with `import_id` | `ux_messages_import_discord`, `ix_messages_discord_id` | Y | N | N |
| `channel_id` | `INTEGER` | Y | N | `channels.channel_id` | N | `ix_messages_channel_time` | Y | D | N |
| `author_identity_id` | `INTEGER` | Y | N | `discord_identities.identity_id` | N | `ix_messages_author_identity` | Y | D | N |
| `author_discord_user_id_original` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `author_discord_name_original` | `TEXT` | Y | N | — | N | `ix_messages_author_name` | Y | N | N |
| `author_display_name_original` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `author_nickname_original` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `author_is_bot_original` | `INTEGER` | Y | N | — | N | — | Y | N | N |
| `webhook_id_original` | `TEXT` | Y | N | — | N | `ix_messages_webhook` | Y | N | N |
| `message_type` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `timestamp_original` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `timestamp_utc` | `TEXT` | Y | N | — | N | `ix_messages_timestamp` | Y | D | N |
| `timestamp_unix` | `REAL` | Y | N | — | N | — | Y | D | N |
| `timestamp_edited_original` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `timestamp_edited_utc` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `content_original` | `TEXT` | N | N | — | N | — | Y | N | N |
| `content_search` | `TEXT` | N | N | — | N | FTS deferred | Y | D | N |
| `reply_to_discord_message_id` | `TEXT` | Y | N | — | N | `ix_messages_reply_id` | Y | N | N |
| `is_pinned` | `INTEGER` | Y | N | — | N | — | Y | N | N |
| `call_ended_timestamp_original` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `attachments_json` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `embeds_json` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `stickers_json` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `reactions_json` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `mentions_json` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `inline_emojis_json` | `TEXT` | Y | N | — | N | — | Y | N | N |
| `source_json` | `TEXT` | N | N | — | N | — | Y | N | N |

Constraints and indexes:

- `UNIQUE(import_id, discord_message_id)` prevents duplicate message IDs within
  a single import run without imposing global uniqueness across captures.
- Composite foreign key `(import_id, source_id)` references
  `imports(import_id, source_id)` and guarantees provenance consistency.
- Boolean columns are constrained to null or `0`/`1`.
- Index `ix_messages_source` on `source_id`.
- Index `ix_messages_import` on `import_id`.
- Index `ix_messages_discord_id` on `discord_message_id`.
- Index `ix_messages_timestamp` on `timestamp_utc`.
- Composite index `ix_messages_channel_time` on `(channel_id, timestamp_utc)`.
- Indexes on `author_identity_id`, `author_discord_name_original`,
  `webhook_id_original`, and `reply_to_discord_message_id`.

`channel_id` and `author_identity_id` are normalized references derived from
source identifiers; original author fields and `source_json` retain the source
snapshot. Attribution is stored separately and never overwrites these fields.

## Attribution Records

Migration 014 adds `players`, `characters`, and normalized relationship tables:
`character_aliases`, `player_discord_identities`, and `player_characters`.
Identity/player and player/character links can have optional UTC validity
intervals so historical account ownership or portrayals are not assumed to be
permanent.

`message_attributions` references one `message_observation_id`, not only a
Discord message ID, because separate imports preserve distinct source
observations. Each claim records its optional player and character, method,
confidence, pending/accepted/rejected/superseded status, evidence JSON, creator,
and reviewer details. At most one claim can be accepted per observation;
accepting a replacement supersedes the prior claim. `attribution_events` keeps
the append-only proposal and review history.

The initial rule pass recognizes a leading `[Speaker]`, `Speaker:`, or
`Speaker - text` / `Speaker — text` marker only when its name exactly matches a
registered character alias (case and whitespace normalized). It may include a
player only when the source identity-to-player and player-to-character links
both apply at the message timestamp. Without a registered marker match, a
known identity link can propose a player-only claim. All rule and future model
claims start as pending; review is required for acceptance.

## `documents`

One extracted document record per source capture/import materialization. The
original source remains the authoritative evidence; extracted text is
reproducible derived content.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `document_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `source_id` | `INTEGER` | N | N | `sources.source_id` | Paired with import FK | `ix_documents_source` | Y | N | N |
| `import_id` | `INTEGER` | N | N | Composite with `source_id` -> `imports(import_id, source_id)` | Y | `ux_documents_import`, `ix_documents_import` | Y | N | N |
| `source_filename` | `TEXT` | N | N | — | N | `ix_documents_filename` | Y | D | N |
| `source_format` | `TEXT` | N | N | — | N | `ix_documents_format` | Y | D | N |
| `file_size_bytes` | `INTEGER` | N | N | — | N | — | Y | D | N |
| `title_original` | `TEXT` | Y | N | — | N | `ix_documents_title` | Y | D | N |
| `title_override` | `TEXT` | Y | N | — | N | `ix_documents_title` | N | N | V |
| `document_type` | `TEXT` | N | N | — | N | `ix_documents_type` | N | N | V |
| `document_status` | `TEXT` | N | N | — | N | `ix_documents_status` | N | N | V |
| `created_at_source` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `modified_at_source` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `imported_at` | `TEXT` | N | N | — | N | — | Y | D | N |
| `page_count` | `INTEGER` | Y | N | — | N | — | Y | D | N |
| `paragraph_count` | `INTEGER` | N | N | — | N | — | Y | D | N |
| `character_count` | `INTEGER` | N | N | — | N | — | Y | D | N |
| `word_count` | `INTEGER` | N | N | — | N | — | Y | D | N |
| `extraction_status` | `TEXT` | N | N | — | N | `ix_documents_extraction_status` | Y | D | N |
| `extraction_error` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `content_text` | `TEXT` | N | N | — | N | FTS deferred | Y | D | N |
| `content_search` | `TEXT` | N | N | — | N | FTS deferred | Y | D | N |
| `metadata_json` | `TEXT` | N | N | — | N | — | Y | D | N |
| `pages_json` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `blocks_json` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `warnings_json` | `TEXT` | N | N | — | N | — | Y | D | N |

Constraints and indexes:

- `UNIQUE(import_id)` ensures one document output per document-source import.
- Composite foreign key `(import_id, source_id)` references
  `imports(import_id, source_id)`.
- `source_format` is constrained to `txt`, `pdf`, or `docx` for this slice.
- `document_type` is constrained to `unknown`, `character_profile`,
  `character_sheet`, `lore`, `worldbuilding`, `story`, `chapter`,
  `rp_transcript`, `rp_scene`, `powerscaling`, `analysis`, `reference`, `notes`,
  or `other`.
- `document_status` is constrained to `historical`, `current`, `reference`,
  `draft`, or `unknown`.
- `extraction_status` is constrained to `success`, `partial`, `no_text`, or
  `error`.
- Counts and file size must be non-negative when present.
- Indexes on `source_id`, `import_id`, `source_filename`, `source_format`,
  `title_original`, `title_override`, `document_type`, `document_status`, and
  `extraction_status`.

The effective display title is `COALESCE(title_override, title_original)`.
`document_type` and `document_status` are explicit archive classifications, even
if initially populated by heuristics.

## `document_chunks`

Reproducible segmentation of one document's extracted text.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `chunk_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `document_id` | `INTEGER` | N | N | `documents.document_id` (`ON DELETE CASCADE`) | Composite with `chunk_index` | `ux_chunks_document_index`, `ix_chunks_document` | Y | N | N |
| `chunk_index` | `INTEGER` | N | N | — | Composite with `document_id` | `ux_chunks_document_index` | Y | D | N |
| `page_start` | `INTEGER` | Y | N | — | N | — | Y | D | N |
| `page_end` | `INTEGER` | Y | N | — | N | — | Y | D | N |
| `paragraph_start` | `INTEGER` | Y | N | — | N | — | Y | D | N |
| `paragraph_end` | `INTEGER` | Y | N | — | N | — | Y | D | N |
| `section_title` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `content_text` | `TEXT` | N | N | — | N | FTS deferred | Y | D | N |
| `content_search` | `TEXT` | N | N | — | N | FTS deferred | Y | D | N |
| `source_location_json` | `TEXT` | Y | N | — | N | — | Y | D | N |

Constraints and indexes:

- `UNIQUE(document_id, chunk_index)`.
- Index `ix_chunks_document` on `document_id`.
- `CHECK(chunk_index >= 0)`; page/paragraph values, when present, are positive
  and end values must not precede start values.

Chunk text is derived from the parent document's extracted text. A chunk has no
independent source hash and must not outlive its document.

## `document_entries`

Detected speaker or roleplay entries within one typed document. Entries remain
document-specific records and are not stored as messages or assumed to align
one-to-one with chunks.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `entry_id` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `document_id` | `INTEGER` | N | N | `documents.document_id` (`ON DELETE CASCADE`) | Composite with `entry_index` | `ux_document_entries_document_index`, `ix_document_entries_document` | Y | N | N |
| `entry_index` | `INTEGER` | N | N | — | Composite with `document_id` | `ux_document_entries_document_index` | Y | D | N |
| `speaker_raw` | `TEXT` | Y | N | — | N | — | Y | D | N |
| `character_id` | `TEXT` | Y | N | — | N | — | Y | N | V |
| `player_id` | `TEXT` | Y | N | — | N | — | Y | N | V |
| `content_text` | `TEXT` | N | N | — | N | FTS deferred | Y | D | N |
| `source_location_json` | `TEXT` | Y | N | — | N | — | Y | D | N |

Constraints and indexes:

- `UNIQUE(document_id, entry_index)`.
- Index `ix_document_entries_document` on `document_id`.
- `CHECK(entry_index >= 0)`.
- Entries cascade with their parent document and do not become Discord messages.

## `schema_migrations`

One row per successfully applied schema migration.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? | Immutable? | Derived? | Interpreted? |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: |
| `version` | `INTEGER` | N | Y | — | — | PK | Y | N | N |
| `name` | `TEXT` | N | N | — | Y | `ux_schema_migrations_name` | Y | N | N |
| `checksum` | `TEXT` | N | N | — | N | `ix_schema_migrations_checksum` | Y | D | N |
| `applied_at` | `TEXT` | N | N | — | N | — | Y | D | N |

The migration ledger records only successful application. Failed migration
attempt details belong in application logs; this first slice does not add a
separate migration-attempt table.

## `boundary_detection_runs`

One record per deterministic boundary analysis execution.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? |
| --- | --- | ---: | ---: | --- | ---: | --- |
| `detection_run_id` | `INTEGER` | N | Y | — | — | PK |
| `run_type` | `TEXT` | N | N | — | — | — |
| `algorithm_version` | `TEXT` | N | N | — | N | `ix_boundary_detection_runs_algorithm` |
| `configuration_json` | `TEXT` | N | N | — | N | — |
| `configuration_sha256` | `TEXT` | N | N | — | N | — |
| `input_scope_json` | `TEXT` | N | N | — | N | — |
| `input_fingerprint` | `TEXT` | N | N | — | N | `ix_boundary_detection_runs_fingerprint` |
| `started_at` | `TEXT` | N | N | — | N | `ix_boundary_detection_runs_status` |
| `completed_at` | `TEXT` | Y | N | — | N | — |
| `status` | `TEXT` | N | N | — | N | `ix_boundary_detection_runs_status` |
| `statistics_json` | `TEXT` | N | N | — | N | — |
| `error_details` | `TEXT` | Y | N | — | N | — |

`run_type` is `full` or `incremental`. `status` is `running`, `completed`, or
`failed`, with consistency checks for completion time and error details. The
configuration and hash preserve the exact ruleset used; the input fingerprint
is calculated from ordered observations and extracted signals.

## `event_boundary_candidates`

One candidate transition, per run, between adjacent message observations. Rows
are created only for non-`none` classifications; run statistics count all
evaluated transitions.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? |
| --- | --- | ---: | ---: | --- | ---: | --- |
| `boundary_candidate_id` | `INTEGER` | N | Y | — | — | PK |
| `detection_run_id` | `INTEGER` | N | N | `boundary_detection_runs.detection_run_id` | Composite with pair | `ix_event_boundary_candidates_run` |
| `left_message_observation_id` | `INTEGER` | N | N | `messages.message_observation_id` | Composite with run/right | `ix_event_boundary_candidates_pair` |
| `right_message_observation_id` | `INTEGER` | N | N | `messages.message_observation_id` | Composite with run/left | `ix_event_boundary_candidates_pair` |
| `boundary_type` | `TEXT` | N | N | — | N | — |
| `raw_score` | `REAL` | N | N | — | N | — |
| `classification` | `TEXT` | N | N | — | N | `ix_event_boundary_candidates_run` |
| `supporting_evidence_json` | `TEXT` | N | N | — | N | — |
| `contradictory_evidence_json` | `TEXT` | N | N | — | N | — |
| `contributions_json` | `TEXT` | N | N | — | N | — |
| `review_status` | `TEXT` | N | N | — | N | `ix_event_boundary_candidates_review` |
| `created_at` | `TEXT` | N | N | — | N | `ix_event_boundary_candidates_review` |

`boundary_type` is `hard` or `soft`; `classification` is `soft_candidate`,
`strong_candidate`, or `hard_candidate`. `review_status` is initialized to
`pending` and reserves `accepted`, `rejected`, and `deferred` for future
review tooling. A unique `(detection_run_id, left_message_observation_id,
right_message_observation_id)` key prevents duplicate transitions within a
run. None of the boundary tables references or writes curated scenes.

## `boundary_detection_run_messages`

Ordered observations included in a detection run. This records the run's exact
input message set and ordering, including messages whose adjacent transitions
were classified as `none`.

| Column | SQLite type | Nullable? | Primary key? | Foreign key? | Unique? | Index? |
| --- | --- | ---: | ---: | --- | ---: | --- |
| `detection_run_id` | `INTEGER` | N | Composite PK | `boundary_detection_runs.detection_run_id` | Composite with position/message | PK |
| `position` | `INTEGER` | N | Composite PK | — | Composite with run | PK |
| `message_observation_id` | `INTEGER` | N | N | `messages.message_observation_id` | Composite with run | `ix_boundary_detection_run_messages_message` |

`UNIQUE(detection_run_id, message_observation_id)` prohibits duplicate
observations in a run; `CHECK(position >= 0)` preserves valid sequence positions.

## Relationship Summary

```text
sources 1 ──< imports 1 ──< messages
   │             │
  │             └──────── documents 1 ──< document_chunks
  │                                  └───< document_entries
   │
   └── supersedes_source_id -> prior source capture

servers 1 ──< categories
  │
  └───────< channels 1 ──< messages >── discord_identities
```

`messages` and `documents` each carry both `source_id` and `import_id`. The
composite foreign key guarantees the import belongs to that source capture.
Hierarchy rows point to the first and last import where they were observed; the
raw source file remains the detailed evidence.

## Current Exclusions

The schema still does not persist Discord thread membership, FTS tables,
attachment child tables, message edit history, semantic search, LLM
functionality, or a GUI. Boundary detection is limited to versioned run and
candidate records; it does not reconstruct scenes or write candidate
membership into curated `scenes` or `scene_messages`.

The schema can retain message payload observations per import, but selecting a
preferred observation across sources requires application logic that considers
accepted imports and explicit source-supersession links. FTS should be added only
after that selection rule is implemented, so obsolete and current observations
are not accidentally mixed in normal search results.
