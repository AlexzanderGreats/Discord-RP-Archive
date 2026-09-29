# Discord RP Archive Roadmap

Yes. Based on the **assessment itself**, plus the architecture you have subsequently established for the application, you are no longer simply trying to “finish an archive script.” You are building a **local RP archival/research application** whose raw corpus is immutable, SQLite is the authoritative index, the GUI is the user-facing layer, and an eventual LLM sits on top as a retrieval/research assistant.

The assessment puts your current state very clearly: the **ingestion foundation is functional**, but the reader, search, backup/recovery, unified schema, testing, and application layers are not finished. The existing database is valid but currently document-only: 114 documents, 9,030 chunks, 4,080 detected entries, and no Discord `messages`/`imports` tables yet.

## The roadmap I'd use

```text
                         DISCORD-RP ARCHIVE
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 0 — PROJECT FOUNDATION                                │
│ README • Git • dependencies • configuration • conventions   │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 1 — UNIFIED INGESTION                                 │
│ Discord JSON • TXT • PDF • DOCX • HTML • provenance         │
│ SHA-256 • duplicate detection • manifests • validation      │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 2 — DATABASE MATURITY                                  │
│ Unified schema • migrations • imports • messages • documents│
│ FTS5 • relationships • integrity checks                     │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 3 — ATTRIBUTION & RP MODEL                            │
│ Discord identity → player → character → proxy               │
│ Scenes • arcs • participants • chronology                   │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 4 — SEARCH                                             │
│ Basic search → Boolean → field filters → advanced queries   │
│ Exact result → source → Reader                              │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 5 — READER                                             │
│ Channel • category • character • player • scene • arc       │
│ Chronological navigation + provenance                       │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 6 — LOCAL APPLICATION                                  │
│ .EXE • startup synchronization • Home • Archive • Search   │
│ Reader • Settings • progress/status                         │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 7 — LLM / RAG                                         │
│ FTS + semantic retrieval → context → local LLM → citations │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE 8 — BACKUP / RELEASE                                  │
│ Automated backups • recovery • tests • packaging • docs     │
└──────────────────────────────────────────────────────────────┘
```

### Phase 0 — Make the project reproducible

This is boring, but it should happen **before the application gets much larger**.

The assessment found that there is currently no Git repository, no top-level README/setup guide, and no automated tests.

**Tasks:**

* [ ] Initialize Git.
* [ ] Create `.gitignore`.
* [ ] Create project README.
* [ ] Document directory structure.
* [ ] Document Python version/dependencies.
* [ ] Document how to build/rebuild the database.
* [ ] Document what `01_Raw`, `02_Processed`, etc. mean.
* [ ] Establish configuration rather than hard-coding paths.
* [ ] Decide what belongs in Git versus what must remain local.
* [ ] Establish a test framework.

**Definition of done:** someone can clone the project, install dependencies, understand the architecture, and reproduce the processing environment.

---

## Phase 1 — Finish ingestion

This is your **current immediate priority**.

The assessment specifically recommends importing and validating the Discord JSON corpus into the existing SQLite database.

Your ingestion pipeline should eventually look like:

```text
01_Raw
   │
   ├── Discord JSON ──────┐
   ├── HTML               │
   ├── TXT               ├──► Ingestion
   ├── PDF               │
   ├── DOCX              │
   └── Images ───────────┘
                           │
                           ▼
                     Validation
                           │
                           ▼
                     Normalization
                           │
                           ▼
                    SQLite archive
```

## Discord

* [ ] Finish `build_archive.py`.
* [ ] Parse the actual DiscordChatExporter schema.
* [ ] Preserve Discord IDs as strings.
* [ ] Preserve original timestamps.
* [ ] Store normalized timestamps.
* [ ] Preserve author objects.
* [ ] Preserve attachments/embeds/reactions/mentions.
* [ ] Preserve original JSON/provenance.
* [ ] Record source-file hashes.
* [ ] Detect duplicate imports.
* [ ] Record import manifests.
* [ ] Validate server/channel IDs against your server map.
* [ ] Handle malformed exports without silently accepting them.

The actual Discord export format you've examined already gives you guild, channel, date-range, export time, and message-level metadata, so the importer can be designed against the real schema rather than guessing.

## Other documents

Your document importer already handles TXT/PDF/DOCX and provenance reasonably well, but:

* [ ] Fix `--recursive` being effectively forced on.
* [ ] Fix `--dry-run` so it actually performs no database mutation.
* [ ] Make import failures return non-zero exit codes.
* [ ] Populate the currently incomplete report statistics.
* [ ] Decide how HTML is handled.
* [ ] Decide whether images need OCR/metadata extraction.

Those are explicitly identified defects in the assessment.

---

## Phase 2 — Unify the database

This is probably the most important architectural phase.

Right now you effectively have:

```text
Document system
      +
Discord system
```

You want:

```text
                 ARCHIVE DATABASE
                       │
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
   Discord          Documents       Metadata
   messages         chunks          relationships
```

The assessment specifically identifies the separate schemas/reporting conventions as a current weakness.

I'd establish a real schema along these lines:

```text
sources
imports
servers
categories
channels
threads
messages

authors
players
characters
proxy_identities

scenes
arcs
scene_participants

documents
document_chunks

attachments
embeds

metadata
```

And then:

```text
messages
   │
   ├── source
   ├── channel
   ├── author
   └── character attribution
```

while:

```text
documents
   │
   └── chunks
```

both ultimately participate in the same searchable archive.

## Also implement

* [ ] Schema version.
* [ ] Migration mechanism.
* [ ] Database initialization command.
* [ ] Integrity check command.
* [ ] Rebuild-index command.
* [ ] Import-status reporting.
* [ ] Duplicate detection.
* [ ] Foreign-key validation.

You do **not** want to reach version 0.9 and discover that changing one table requires manually rebuilding the entire database.

---

## Phase 3 — Build the actual RP model

This is where your archive stops being merely a Discord database.

The key insight from the earlier architecture is:

> **Discord identity ≠ player ≠ character.**

You need to be able to represent:

```text
Discord Account
       │
       ▼
     Player
       │
       ├──────────────┐
       ▼              ▼
   Character A    Character B
```

And:

```text
Webhook / Proxy
       │
       ▼
   Character
```

This is particularly important for your archive because one person can portray multiple characters, while a bot/webhook may represent a character without being a human player. Your earlier design explicitly calls for retaining the raw Discord author while applying character attribution separately.

## Build

* [ ] Player registry.
* [ ] Discord identity registry.
* [ ] Character registry.
* [ ] Proxy/webhook registry.
* [ ] Attribution mappings.
* [ ] Attribution confidence/status.
* [ ] Effective dates for mappings if necessary.
* [ ] Ability to correct an attribution without modifying raw messages.

That last part is critical:

```text
RAW MESSAGE
Author = Webhook 123

        ↓

ATTRIBUTION

Webhook 123 = Winter

        ↓

READER

Winter: "..."
```

If you later realize the webhook was actually another character, you change the mapping—not the historical message.

---

## Phase 4 — Chronology

You specifically want the archive to reconstruct RP history.

Don't make one giant timeline.

Support several dimensions:

```text
Discord chronology
Channel chronology
Character chronology
Player chronology
Scene chronology
Arc chronology
```

The architecture already identified the distinction between Discord chronology and fictional/RP chronology.

Then add:

## Scenes

```text
Arc
 ├── Scene
 │    ├── messages
 │    ├── channels
 │    └── participants
 └── Scene
```

### Eventually

```text
Arc I
 ├── Scene 1 — Arrival
 ├── Scene 2 — Encounter
 ├── Scene 3 — Battle
 └── Scene 4 — Aftermath
```

This is what eventually lets you ask:

> “Show me every scene in which Winter and Faeya interacted.”

rather than merely:

> “Find messages containing Winter and Faeya.”

---

## Phase 5 — Build Search

This is the first feature that will make the archive **feel useful**.

Start simple:

```text
Winter
```

Then:

```text
"God Shattering Star"
```

Then Boolean:

```text
Winter AND Oblivion
Winter OR Ultima
Winter NOT Faeya
```

Then grouped expressions:

```text
(Winter OR Ultima) AND Oblivion
```

Then field searches:

```text
character:Winter
player:Alex
channel:Arena
author:Username
```

Then dates:

```text
after:2021-01-01
before:2023-01-01
```

And eventually:

```text
character:Winter
AND
participant:Faeya
AND
arc:"Final War"
```

Your earlier search design already specifies this progression, including Boolean operators and field-specific searches.

## Search result should contain

```text
┌──────────────────────────────────────┐
│ Winter — #battlefield                │
│ 2023-08-17 21:44                     │
│                                      │
│ "...Winter raised his hand..."       │
│                                      │
│ Character: Winter                    │
│ Player: Alex                         │
│ Source: message 123456789            │
│                                      │
│ [Open in Reader]                     │
└──────────────────────────────────────┘
```

That last **Open in Reader** connection is important.

Search shouldn't terminate at search results.

---

## Phase 6 — Build the Reader

Now turn the data into something you can actually **read**.

You want:

## Channel reader

```text
# Battlefield

2023-08-17

Winter:
> ...

Faeya:
> ...

Winter:
> ...
```

### Character reader

```text
WINTER

All appearances
─────────────────
2021
2022
2023
2024
```

### Scene reader

```text
ARC III
└── Scene 14 — The Final Encounter

Participants:
Winter
Faeya
Oblivion
...

[Read Scene]
```

### Source provenance

Every message should ultimately be traceable back to:

```text
Message
 ↓
Channel
 ↓
Export
 ↓
Raw JSON
```

That preserves the archival property rather than turning your reader into a rewritten fan-wiki.

---

## Phase 7 — Turn it into the `.exe`

**Only now** would I make the polished GUI the center of development.

Your desired application architecture is essentially:

```text
                 ┌───────────────┐
                 │     GUI       │
                 └───────┬───────┘
                         │
              Application Services
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
    Archive           Search            Reader
       │                 │
       └────────┬────────┘
                ▼
             SQLite
                ▲
                │
           Ingestion
                ▲
                │
             01_Raw
```

And the application should have at least:

## Home

* Recently ingested
* Recently viewed
* Corpus browser
* Statistics

### Archive

* Servers
* Categories
* Channels
* Documents
* Characters
* Players
* Arcs

### Search

Your full advanced search system.

### Reader

Contextual reading of Discord/document material.

### LLM Chat

Later.

### Settings

* Raw archive location
* Database location
* Import behavior
* Backup behavior
* LLM configuration
* Memory/project-context settings

---

## Phase 8 — Startup synchronization

This is one of your more important application requirements.

You don't want:

> “Run the importer manually every time.”

You want:

```text
Launch .exe
     │
     ▼
Scan 01_Raw
     │
     ▼
Compare manifest / hashes
     │
     ├── Nothing changed ──► Open application
     │
     └── Changes detected
              │
              ▼
       Import / validate
              │
              ▼
          Re-index
              │
              ▼
         Open application
```

And the startup screen should tell you what's happening:

```text
Discord RP Archive

Scanning archive...
Found 14 new files
Found 2 modified files

Importing:
██████████████░░░░ 78%

Indexing...
```

The raw directory remains authoritative. The application merely **observes and indexes it**; it shouldn't rearrange or mutate your raw archive. That architecture was already established in the project discussion.

---

## Phase 9 — LLM/RAG

This is where the project becomes considerably more interesting.

Do **not** make the LLM the database.

Instead:

```text
User
 │
 ▼
Question
 │
 ├── FTS5
 ├── Metadata filters
 └── Semantic search
          │
          ▼
   Relevant archive material
          │
          ▼
        Llama
          │
          ▼
 Answer + source references
```

So you could eventually ask:

> “When did Winter first meet Faeya?”

or:

> “Find the scene where Winter realized that Faeya remembered the previous event.”

or:

> “Summarize Winter's relationship with Oblivion across the RP.”

The model researches the archive; it doesn't invent the archive's contents.

Your earlier architecture explicitly settled on **SQLite/FTS5 first, embeddings later, and Llama after the retrieval foundation exists**.

---

## Phase 10 — Backup, testing, and release

This is the final “make it trustworthy” layer.

The assessment specifically calls out the absence of backup/recovery infrastructure and automated tests.

## Tests

At minimum:

* [ ] JSON parsing
* [ ] malformed JSON
* [ ] timestamps
* [ ] Discord IDs
* [ ] duplicate detection
* [ ] SHA-256
* [ ] metadata overrides
* [ ] server-map validation
* [ ] database migrations
* [ ] FTS5 searches
* [ ] Boolean search parser
* [ ] character attribution
* [ ] reader navigation
* [ ] incremental synchronization

### Backup

```text
archive.sqlite3
      │
      ├── Backup 001
      ├── Backup 002
      └── Backup 003
```

Plus:

* [ ] SQLite integrity check.
* [ ] Backup rotation.
* [ ] Restore command.
* [ ] Backup verification.
* [ ] Recovery documentation.

And because this archive contains personal/roleplay material, the assessment correctly identifies the need to make an explicit decision about backup security, redaction, encryption, and version control.

---

## What I would **not** work on yet

This is probably the most useful distinction.

## Don't build yet

❌ Fancy GUI
❌ Llama integration
❌ Embeddings
❌ Character AI analysis
❌ Elaborate scene inference
❌ Automatic RP chronology reconstruction
❌ Beautiful HTML themes
❌ `.exe` packaging

### Build first

✅ Unified ingestion
✅ Unified SQLite schema
✅ Provenance
✅ Validation
✅ Incremental imports
✅ FTS5
✅ Tests
✅ Basic reader/search

Because otherwise you'll build a beautiful interface around an unstable backend.

---

## My actual current position

I'd describe the project like this:

| Component               | State                                                |
| ----------------------- | ---------------------------------------------------- |
| Raw archive             | 🟢 Established                                       |
| Discord exports         | 🟢 Established                                       |
| Document ingestion      | 🟢 Functional                                        |
| Provenance              | 🟢 Strong foundation                                 |
| SQLite                  | 🟡 Functional but incomplete                         |
| Discord database        | 🟡 Needs completion                                  |
| Unified schema          | 🔴 Not finished                                      |
| Character attribution   | 🔴 Not implemented                                   |
| RP chronology           | 🔴 Not implemented                                   |
| Reader                  | 🔴 Not implemented                                   |
| Search                  | 🟡 FTS foundation exists; application search doesn't |
| Advanced Boolean search | 🔴 Not implemented                                   |
| GUI                     | 🔴 Not implemented                                   |
| Startup synchronization | 🔴 Not implemented                                   |
| LLM/RAG                 | 🟡 Local Ollama Q&A for retrieved Discord messages; semantic/document retrieval pending |
| Backups/recovery        | 🔴 Not implemented                                   |
| Automated tests         | 🔴 Not implemented                                   |
| Packaging `.exe`        | 🔴 Not implemented                                   |
| Documentation           | 🔴 Needs work                                        |
| Git/version control     | 🔴 Needs work                                        |

So you're **not starting from scratch**. The assessment's characterization of the project as a “solid ingestion prototype, but not yet a complete archive application” is still the right description.

## The critical path

If I reduce the entire roadmap to the sequence I'd actually follow:

```text
1.  Git + README
        ↓
2.  Fix ingestion defects
        ↓
3.  Import Discord corpus
        ↓
4.  Unify database schema
        ↓
5.  Add migrations + integrity checking
        ↓
6.  Add tests
        ↓
7.  Implement character/player attribution
        ↓
8.  Implement FTS5 search
        ↓
9.  Implement advanced search syntax
        ↓
10. Build Reader
        ↓
11. Build chronology / scenes / arcs
        ↓
12. Build GUI
        ↓
13. Add startup synchronization
        ↓
14. Add backup/recovery
        ↓
15. Package .exe
        ↓
16. Add embeddings
        ↓
17. Add local Llama/RAG
        ↓
18. Polish
```

### And the immediate next milestone should be

> **“The entire raw corpus can be deterministically ingested into one validated SQLite archive, with every record traceable back to its original source.”**

Once **that** is true, everything you want afterward—Search, Reader, character browsing, chronology, the `.exe`, and eventually Llama—becomes a layer built on top of a stable foundation rather than another rewrite.

That's the major transition your project is approaching now: **from an ingestion prototype into an actual archival platform.** 🗃️🔎🧩
