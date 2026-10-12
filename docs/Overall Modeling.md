# Overall Modeling

## Purpose

This project is a local archival and research application for historical roleplay material, especially Discord roleplay spread across servers, categories, channels, threads, documents, and supplementary files.

The system must do two things at once:

1. Preserve the historical record faithfully.
2. Make the record usable through readable transcripts, attribution, chronology, structured browsing, and full-text search.

The central design rule is:

> **Evidence is preserved; interpretation is layered on top.**

The application must never silently rewrite the historical source material in order to make it more convenient to read.

```text
Raw source files
    ↓
Validated ingestion
    ↓
SQLite archival/index database
    ↓
Attribution, chronology, and editorial annotations
    ↓
Search, reader, reports, and GUI
```

***

## I. What Is Mutable and Immutable

### 1. Immutable source evidence

“Immutable” means the application does not edit or overwrite the original evidence after it has been captured. If the source needs correction, re-exporting, replacement, or exclusion, that is recorded as a new provenance event rather than silently changing the old file.

#### Raw files

The original archive files are immutable:

- DiscordChatExporter JSON exports
- DiscordChatExporter HTML exports
- Plain-text exports
- PDF files
- DOCX files
- Images
- Screenshots
- CSV files
- YAML/JSON configuration snapshots that were part of the original archive
- Any other historical source artifact

Raw files belong under `01_Raw/` and are never rewritten by the application.

```text
01_Raw/
  Discord/
  Documents/
  Images/
  External-References/
```

The archive application may read, hash, validate, and index these files. It must not rename, move, rewrite, deduplicate destructively, or otherwise mutate them.

#### Immutable source observations

The following fields should be stored exactly as they appeared in the source export:

- Source file path or archive-relative path.
- SHA-256 hash of the source file.
- File size.
- Import time.
- Parser/importer version.
- Discord server/guild ID.
- Discord server/guild name as exported.
- Category ID and category name as exported.
- Channel ID and channel name as exported.
- Thread ID and thread name as exported.
- Discord message ID.
- Discord message timestamp.
- Message content exactly as exported.
- Original author object exactly as exported.
- Original author ID, username, discriminator where present, display name, avatar URL, bot status, and webhook-related fields.
- Webhook ID where present.
- Reply/reference fields.
- Attachments, embeds, stickers, reactions, mentions, and other message metadata.
- Source document text as extracted.
- Raw parser output or source JSON where practical.

#### Discord IDs

Discord identifiers are immutable identifiers, not quantities.

Store all Discord IDs as `TEXT`, including:

- Guild/server IDs.
- Category IDs.
- Channel IDs.
- Thread IDs.
- Message IDs.
- User IDs.
- Webhook IDs.
- Attachment IDs where provided.

Do not convert Discord snowflake IDs to floating-point numbers. Treat them as opaque strings.

#### Timestamps

Preserve the original timestamp string from the source and also store a normalized UTC timestamp for ordering and querying.

```text
timestamp_original = source value exactly as exported
timestamp_utc      = normalized ISO-8601 UTC timestamp
timestamp_local    = optional display value in archive timezone
```

The source timestamp is evidence. A display timezone is a convenience layer.

#### Historical names

Discord names are not permanently stable in the real world, but the name in an exported message is part of the historical record.

Therefore:

- Preserve exported names exactly as found.
- Preserve stable IDs whenever available.
- Never replace historical exported names with current display names.
- Keep separate alias/current-preference fields in the identity or player layer if desired.

For example:

```text
Raw message author:
  username: OldDiscordName
  user_id: 123456789

Current archive identity:
  preferred_name: Player A
  aliases: OldDiscordName, NewDiscordName
```

The message should remain historically attributable to `OldDiscordName`; the reader can also show the preferred archival label where appropriate.

### 2. Derived but reproducible data

Derived data can be regenerated from raw files by running the importer again. It is not itself source evidence, but it should be deterministic and traceable.

Examples:

- Normalized timestamps.
- Parsed Discord message rows.
- Extracted document text.
- Document chunks.
- Search-normalized text.
- SHA-256 file hashes.
- Duplicate-detection records.
- Import manifests.
- Generated HTML/Markdown reader output.
- Full-text-search indexes.
- Search snippets.
- Attachment inventory rows.
- Derived channel/category tree.

Derived data belongs in SQLite or generated-output directories.

```text
02_Processed/
03_Reader/
04_Search/
archive.sqlite3
```

If the parser changes, derived data may be rebuilt. The raw file remains unchanged.

#### Source replacement and recapture policy

A new file hash at the same path is a **new immutable capture**, not an in-place
replacement of the old evidence. The archive policy is:

> Treat `XYZ` as a newer snapshot of the same logical export only when it belongs
> to the same configured source and the same declared scope. Preserve both
> captures. Mark `ABC` as superseded only as the prior/latest capture for that
> source; never delete its file, provenance, or observations.

So the archive means:

```text
Logical source: channel export for #winter-rp
  Capture ABC (older, retained, no longer latest)
       └── superseded as latest capture by
            Capture XYZ (newer, retained, current latest)

Messages present in both captures are overlapping observations, not duplicate
logical messages. Messages present only in ABC are still historical evidence.
```

The three interpretations are not mutually exclusive. `XYZ` is a newer snapshot
with potentially overlapping messages (the content interpretation); `ABC` and
`XYZ` are both preserved historical captures (the evidence rule); and `ABC` may
be marked `superseded` for the narrow purpose of identifying the latest capture.
Supersession does not mean its contents were false, deleted, or replaced in the
historical record.

##### Identity and records

Distinguish three records/concepts:

- **Logical source**: a configured export identity and scope, such as a channel
  export or a defined server export. A path by itself is not a reliable logical
  identity because a path may be reused or moved.
- **Source capture**: one immutable byte-for-byte file version, identified by
  its SHA-256 and associated with a logical source. Store its path, hash, size,
  capture/import timestamps, scope, status, and optional `supersedes_capture_id`.
- **Import run**: one attempt to parse a capture with a specific importer/parser
  version and configuration. A retry is another run, not another raw capture.

If no explicit logical source ID exists yet, archive-relative path plus declared
server/channel/range scope may be used as a provisional key. Do not infer that
two files are replacements solely because their filenames match. A scope change
(for example, one channel export becoming a server-wide export) is a different
logical source unless explicitly declared otherwise.

##### Processing `ABC` followed by `XYZ`

1. Keep the original `ABC` file unchanged and register its capture/hash.
2. Discover the changed bytes as capture `XYZ`; do not overwrite the hash or
   metadata on capture `ABC`.
3. Validate and import `XYZ` as a new run. A failed or incomplete run must not
   become the latest accepted capture and must not supersede the prior valid
   capture.
4. On successful validation, mark `XYZ` current/latest for that logical source
   and mark `ABC` `superseded` as the previous latest capture. Record the
   supersession link and audit event.
5. Link every message observation to the capture/run that supplied it.

For messages in overlapping exports:

- If a Discord `message_id` and its source payload are unchanged, keep one
  logical message and record that it was observed in both captures.
- If the same `message_id` has changed source content or metadata, retain both
  capture-specific observations/versions. Do not silently discard the later
  observation just because `messages.message_id` is unique.
- If a message appears in `ABC` but not `XYZ`, retain its `ABC` observation.
  Absence from a later export is not a deletion instruction. Record whether it
  is absent from the latest capture; only an explicit, separately audited
  tombstone may represent a confirmed deletion.
- If different logical sources provide conflicting observations for the same
  message ID, preserve both and flag the conflict for resolution. Do not choose
  a winner based only on import time.

The ordinary reader/search view may use the observation from the latest
successfully accepted capture of the relevant logical source. A historical
capture view must be able to show that capture's own observation. FTS should
index the selected current observation by default; historical-version search
can be an explicit mode.

This policy requires source-capture history and capture-specific message
observations (or equivalent provenance tables). A single mutable source hash on
`messages`, combined with a global unique `message_id`, cannot preserve divergent
payloads from successive exports. The Discord importer now records observations
per import and advances the accepted-capture chain only for clean runs. Current
observation selection and FTS indexing remain reader/search responsibilities and
are not implemented by the importer.

### 3. Interpretable and revisable data

Interpretations represent informed archival conclusions. They are necessary for a usable roleplay archive but must be explicitly marked as interpretations rather than disguised as raw facts.

Examples:

- Player identity.
- Character identity.
- Character aliases.
- Mapping a Discord account to a player.
- Mapping a webhook/Tupperbox proxy to a character.
- Mapping an individual message to a character.
- Scene boundaries.
- Scene titles.
- Scene participants.
- Arc membership.
- Fictional/in-world dates.
- Location/setting labels.
- Content tags.
- Canon status.
- Editorial summaries.
- Relationship annotations.
- Roleplay-versus-OOC classification.

These records must be mutable, reviewable, and auditable.

```text
Raw Message
  author = webhook 123
  content = "..."

        ↓

Attribution record
  character = Winter
  method = exact_webhook
  confidence = confirmed
  source = manual registry
  reviewed_at = 2026-09-25

        ↓

Reader display
  Winter (played by Player A): "..."
```

If the mapping is later found to be wrong, revise the attribution record. Do not rewrite the raw message author or message text.

### 4. Mutable mappings and annotations

These records are expected to evolve as the archive becomes better understood:

- Character-to-player mappings.
- Discord-account-to-player mappings.
- Webhook/proxy-to-character mappings.
- Character aliases and prior names.
- Manual message-attribution overrides.
- Channel purpose labels.
- OOC/RP channel classification.
- Scene boundaries and names.
- Arc membership.
- Fictional chronology.
- Character and player notes.
- Tags and content warnings.
- Reader display preferences.
- Search aliases.
- Redaction/display preferences for future public copies.

Mutable mappings must have:

- A stable record ID.
- Creation time.
- Last-updated time.
- Optional source/reference.
- Confidence/status.
- Notes field.
- Optional effective date range.
- Optional reviewer/creator identifier.

### 5. Provenance rule

Every record shown in the reader or search interface must answer:

```text
Where did this come from?
How was it imported?
What source file contains the original?
What interpretation, if any, was applied?
How certain is that interpretation?
```

The minimum provenance chain is:

```text
Message or document chunk
    ↓
Source file
    ↓
Import run
    ↓
Parser version
    ↓
Raw archive path and SHA-256 hash
```

For interpreted material:

```text
Character attribution / scene / arc
    ↓
Rule, mapping, or manual annotation
    ↓
Confidence and status
    ↓
Creation/review history
```

***

## II. Core Data Model

### 1. High-level model

The archive is not merely a Discord database and not merely a document library. It is a unified archive with several related content types.

```text
                         Archive Database
                                │
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
   Discord corpus         Document corpus          Editorial layer
   messages/channels      PDFs/DOCX/TXT            characters/scenes/arcs
        │                       │                        │
        └───────────────────────┼────────────────────────┘
                                ▼
                         Unified search layer
                                │
                                ▼
                         Reader and desktop GUI
```

### 2. Main table groups

The archive uses a strongly typed content model. Messages and documents are
different kinds of source content, and chunks and entries are document-specific
derived content. Shared relationships connect these typed records without
flattening them into one generic content table.

The final schema may add fields or supporting tables, but this separation is
the canonical conceptual model.

#### Canonical archive abstraction

The long-term schema uses five shared archive tables as its stable abstraction:

```text
archive_sources
archive_imports
archive_content
archive_entities
archive_links
```

These names are canonical across the architecture documents. The strongly
typed content model is represented within `archive_content` by distinct content
kinds and their typed records:

```text
messages
documents
document_chunks
document_entries
```

Supporting operational tables such as `import_errors`, `source_hashes`, and
`schema_migrations` remain implementation details around the five-table
abstraction rather than replacing it.

These are the primary content records:

- `messages` stores normalized Discord messages while preserving their
  source-level fields and original content.
- `documents` stores imported TXT, PDF, DOCX, and other document-level source
  records together with extracted text and metadata.
- `document_chunks` stores reproducible document segmentation for retrieval and
  processing.
- `document_entries` stores detected speaker or roleplay entries within a
  document. An entry is not assumed to be a chunk, and neither is required to
  map one-to-one with the other.

`messages` and `documents` remain strongly typed because their source
structures and lifecycle rules differ. They participate in the same archive
through the `archive_content` boundary and shared relationships.

#### Shared relationships

```text
archive_sources
archive_imports
archive_entities
archive_links
```

These tables connect typed content to provenance, identities, narrative
organization, and editorial interpretation:

- `archive_sources` identifies immutable source files or artifacts.
- `archive_imports` records an ingestion run and the parser/importer that produced
  derived records.
- `archive_entities` is a domain boundary containing explicit tables such as
  `players`, `characters`, `locations`, `arcs`, and `scenes`. It is not a
  polymorphic entity table.
- `archive_links` is reserved for content-to-content relationships, such as a
  message reply, message reference, quote, or document citation.

Content relationships and editorial relationships are different categories:

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
    scene -> character/player   scene_participants
```

Use explicit foreign keys and junction tables for editorial relationships.
They carry different semantics, constraints, and review workflows from
content-to-content links. Do not force both categories into `archive_links`.

The relationship tables should use stable typed-record references and retain
confidence, status, notes, and provenance where the relationship is an
interpretation rather than a source fact.

#### Discord structure

```text
servers
categories
channels
threads
messages
message_replies
attachments
embeds
reactions
mentions
```

#### Identity and attribution

```text
discord_identities
players
player_aliases
characters
character_aliases
proxy_identities
attribution_rules
message_attributions
```

#### RP chronology and editorial organization

```text
arcs
scenes
scene_messages
scene_channels
scene_participants
fictional_dates
locations
tags
message_tags
document_tags
```

#### Documents and extracted text

```text
documents
document_chunks
document_entries
document_metadata
```

#### Search and derived views

```text
messages_fts
document_chunks_fts
search_documents
reader_views
```

Not every one of these must be built immediately. The first implementation should focus on the smallest reliable core:

```text
sources
imports
servers
categories
channels
messages
discord_identities
documents
document_chunks
```

Then add attribution, scenes/arcs, and advanced search.

### 3. Relationship model

```text
Source File ──< archive_imports
  │              │
  │              ├── archive_content: Message
  │              │     └── Message Tags
  │              │
  │              └── archive_content: Document
  │                    ├── Document Chunk
  │                    ├── Document Entry
  │                    └── Document Tags
  │
  └── provenance for all derived content

archive_content: Messages / Documents / Chunks / Entries
  ├── archive_links (content-to-content only)
  └── explicit editorial tables
        ├── message_attributions
        ├── scene_messages
        └── scene_participants
```

Identity and interpretation are separate:

```text
Discord Identity ──► Player ──► Character
        │                             ▲
        │                             │
        └── Webhook / Proxy ──────────┘

Message or Document Entry ──► Attribution ──► Entity
```

This permits:

- One player to portray multiple characters.
- Multiple Discord accounts/names to correspond to one player.
- A bot/webhook/proxy to represent a character.
- A character to have multiple aliases or proxy identities.
- Some messages to remain unresolved.
- Later corrections without rewriting source evidence.

### 4. Discord location model

Discord “location” should be modeled as a hierarchy:

```text
Server
  └── Category
        └── Channel
              └── Thread
                    └── Message
```

Not every channel belongs to a category, and not every message belongs to a thread. The database must allow nullable category/thread relationships.

Suggested metadata:

#### `servers`

```text
server_id
discord_server_id
name_original
name_display
source_id
first_seen_at
last_seen_at
```

#### `categories`

```text
category_id
discord_category_id
server_id
name_original
position_original
```

#### `channels`

```text
channel_id
discord_channel_id
server_id
category_id nullable
name_original
channel_type
topic_original nullable
position_original nullable
is_ooc nullable
is_rp nullable
```

#### `threads`

```text
thread_id
discord_thread_id
parent_channel_id
name_original
thread_type
archived_at nullable
```

#### `messages`

```text
message_id
discord_message_id
source_id
channel_id
thread_id nullable
author_identity_id nullable
webhook_id_original nullable
timestamp_original
timestamp_utc
timestamp_local_display nullable
content_original
content_search
reply_to_discord_message_id nullable
raw_message_json
```

`content_original` must retain the exported message text. `content_search` may normalize whitespace, markup, mention forms, and Unicode variants for search, but it must never replace the original text.

### 5. Documents model

Documents should use the same provenance approach as Discord files.

```text
Document Source File
    ↓
Document Record
    ↓
Extracted Text
    ↓
Chunks
    ↓
FTS Search Index
```

Suggested core fields:

```text
documents
  document_id
  source_id
  title_original
  title_display
  document_type
  extracted_text
  extraction_status
  extraction_method
  created_at_original nullable
  metadata_json

document_chunks
  chunk_id
  document_id
  chunk_index
  heading_path nullable
  content_original
  content_search
  start_offset
  end_offset
```

Documents and Discord messages should ultimately participate in a unified reader/search experience, but they do not need to be forced into the same physical table.

***

## III. Identity, Player, Character, and Proxy Modeling

### 1. Fundamental distinction

The archive must maintain the following distinction:

> **Discord identity ≠ player ≠ character.**

```text
Discord Account / Username
          │
          ▼
        Player
          │
     ┌────┴────┐
     ▼         ▼
Character A  Character B
```

For proxy-based roleplay:

```text
Webhook / Tupperbox Proxy
          │
          ▼
      Character
          │
          ▼
        Player
```

A message may show a Discord account, a bot, a webhook, a Tupperbox persona, or an unresolved identity. Those are source-level identities. A player and a character are editorial/archive-level identities.

### 2. Discord identity

A `discord_identity` represents what Discord/export data says authored a message.

Suggested fields:

```text
identity_id
discord_user_id nullable
webhook_id nullable
identity_type
username_original
display_name_original
avatar_url_original nullable
is_bot
raw_identity_json
first_seen_at
last_seen_at
```

Possible `identity_type` values:

```text
user
bot
webhook
system
unknown
```

A webhook should not automatically become a player. It may represent:

- A character proxy.
- A system/bot message.
- An external integration.
- An unknown source.

### 3. Player

A `player` represents the person or archival participant behind one or more Discord identities and potentially one or more characters.

Suggested fields:

```text
player_id
display_name
privacy_name nullable
notes nullable
status
created_at
updated_at
```

A player may have aliases:

```text
player_aliases
  player_alias_id
  player_id
  alias
  alias_type
  effective_start nullable
  effective_end nullable
```

Examples of alias types:

```text
discord_username
discord_display_name
historical_name
preferred_name
ooc_name
```

### 4. Character

A `character` represents an in-world roleplay persona.

Suggested fields:

```text
character_id
canonical_name
display_name
description nullable
status
notes nullable
created_at
updated_at
```

A character may have aliases:

```text
character_aliases
  character_alias_id
  character_id
  alias
  alias_type
  effective_start nullable
  effective_end nullable
```

Examples:

```text
canonical
nickname
title
former_name
proxy_name
abbreviation
```

### 5. Proxy identity

A `proxy_identity` describes a Tupperbox/webhook/proxy pattern that may identify a character.

Suggested fields:

```text
proxy_identity_id
character_id nullable
webhook_id nullable
proxy_name
proxy_avatar_url nullable
prefix_pattern nullable
suffix_pattern nullable
effective_start nullable
effective_end nullable
source
confidence
notes
```

A proxy identity can be linked to a character with a confidence level but should not mutate raw message data.

### 6. Attribution model

A message attribution is an explicit claim that a particular message should be displayed or searched as a certain character and/or player.

```text
messages
    │
    └── message_attributions
          ├── character
          ├── player
          ├── source/method
          ├── confidence
          └── audit details
```

Suggested fields:

```text
message_attribution_id
message_id
character_id nullable
player_id nullable
attribution_rule_id nullable
method
confidence
status
created_at
updated_at
reviewed_at nullable
notes nullable
```

Suggested `method` values:

```text
exact_discord_identity
exact_webhook
exact_proxy_name
channel_context
manual_override
imported_mapping
llm_suggestion
unknown
```

Suggested `confidence` values:

```text
confirmed
reviewed
probable
inferred
unresolved
rejected
```

Suggested `status` values:

```text
active
superseded
needs_review
rejected
```

### 7. Attribution rules

The system should support reusable rules where possible.

Examples:

```text
Webhook 123456789012345678 → Character Winter
Discord user 987654321098765432 → Player A
Proxy name "Captain Voss" → Character Elena Voss
Messages in #winter-rp from user X during a defined period → probable Character Winter
```

Suggested fields:

```text
attribution_rules
  attribution_rule_id
  rule_type
  match_value
  character_id nullable
  player_id nullable
  effective_start nullable
  effective_end nullable
  confidence
  source
  notes
  enabled
```

A manual override must always take priority over an automatic or inferred rule.

### 8. Identity change lifecycle examples

These examples define lifecycle behavior, not additional identity types. They
assume `effective_start` is inclusive and `effective_end` is exclusive, both in
UTC. Those fields describe when a fact was true in the archive's subject matter.
Audit history describes when the archive recorded or corrected that fact. A
late correction therefore keeps its real-world effective dates and gets a new
audit timestamp; the two timelines must not be confused.

For versioned claims, do not overwrite the old row in place. Close its effective
interval, change its status, and retain it in audit history. `superseded` means
that a newer claim replaces this claim; it does not mean the old claim never
existed. A normal end to a true relationship should use an ended/inactive status
appropriate to that table, not `superseded`.

#### Example 1: Player A changes their Discord username

At first, Discord user ID `123` is recorded as username `winter_writer` and is
linked to Player A. Later, the same Discord user ID appears as
`winter-archive`.

Records that change:

- Keep the `discord_identity` for Discord user ID `123`; the stable user ID is
  the identity key, not the username.
- End the `winter_writer` row in a `discord_identity_aliases` (or equivalent
  identity-name history) table and create an active `winter-archive` alias with
  a new effective interval. If the model stores one mutable current username on
  `discord_identity`, update that current value but still retain both historical
  observations elsewhere.
- Keep the Discord-identity-to-Player-A link unchanged.
- Do not edit source message author fields or existing message attributions.
  The exported username remains evidence of what each message showed then.
- Add an audit event recording the old and new names, when the change took
  effect if known, when it was recorded, and the evidence/source.

This exposes a schema need: player aliases alone are insufficient because a
Discord username belongs to a Discord identity and can change independently of
the person's archive-level name.

#### Example 2: Player A stops playing Winter; proxy/webhook changes hands

Player A portrays Winter through Discord user `123` and webhook/proxy `W-1`.
On 2025-04-01, Player A stops playing Winter and Player B takes over. The
webhook is replaced with `W-2`; later, the old proxy name `Winter` is reused for
another character.

Records that change:

- End the Player-A-to-Winter portrayal relationship at 2025-04-01. Create a new
  Player-B-to-Winter relationship beginning at that instant. These must be
  separate, temporal relationship rows (for example, `player_character_roles`),
  not one mutable `player_id` field on `characters`.
- End the old webhook/proxy-to-Winter mapping at the handoff and create a new
  mapping for `W-2` to Winter from its actual start date. Do not change the old
  mapping's webhook ID to `W-2`.
- If proxy name `Winter` is later reused for another character, close the
  Winter-name mapping and create a separate mapping to the other character for
  the later interval. The name string itself is not a stable proxy identity.
- Keep message attributions before 2025-04-01 linked to Player A/Winter, and
  attribute later messages using the evidence/rule active at their message time.
  Do not bulk-change old attributions merely because the current player changed.
- Audit each relationship end and replacement separately, including the known
  effective date and the date/reason the archive recorded it.

This scenario requires temporal player-character and proxy-character links. A
single current owner or current character on either entity cannot represent the
handoff or safe proxy reuse.

#### Example 3: Character Winter is renamed to Frost

The in-world character remains the same persona; only its name changes on
2025-06-10.

Records that change:

- Keep the same `character_id`; update its current canonical/display name to
  `Frost`.
- End the `Winter` character-alias interval at 2025-06-10 and create/activate
  `Frost` as the current canonical name or alias from that date.
- Keep message attributions linked to the same `character_id`. Historical
  source text and proxy names remain unchanged.
- Record the rename in audit history, including the former and new name and the
  effective date.

If `Winter` and `Frost` are actually different personas, this is not a rename;
use a new character identity and a relationship such as successor/related-to.

#### Example 4: Character identities merge, then a character is split

The archive discovers that character records `C-10` and `C-22` were duplicates.
Later it discovers that a different record, `C-30`, had incorrectly combined
two distinct personas, now identified as `C-31` and `C-32`.

For the merge:

- Keep `C-10` and `C-22` as durable IDs; do not delete either or reuse an ID.
- Choose a surviving canonical record, for example `C-10`. Mark `C-22`
  `superseded` and add an explicit `C-22 -> C-10` merge/successor link with
  reason, evidence, and audit history.
- Do not rewrite every historical attribution from `C-22` to `C-10`. Preserve
  the original claim and let current views optionally resolve the successor.

For the split:

- Keep `C-30` as a historical record and mark it `superseded` by a split; create
  new IDs `C-31` and `C-32` and record both successor links.
- Do not guess which old messages belong to which new character. Add corrected
  message-attribution versions only where evidence supports a target; leave
  unresolved messages unresolved or flagged for review.
- Retain each prior attribution as `superseded`, with the replacement
  attribution(s), reason, reviewer, and recorded time in audit history.

This requires lineage capable of one-to-one merges and one-to-many splits. A
single `superseded_by_character_id` column handles a merge but cannot represent
one old character splitting into two successors; use a relationship table if
both operations must be represented uniformly.

#### Example 5: Webhook reuse reveals an earlier wrong attribution

Webhook `W-9` was assumed to represent Winter from January through March. Review
of exported proxy names shows it represented Winter only in January; the
webhook was reused for Raven from February onward. Some February messages had
already been attributed to Winter.

Records that change:

- End/supersede the `W-9 -> Winter` mapping at February 1 and create a
  `W-9 -> Raven` mapping from February 1. The two intervals must not overlap
  unless the evidence explicitly supports ambiguity.
- Preserve the original February message-attribution rows, but mark the mistaken
  Winter claims `superseded`. Create replacement Raven attributions only for
  messages verified to be Raven; uncertain messages become `needs_review` or
  unresolved.
- Record the correction as learned now, while preserving the mapping's actual
  effective interval beginning February 1. Store who corrected it, why, and the
  evidence in audit history.
- Do not change the raw message's `webhook_id`, author snapshot, or content.

This distinguishes a mapping change from a correction to already-created
attributions. The audit trail should preserve both the original mistaken claim
and the corrected claim rather than making the correction appear to have been
known at the time.

### 9. LLM policy

LLM-assisted attribution may be useful later, but it must be advisory only.

The LLM may:

- Suggest likely character mappings.
- Identify messages that appear inconsistent with current mappings.
- Suggest scene boundaries.
- Extract likely character names from documents.
- Rank likely matches for human review.

The LLM must not:

- Silently alter historical message data.
- Apply character mappings without a reviewable record.
- Replace manual or confirmed mappings.
- Turn uncertain inferences into asserted facts.
- Become the only source of a factual claim.

```text
LLM suggestion
    ↓
Review queue
    ↓
Human accepts / edits / rejects
    ↓
Recorded attribution or annotation
```

***

## IV. Scenes, Arcs, Locations, and Chronology

### 1. Multiple chronological dimensions

The archive must support more than one timeline.

```text
Discord chronology
    When messages were posted.

Channel chronology
    The order of messages in one Discord channel/thread.

Character chronology
    Messages attributed to a character, ordered by Discord time.

Player chronology
    Messages associated with a player, ordered by Discord time.

Scene chronology
    Editorially grouped messages forming a scene.

Arc chronology
    Editorially grouped scenes forming a larger story arc.

Fictional chronology
    In-world dates/events, where known.
```

Do not assume that Discord posting order is the same as in-world time.

### 2. Arc model

An arc is a higher-level editorial grouping.

```text
Arc
  ├── Scene 1
  ├── Scene 2
  ├── Scene 3
  └── Scene 4
```

Suggested fields:

```text
arcs
  arc_id
  title
  summary nullable
  discord_start_at nullable
  discord_end_at nullable
  fictional_start nullable
  fictional_end nullable
  status
  notes nullable
```

### 3. Scene model

A scene is a curated set of messages, potentially spanning multiple channels and time ranges.

Suggested fields:

```text
scenes
  scene_id
  arc_id nullable
  title
  summary nullable
  discord_start_at nullable
  discord_end_at nullable
  fictional_start nullable
  fictional_end nullable
  location_id nullable
  confidence
  status
  notes nullable
```

A scene should relate to messages through a join table:

```text
scene_messages
  scene_id
  message_id
  sequence_override nullable
  inclusion_reason nullable
```

Do not place only a start/end timestamp on a scene if it crosses channels or contains exclusions. Explicit message membership is more reliable.

### 4. Locations

Locations may mean two different things:

1. **Discord location:** server/category/channel/thread.
2. **Fictional location:** an in-world place or setting.

Keep them separate.

```text
Discord location:
  Server → Category → Channel → Thread

Fictional location:
  World → Region → Settlement → Site → Scene
```

Suggested `locations` fields:

```text
location_id
canonical_name
location_type
parent_location_id nullable
description nullable
notes nullable
```

Possible `location_type` values:

```text
world
continent
nation
region
city
district
building
room
battlefield
unknown
```

A scene may have one primary fictional location and optional additional location tags.

### 5. Scene participants

Scene participants should be explicit annotations, not merely keyword matches.

```text
scene_participants
  scene_id
  character_id nullable
  player_id nullable
  participation_type
  confidence
  notes nullable
```

Possible `participation_type` values:

```text
speaking
present
mentioned
narrated
offscreen
unknown
```

This eventually supports questions such as:

```text
Show scenes involving Winter and Faeya.
Show all arcs in which Character A appeared.
Show scenes set in the marketplace.
```

***

## V. SQLite Modeling and Database Rules

### 1. SQLite role

SQLite is the authoritative local **index, metadata store, and research database**. It is not a replacement for the raw archive.

```text
01_Raw is preservation evidence.
SQLite is searchable, structured derived knowledge.
Generated reader files are presentation outputs.
```

The database must be rebuildable from:

- Raw files.
- Import manifests.
- Mapping/configuration files.
- Editorial annotations, if stored separately or backed up.

### 2. Database rules

```text
[ ] Enable foreign keys on every connection.
[ ] Use transactions for each import.
[ ] Store Discord IDs as TEXT.
[ ] Store normalized timestamps in ISO-8601 UTC form.
[ ] Preserve raw source text separately from normalized search text.
[ ] Use migration files for schema changes.
[ ] Keep imports idempotent.
[ ] Do not silently discard malformed input.
[ ] Record warnings and errors.
[ ] Maintain a schema version.
[ ] Verify integrity and foreign keys regularly.
[ ] Back up before applying migrations.
```

SQLite foreign-key enforcement must be enabled on each connection; defining foreign keys in schema alone is not enough. [sqlite](https://sqlite.org/foreignkeys.html)

### 3. Schema migrations

Schema changes must be explicit, ordered, and reversible where practical.

```text
migrations/
  001_initial_schema.sql
  002_add_sources_and_imports.sql
  003_add_discord_tables.sql
  004_add_documents.sql
  005_add_attribution.sql
  006_add_scenes_and_arcs.sql
  007_add_fts.sql
```

Track applied migrations:

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);
```

Migration rules:

- Never edit a migration that has already been applied to a real archive.
- Create a new migration for corrections.
- Test migrations against an empty database and a representative old database.
- Back up before migration.
- Record migration success/failure in logs.
- Avoid manual schema changes through a database GUI unless the equivalent migration is committed immediately.

### 4. Integrity checks

Provide commands or GUI actions for:

```text
Initialize database.
Apply migrations.
Check schema version.
Check database integrity.
Check foreign keys.
List import failures.
List unresolved attributions.
Rebuild FTS indexes.
Back up database.
Restore database.
Verify backup.
```

At minimum, run:

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

after migrations, significant imports, and restores.

### 5. Database access layer

The GUI should not contain raw SQL or parsing logic.

```text
GUI
  ↓
Application Services
  ├── Import Service
  ├── Archive Service
  ├── Attribution Service
  ├── Search Service
  ├── Reader Service
  └── Backup Service
  ↓
Repository / Data Access Layer
  ↓
SQLite
```

This keeps the application testable and lets CLI commands, automated tests, and the GUI all use the same archive logic.

***

## VI. Search Language

### 1. Search goals

The search system must support:

- Roleplay content search.
- Exact phrase search.
- Character search.
- Player search.
- Raw Discord author search.
- Webhook/proxy search.
- Channel/category/server search.
- Date-range search.
- Scene/arc search.
- Attachment/document search.
- Boolean combinations.
- Results that can open directly in reader context.

Search must always preserve a distinction between:

```text
Raw Discord author
Player
Character
```

For example:

```text
author:OldDiscordName
player:"Player A"
character:Winter
```

are three different kinds of query.

### 2. Search syntax, version 1

#### Plain terms

```text
winter
oblivion
marketplace
```

Searches normalized full-text content and selected aliases.

#### Exact phrases

```text
"God Shattering Star"
"the final encounter"
```

#### Boolean operators

```text
Winter AND Oblivion
Winter OR Ultima
Winter NOT Faeya
(Winter OR Ultima) AND Oblivion
```

Operator precedence should be:

```text
1. Parentheses
2. NOT
3. AND
4. OR
```

#### Field filters

```text
character:Winter
player:"Player A"
author:OldDiscordName
proxy:"Captain Voss"
channel:marketplace
category:"Main Story"
server:"Old RP Server"
scene:"The Final Encounter"
arc:"Final War"
location:marketplace
source:discord
type:document
has:attachment
```

#### Date filters

```text
after:2021-01-01
before:2023-01-01
on:2022-06-14
year:2022
```

Document the exact boundary rules:

```text
after:2022-01-01
  means timestamp >= 2022-01-01T00:00:00 in archive timezone

before:2023-01-01
  means timestamp < 2023-01-01T00:00:00 in archive timezone
```

#### Examples

```text
character:Winter AND "silver compass"

player:"Player A" AND channel:marketplace
after:2022-06-01 before:2022-07-01

arc:"Final War" AND participant:Faeya

author:OldDiscordName AND has:attachment

type:document AND "character sheet"
```

#### 3. Search implementation model

The query is parsed rather than passed directly to SQL.

```text
User query
  ↓
Tokenizer
  ↓
Parser
  ↓
Abstract syntax tree
  ↓
Compiler
  ↓
Parameterized SQLite SQL + FTS5 query
  ↓
Results
```

Example:

```text
character:Winter AND ("silver compass" OR map)
```

Possible AST:

```text
AND
├── FIELD(character, "Winter")
└── OR
    ├── PHRASE("silver compass")
    └── TERM("map")
```

The compiler should turn:

- Text terms and phrases into FTS5 expressions.
- Field values into parameterized SQL predicates.
- Dates into validated timestamp conditions.
- Character/player filters into joins or indexed lookup conditions.

Never concatenate untrusted input into SQL.

### 4. FTS5 role

SQLite FTS5 provides fast full-text search. Use it for message text, document chunks, aliases, attachment names, embed text, and optionally titles/scene summaries. [medium](https://medium.com/@johnidouglasmarangon/full-text-search-in-sqlite-a-practical-guide-80a69c3f42a4)

FTS5 does not replace structured filtering. A good search query combines:

```text
FTS5 text search
    +
structured metadata filters
    +
attribution joins
    +
date/channel/scene constraints
```

### 5. Search result model

Every result should provide enough context to verify it.

```text
Character: Winter
Player: Player A
Raw author: Tupperbox webhook / OldDiscordName
Channel: #battlefield
Category: Main Story
Discord timestamp: 2023-08-17 21:44
Arc: Final War
Scene: The Final Encounter
Attribution: reviewed
Source: Discord export / message ID
```

Actions:

```text
Open in Reader
Open source context
Copy message link/identifier
Show attribution details
Show raw source metadata
```

***

## VII. Archive Invariants and Guarantees

These invariants are non-negotiable properties of the archive. They constrain
ingestion, schema design, interpretation, search, and application architecture.

1. Raw files are never modified by the application.

2. A logical source is identified independently from its immutable captures; a
  capture is identified by its SHA-256 and retains its archive-relative path
  and declared scope. A changed hash creates a new capture, never an overwrite.

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

***

## VIII. Reader Model

### 1. Reader principles

The reader is not a rewritten wiki. It is a contextual way to read source-derived content.

Every reader view should allow the user to answer:

```text
Who is speaking?
Who is the raw source author?
What character attribution was applied?
Which channel/thread did this come from?
When was it posted?
Which raw export contains it?
What scene/arc annotations apply?
```

### 2. Reader views

#### Channel reader

```text
Server
  Category
    Channel
      Messages in original Discord chronology
```

#### Category reader

```text
Category
  Multiple included channels
  Chronological interleaving with channel separators
```

#### Character reader

```text
Character
  All attributed appearances
  Grouped by year, arc, scene, or channel
```

#### Player reader

```text
Player
  Messages from associated Discord identities
  Characters portrayed
  OOC/RP differentiation where available
```

#### Scene reader

```text
Arc
  Scene
    Ordered included messages
    Participants
    Locations
    Source channels
    Discord and fictional dates
```

#### Document reader

```text
Document
  Original file metadata
  Extracted text
  Chunks/headings
  Related characters/scenes/tags
```

### 3. Source navigation

Every reader item should have a route back to source evidence:

```text
Message
  ↓
Channel/thread
  ↓
Import run
  ↓
Source file
  ↓
Raw JSON/HTML/document
```

The generated reader may show a friendly name, but it must preserve enough identifiers to find the original exported content.

***

## IX. GUI Architecture

Implement the application layers in this order:

```text
SQLite
  ↓
Application services
  ↓
Search
  ↓
Reader
  ↓
GUI
  ↓
Startup synchronization
  ↓
Windows packaging
```

### 1. Architectural diagram

```text
                 ┌────────────────────┐
                 │        GUI         │
                 │ Home / Archive /   │
                 │ Search / Reader /  │
                 │ Settings           │
                 └─────────┬──────────┘
                           │
                 ┌─────────▼──────────┐
                 │ Application Services│
                 ├────────────────────┤
                 │ Import Service     │
                 │ Archive Service    │
                 │ Search Service     │
                 │ Reader Service     │
                 │ Attribution Service│
                 │ Backup Service     │
                 └─────────┬──────────┘
                           │
                 ┌─────────▼──────────┐
                 │ Repository / DAL   │
                 └─────────┬──────────┘
                           │
                 ┌─────────▼──────────┐
                 │ SQLite Archive DB   │
                 └─────────┬──────────┘
                           │
                 ┌─────────▼──────────┐
                 │ 01_Raw Source Files │
                 └────────────────────┘
```

### 2. Core GUI screens

#### Home

- Archive health/status.
- Database schema version.
- Last successful import.
- Files awaiting import.
- Recently opened content.
- Recent searches.
- Import warnings.
- Unresolved character-attribution count.
- Database backup status.

#### Archive browser

- Servers.
- Categories.
- Channels.
- Threads.
- Documents.
- Characters.
- Players.
- Arcs.
- Scenes.
- Locations.
- Tags.

#### Search

- Search bar.
- Optional advanced syntax help.
- Date/channel/character filters.
- Search result snippets.
- “Open in Reader.”
- Display of raw author versus attributed character.
- Query errors with useful messages.

#### Reader

- Contextual message/document display.
- Channel/category breadcrumbs.
- Character and player badges.
- Scene/arc context.
- Source/provenance panel.
- Message navigation.
- Copy/export options.
- Optional raw metadata panel.

#### Attribution review

- Unresolved messages.
- Suggested matches.
- Existing rule/mapping view.
- Accept/edit/reject controls.
- Confidence and notes.
- Audit history.

#### Settings

- Raw archive directory.
- SQLite database directory.
- Backup directory.
- Import behavior.
- Archive timezone.
- Display/redaction preferences.
- Optional LLM configuration later.
- Logging level.
- Portable versus installed mode settings.

### 3. Startup synchronization

The application should support incremental synchronization.

```text
Launch application
    ↓
Locate configured raw archive
    ↓
Scan source files
    ↓
Compare logical sources, scopes, capture hashes, and import history
  ├── No changes ─────────► Open Home
  ↓ Changes found
Import changed sources
  ↓
Validate imports and database integrity
  ↓
Rebuild affected indexes
  ↓
Record results and warnings
  ↓
Open Home
```

The application must not silently import malformed files or hide import errors. It should show progress and preserve a readable import report.

***

## X. Windows Execution and Packaging

### 1. Packaging goal

The final application should run on Windows as a local executable without requiring the user to install Python separately.

Packaging is a late-stage deliverable. It should happen after ingestion, schema migrations, search, reader behavior, backup, and error handling are stable.

### 2. PyInstaller

PyInstaller is an appropriate initial packaging choice because it bundles the Python interpreter and required dependencies so the application can run without a separate Python installation. [pyinstaller](https://www.pyinstaller.org/)

Build the Windows package on Windows.

Start with a **one-folder** build:

```text
dist/
  DiscordRPArchive/
    DiscordRPArchive.exe
    _internal/
    resources/
```

Use a one-folder build first because it is easier to debug, inspect logs, update resources, and handle data files than a single-file executable.

Move to `--onefile` only after the application is stable and only if distribution convenience is worth the trade-offs.

### 3. Application data directories

Keep application code separate from user/archive data.

Recommended installed-app layout:

```text
Application installation:
  C:\Program Files\DiscordRPArchive\
    DiscordRPArchive.exe
    bundled resources
    application code

User configuration and logs:
  %LOCALAPPDATA%\DiscordRPArchive\
    config.toml
    logs\
    cache\

User archive data:
  User-selected directory, for example:
  D:\RP-Archives\MyServerArchive\
    01_Raw\
    archive.sqlite3
    backups\
    output\
```

The raw archive should not be stored inside the executable installation directory.

### 4. Bundled resources versus user data

#### Bundled resources

These are application-owned and may be replaced on an application upgrade:

- Icons.
- Default templates.
- Static CSS.
- Help files.
- Built-in migration files.
- Application defaults.
- Bundled licenses.

#### User data

These must survive upgrades and must not be stored inside the install directory:

- Raw exports.
- SQLite database.
- Backups.
- User configuration.
- Mapping files.
- Reader output.
- User annotations.
- Logs.
- Search history, if retained.
- LLM models, if added later.

### 5. Configuration files

Use a human-readable configuration format such as TOML.

Example:

```toml
[archive]
raw_directory = "D:/RP-Archives/MyServerArchive/01_Raw"
database_path = "D:/RP-Archives/MyServerArchive/archive.sqlite3"
backup_directory = "D:/RP-Archives/MyServerArchive/backups"
timezone = "America/Los_Angeles"

[import]
auto_sync_on_startup = true
validate_before_import = true
stop_on_error = false

[reader]
show_raw_author = true
show_attribution_confidence = true

[logging]
level = "INFO"
```

Store a safe `config.example.toml` in Git. Keep real local configuration out of Git.

### 6. SQLite location

The SQLite database should usually live alongside the archive, but outside `01_Raw/`.

```text
D:\RP-Archives\MyServerArchive\
  01_Raw\
  archive.sqlite3
  backups\
  output\
  mappings\
```

This keeps one archive self-contained and portable while preserving the distinction between raw evidence and derived/index data.

Do not place the primary SQLite database under `Program Files`, where normal users may lack write permission.

### 7. Logs

Store logs outside the installation directory.

Suggested structure:

```text
%LOCALAPPDATA%\DiscordRPArchive\logs\
  application.log
  import.log
  migration.log
  error.log
```

Logs should record:

- Application version.
- Database schema version.
- Import start/end time.
- Source path.
- Hash.
- Number of records processed.
- Warnings.
- Errors with stack traces.
- Migration results.
- Backup/restore results.

Logs should avoid unnecessarily copying message contents unless verbose diagnostic mode is explicitly enabled.

### 8. Portable versus installed mode

Support two eventual modes.

#### Portable mode

```text
DiscordRPArchive/
  DiscordRPArchive.exe
  data/
    01_Raw/
    archive.sqlite3
    backups/
    config.toml
    logs/
```

Advantages:

- Easy to keep on an external drive.
- Easy to move or back up as one folder.
- Good for private/personal archival projects.

Disadvantages:

- Paths may change between computers.
- Large archives may make the portable folder cumbersome.
- Upgrading the application requires care.

#### Installed mode

```text
Program Files:
  Application executable and bundled resources.

User archive location:
  Selected separately by the user.
```

Advantages:

- Conventional Windows behavior.
- Easier application updates.
- Clear separation between app binaries and valuable user data.

Disadvantages:

- Requires configuration.
- The archive is not automatically in one portable folder.

For this project, start with **portable/development mode** and add installed mode later.

### 9. Application updates

Application updates must not overwrite:

- `01_Raw/`
- `archive.sqlite3`
- `backups/`
- `mappings/`
- user `config.toml`
- generated reader output unless deliberately regenerated
- logs

Upgrade flow:

```text
Back up database
    ↓
Install/update application binaries
    ↓
Launch application
    ↓
Run pending database migrations
    ↓
Verify integrity
    ↓
Open archive
```

If a migration fails, the application should stop and provide a recovery path rather than continue with a partially upgraded database.

### 10. Executable versus archive location

The executable and the archive must be considered separate things.

```text
Executable:
  The program used to access and process the archive.

Archive:
  The irreplaceable historical data and derived local database.
```

Recommended rule:

> The application may be reinstalled. The archive must survive application reinstall, update, crash, or replacement.

The executable can live in:

```text
C:\Program Files\DiscordRPArchive\
```

or a portable program folder.

The archive should live in a user-selected, backed-up location:

```text
D:\RP-Archives\MyServerArchive\
```

***

## XI. Backup, Recovery, and Privacy

### 1. Backup model

Back up both:

1. Raw archive files.
2. SQLite database plus mappings and configuration.

```text
Archive root
  ├── 01_Raw/
  ├── archive.sqlite3
  ├── mappings/
  ├── config.toml
  └── backups/
```

A database backup is not a replacement for raw-file backups, and raw-file backups are not a replacement for the database/annotation backup.

### 2. Backup requirements

```text
[ ] Scheduled or prompted database backup.
[ ] Backup rotation.
[ ] SQLite integrity verification.
[ ] Restore command/workflow.
[ ] Restore drill on a test copy.
[ ] Backup manifest with date and application/schema version.
[ ] Optional encryption decision.
[ ] Separate copy on another physical device or trusted encrypted storage.
```

Python’s `sqlite3` module includes a backup API that can create a database backup while the source database is being used, making it suitable for a local backup command. [docs.python](https://docs.python.org/3/library/sqlite3.html)

### 3. Privacy

This archive may contain personal discussions, usernames, historical social material, and roleplay content created by other people.

Decide explicitly:

- Whether backups are encrypted.
- Whether real player identities are replaced with archival pseudonyms.
- Whether public/shared reader exports omit OOC content.
- Whether attachments/media require separate privacy controls.
- Whether sensitive channels are excluded from indexing or display.
- Whether logs contain content or only technical metadata.
- Whether Git contains only code and sanitized fixtures.

***

## XII. Non-Goals for Early Development

The following should not block the archival foundation:

- A polished visual theme.
- Full automatic character attribution.
- Automatic scene detection.
- Automatic fictional chronology reconstruction.
- Semantic/vector search.
- Local LLM integration.
- Public web hosting.
- Multi-user synchronization.
- Cloud dependence.
- Packaging as a single `.exe`.
- Complex embeddings or RAG.

The early milestone is narrower:

> **All raw sources can be deterministically ingested into one validated local SQLite archive, and every indexed record is traceable to its original source file.**

After that is true, the project can safely grow into a searchable reader, a character/scene research tool, a Windows application, and eventually a local RAG system.
