# Archive Data Model Overview

This document explains the current project architecture, the current SQLite structure, and the proposed long-term archive model in the naming conventions already used across the repository.

---

## 01 — Import Pipeline

The project currently has two ingestion paths that both end in SQLite, but they are not yet unified into a single archival model.

```mermaid
flowchart LR
    A[01_Raw\nDiscord JSON exports\nTXT / PDF / DOCX files] --> B[Discovery]
    B --> C[Validation]
    C --> D[Parsing / Normalization]
    D --> E[SQLite archive database]
    E --> E1[sources]
    E --> E2[imports]
    E --> E3[message observations]
    E -. later .-> F[FTS search indexes]

    D --> D1[build_archive.py\nDiscord messages]
    D --> D2[document_parser.py\nText / document records]

    E --> E4[legacy document tables]
```

### Current implementation split

1. Discord pipeline: `src/build_archive.py` imports DiscordChatExporter JSON into
    `sources`, `imports`, and per-import message observations. FTS is deferred.

2. Document pipeline: `src/document_parser.py` and `src/alt_archive_builder.py`
    import TXT/PDF/DOCX into typed documents, chunks, and entries; FTS is deferred.

### Important note

The current implementation is solid for each type independently, but the database still treats Discord content and document content as separate families. The archive does not yet have a shared identity layer for:

- character
- player
- source entity
- scene
- timeline
- event
- document relationship

---

## 02 — Legacy SQLite Schema Diagram

This diagram documents the pre-migration layout. The active target schema is
documented in [Implementation Schema.md](Implementation%20Schema.md).

```mermaid
erDiagram
    IMPORTS ||--o{ MESSAGES : contains
    MESSAGES ||--o{ MESSAGES_FTS : indexed_by

    IMPORTS {
        INTEGER import_id PK
        TEXT source_file
        TEXT source_sha256
        TEXT server_name
        TEXT category_name
        TEXT channel_name
        TEXT imported_at_utc
        INTEGER source_message_count
        INTEGER imported_message_count
    }

    MESSAGES {
        INTEGER row_id PK
        TEXT message_id
        TEXT message_type
        TEXT timestamp_original
        TEXT timestamp_utc
        TEXT timestamp_edited
        TEXT category_name
        TEXT channel_name
        TEXT author_discord_id
        TEXT author_discord_name
        TEXT character_name
        TEXT player_name
        TEXT content_original
        TEXT content_search
        TEXT source_file
        TEXT source_sha256
        TEXT source_json
    }

    MESSAGES_FTS {
        TEXT content_search
        TEXT character_name
        TEXT player_name
        TEXT author_discord_name
        TEXT channel_name
        TEXT category_name
    }
```

```mermaid
erDiagram
    DOCUMENTS ||--o{ DOCUMENT_CHUNKS : contains
    DOCUMENTS ||--o{ DOCUMENT_ENTRIES : contains
    DOCUMENTS ||--o{ DOCUMENTS_FTS : indexed_by

    DOCUMENTS {
        INTEGER document_id PK
        TEXT source_file
        TEXT source_path
        TEXT source_filename
        TEXT source_extension
        TEXT source_format
        TEXT source_sha256
        TEXT title
        TEXT document_type
        TEXT status
        TEXT imported_at_utc
        TEXT content_text
        TEXT content_search
        TEXT metadata_json
        TEXT pages_json
        TEXT blocks_json
        TEXT warnings_json
    }

    DOCUMENT_CHUNKS {
        INTEGER chunk_id PK
        INTEGER document_id FK
        INTEGER chunk_index
        TEXT section_title
        TEXT content_text
        TEXT content_search
    }

    DOCUMENT_ENTRIES {
        INTEGER entry_id PK
        INTEGER document_id FK
        INTEGER entry_index
        TEXT speaker_raw
        TEXT character_id
        TEXT player_id
        TEXT content_text
    }

    DOCUMENTS_FTS {
        TEXT title
        TEXT content_text
        TEXT content_search
        TEXT document_type
        TEXT source_file
    }
```

### What this schema does well

- Stores raw import metadata and source hashes
- Preserves original Discord message payloads
- Supports document parsing and chunk extraction
- Enables full-text searching with SQLite FTS5
- Separates provenance from extracted content

### What it still does not do well

- Unify Discord and document records under a single archival model
- Treat characters, players, scenes, and events as first-class entities
- Link message content cleanly to character attribution and narrative context
- Represent a true relationship graph beyond simple timestamps and search indexes

---

## 03 — Proposed Archive Data Model

The proposed model keeps the raw archive faithful while introducing a shared archival layer that can relate messages, documents, identities, and narrative structures.

```mermaid
flowchart TD
    A[01_Raw\nImmutable source files] --> B[archive_sources]
    B --> C[archive_imports]
    C --> D[archive_content]

    D --> D1[messages]
    D --> D2[documents]
    D --> D3[document_chunks]
    D --> D4[document_entries]

    B --> E[archive_entities]
    E --> E1[players]
    E --> E2[characters]
    E --> E3[discord_identities]
    E --> E4[scenes / arcs / locations]

    D --> F[archive_links]
    F --> F1[message -> message]
    F --> F2[document -> document]
    F --> F3[message -> document]

    D --> H[editorial relationships]
    E --> H
    H --> H1[message_attributions]
    H --> H2[scene_messages]
    H --> H3[scene_participants]
    H --> H4[scene -> arc / location]

    D --> G[archive_search]
    G --> G1[FTS indexes]
    G --> G2[structured metadata filters]
```

### Proposed conceptual tables

```mermaid
erDiagram
    ARCHIVE_SOURCES ||--o{ ARCHIVE_IMPORTS : records
    ARCHIVE_IMPORTS ||--o{ MESSAGES : produces
    ARCHIVE_IMPORTS ||--o{ DOCUMENTS : produces
    DOCUMENTS ||--o{ DOCUMENT_CHUNKS : contains
    DOCUMENTS ||--o{ DOCUMENT_ENTRIES : contains

    PLAYERS ||--o{ CHARACTERS : portrays
    DISCORD_IDENTITIES ||--o{ MESSAGES : authors
    CHARACTERS ||--o{ ATTRIBUTIONS : attributed_to
    MESSAGES ||--o{ ATTRIBUTIONS : receives
    DOCUMENT_ENTRIES ||--o{ ATTRIBUTIONS : receives
    SCENES ||--o{ SCENE_MESSAGES : contains
    MESSAGES ||--o{ SCENE_MESSAGES : included_in

    ARCHIVE_LINKS {
        INTEGER link_id PK
        INTEGER from_content_id FK
        INTEGER to_content_id FK
        TEXT link_type
        TEXT confidence
    }

    PLAYERS {
        INTEGER player_id PK
        TEXT preferred_name
        TEXT notes
    }

    CHARACTERS {
        INTEGER character_id PK
        INTEGER player_id FK
        TEXT canonical_name
        TEXT description
        TEXT notes
    }

    SCENES {
        INTEGER scene_id PK
        INTEGER arc_id FK
        INTEGER location_id FK
        TEXT title
        TEXT summary
        TEXT status
    }

    ARCS {
        INTEGER arc_id PK
        TEXT title
        TEXT summary
        TEXT status
    }

    LOCATIONS {
        INTEGER location_id PK
        TEXT canonical_name
        TEXT location_type
        INTEGER parent_location_id
    }

    MESSAGES {
        INTEGER row_id PK
        TEXT message_id
        TEXT source_id FK
        TEXT discord_identity_id FK
        TEXT content_original
        TEXT timestamp_utc
    }

    DOCUMENTS {
        INTEGER document_id PK
        TEXT source_id FK
        TEXT title_original
        TEXT document_type
        TEXT extracted_text
    }

    DOCUMENT_CHUNKS {
        INTEGER chunk_id PK
        INTEGER document_id FK
        INTEGER chunk_index
        TEXT content_original
    }

    DOCUMENT_ENTRIES {
        INTEGER entry_id PK
        INTEGER document_id FK
        INTEGER entry_index
        TEXT speaker_raw
        TEXT content_original
    }

    MESSAGE_ATTRIBUTIONS {
        INTEGER message_attribution_id PK
        INTEGER message_id FK
        INTEGER character_id FK
        INTEGER player_id FK
        TEXT method
        TEXT confidence
    }

    SCENE_MESSAGES {
        INTEGER scene_id FK
        INTEGER message_id FK
        INTEGER sequence_override
    }

    SCENE_PARTICIPANTS {
        INTEGER scene_id FK
        INTEGER character_id FK
        INTEGER player_id FK
        TEXT participation_type
        TEXT confidence
    }
```

The `archive_*` names are architectural groupings, not invitations to create
generic polymorphic rows. In particular:

- `archive_content` is the content boundary containing explicit tables such
  as `messages`, `documents`, `document_chunks`, and `document_entries`.
- `archive_entities` is the domain boundary containing explicit tables such
  as `players`, `characters`, `scenes`, `arcs`, `locations`, and Discord
  identities.
- `archive_links` is reserved for content-to-content relationships such as
    message replies, message references, quotes, document relationships, and
    citations. Its endpoints are content records.
- Editorial relationships use explicit tables such as
    `message_attributions`, `scene_messages`, and `scene_participants`. A
    message-to-character relationship is not the same kind of relationship as a
    message reply.

Do not add `content_type`, `entity_type`, or a universal JSON payload merely to
make unrelated records fit one table. Add an explicit table when the record has
different semantics, lifecycle rules, constraints, or queries.

### Design direction

The proposed archive model is not a replacement for the raw materials. Instead, it adds a structured layer that understands:

- which source file a record came from
- which identities are involved
- which content objects are related
- how Discord events and document entries connect to narrative context
- how to search and reason across both structured and unstructured material

### Key project-aligned principles

1. Preserve the raw source
   - Keep `01_Raw` immutable and hash-verified.

2. Separate raw from derived
   - `02_Processed` is where extracted, normalized, and analyzed data lives.

3. Keep content type aware
    - `messages`, `documents`, `document_chunks`, and `document_entries` remain explicit typed records within the `archive_content` boundary.

4. Model identity explicitly
    - Discord identity, player, character, bot, webhook, and narrator must be distinguishable through explicit tables and relationships.

5. Treat relationships as real archival data
    - Store content relationships in `archive_links` and editorial relationships in explicit junction or foreign-key tables. Do not infer either category only at query time.

---

## Summary

The current implementation is a strong ingestion foundation: it imports Discord exports and documents into SQLite and builds FTS indexes for search. The long-term model should keep that foundation while adding the five `archive_*` boundaries and explicit typed tables for content and domain concepts. Shared links should be introduced only where they solve a demonstrated cross-table relationship that cannot be expressed clearly with ordinary foreign keys.
