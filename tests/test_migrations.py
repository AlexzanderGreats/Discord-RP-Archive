from __future__ import annotations

import sqlite3
import unittest

from archive.migrations.runner import run_migrations


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE imports (
                import_id INTEGER PRIMARY KEY, source_file TEXT NOT NULL, source_sha256 TEXT NOT NULL UNIQUE,
                server_name TEXT, server_id TEXT, category_name TEXT, category_id TEXT,
                channel_name TEXT, channel_id TEXT, imported_at_utc TEXT NOT NULL,
                source_message_count INTEGER NOT NULL, imported_message_count INTEGER NOT NULL,
                oldest_timestamp_utc TEXT, newest_timestamp_utc TEXT
            );
            INSERT INTO imports VALUES (1, 'export.json', 'abc123', 'Server', 's1', 'Category', 'c1',
                'Channel', 'ch1', '2026-01-01T00:00:00+00:00', 1, 1, NULL, NULL);
            INSERT INTO imports VALUES (2, 'export.json', 'def789', 'Server', 's1', 'Category', 'c1',
                'Channel', 'ch1', '2026-01-02T00:00:00+00:00', 1, 1, NULL, NULL);
            CREATE TABLE messages (
                row_id INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, message_type TEXT,
                timestamp_original TEXT, timestamp_utc TEXT, timestamp_unix REAL, timestamp_edited TEXT,
                category_name TEXT, category_id TEXT, channel_name TEXT, channel_id TEXT, thread_name TEXT,
                author_discord_id TEXT, author_discord_name TEXT, author_display_name TEXT,
                author_nickname TEXT, author_is_bot INTEGER, webhook_id TEXT, character_id TEXT,
                character_name TEXT, player_id TEXT, player_name TEXT, content_original TEXT NOT NULL,
                content_search TEXT NOT NULL, reply_to_message_id TEXT, is_pinned INTEGER,
                call_ended_timestamp TEXT, attachments_json TEXT, embeds_json TEXT, stickers_json TEXT,
                reactions_json TEXT, mentions_json TEXT, inline_emojis_json TEXT, source_file TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, source_json TEXT NOT NULL
            );
            INSERT INTO messages VALUES (1, 'm1', NULL, '2026-01-01T00:00:00Z',
                '2026-01-01T00:00:00+00:00', 1767225600, NULL, 'Category', 'c1', 'Channel', 'ch1', NULL,
                'u1', 'user', 'User', NULL, 0, NULL, NULL, NULL, NULL, NULL, 'hello', 'hello', NULL, 0,
                NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'export.json', 'abc123', '{"content":"hello"}');
            CREATE TABLE documents (
                document_id INTEGER PRIMARY KEY, source_file TEXT NOT NULL, source_path TEXT NOT NULL,
                source_filename TEXT NOT NULL, source_extension TEXT NOT NULL, source_format TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, file_size_bytes INTEGER NOT NULL, title TEXT,
                document_type TEXT NOT NULL, status TEXT NOT NULL, created_at_source TEXT,
                modified_at_source TEXT, imported_at_utc TEXT NOT NULL, page_count INTEGER,
                paragraph_count INTEGER, character_count INTEGER, word_count INTEGER,
                extraction_status TEXT NOT NULL, extraction_error TEXT, content_text TEXT NOT NULL,
                content_search TEXT NOT NULL, metadata_json TEXT NOT NULL, pages_json TEXT,
                blocks_json TEXT, warnings_json TEXT, source_location_json TEXT
            );
            INSERT INTO documents VALUES (7, 'doc.txt', '01_Raw/doc.txt', 'doc.txt', '.txt', 'txt',
                'def456', 4, 'Doc', 'notes', 'historical', NULL, NULL, '2026-01-02T00:00:00+00:00',
                NULL, 1, 4, 1, 'success', NULL, 'text', 'text', '{}', NULL, NULL, '{}', NULL);
            CREATE TABLE document_chunks (
                chunk_id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
                page_start INTEGER, page_end INTEGER, paragraph_start INTEGER, paragraph_end INTEGER,
                section_title TEXT, content_text TEXT NOT NULL, content_search TEXT NOT NULL,
                source_location_json TEXT
            );
            INSERT INTO document_chunks VALUES (9, 7, 0, NULL, NULL, 1, 1, NULL, 'text', 'text', NULL);
            CREATE TABLE document_entries (entry_id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL);
            INSERT INTO document_entries VALUES (1, 7);
            """
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_migrations_preserve_legacy_rows_and_are_repeatable(self) -> None:
        self.assertEqual(run_migrations(self.connection), 22)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM legacy_document_entries").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 22)
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'legacy_%' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(
            tables,
            {
                "sources", "imports", "servers", "categories", "channels", "messages",
                "discord_identities", "documents", "document_chunks", "document_entries",
                "players", "characters", "character_aliases", "player_discord_identities",
                "player_characters", "message_attributions", "attribution_events", "schema_migrations",
                "arcs", "scenes", "scene_messages", "scene_participants",
                "boundary_detection_runs", "event_boundary_candidates",
                "boundary_detection_run_messages",
                "segmentation_runs", "candidate_spans", "candidate_span_messages",
                "candidate_reply_links", "segmentation_boundary_decisions",
                "identity_inference_runs", "provisional_identity_candidates",
                "identity_candidate_evidence", "identity_candidate_review_events",
                "identity_candidate_evidence_overrides",
                "attribution_confidence_runs", "attribution_confidence_candidates",
                "attribution_confidence_evidence",
                "attribution_review_events", "attribution_review_decisions",
                "attribution_review_evidence_overrides", "attribution_review_input_changes",
                "pipeline_runs",
            },
        )
        message_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(messages)")
        }
        self.assertNotIn("thread_id", message_columns)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        migrated_entry = self.connection.execute(
            "SELECT document_id, entry_index, content_text FROM document_entries"
        ).fetchone()
        self.assertEqual(tuple(migrated_entry), (7, 0, ""))
        self.assertEqual(run_migrations(self.connection), 22)

    def test_changed_applied_migration_is_rejected(self) -> None:
        run_migrations(self.connection)
        self.connection.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1"
        )

        with self.assertRaisesRegex(RuntimeError, "does not match its recorded checksum"):
            run_migrations(self.connection)

    def test_known_historical_migration_checksum_is_accepted(self) -> None:
        run_migrations(self.connection)
        self.connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            ("25439f9cbd3bb2e571d275b8a7121a2e47e998d1b4abb1a61e93723eaf3b3aa5",),
        )

        self.assertEqual(run_migrations(self.connection), 22)

    def test_candidate_migrations_upgrade_a_version_15_database(self) -> None:
        run_migrations(self.connection)
        self._drop_pipeline_migration()
        self.connection.execute("DROP TABLE attribution_review_evidence_overrides")
        self.connection.execute("DROP TABLE attribution_review_input_changes")
        self._drop_attribution_review_input_triggers()
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        self.connection.execute("DROP TABLE attribution_review_decisions")
        self.connection.execute("DROP TABLE attribution_review_events")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        self.connection.execute("DROP TABLE attribution_confidence_evidence")
        self.connection.execute("DROP TABLE attribution_confidence_candidates")
        self.connection.execute("DROP TABLE attribution_confidence_runs")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        self.connection.execute("DROP TABLE identity_candidate_evidence_overrides")
        self.connection.execute("DROP TABLE identity_candidate_review_events")
        self.connection.execute("DROP TABLE identity_candidate_evidence")
        self.connection.execute("DROP TABLE provisional_identity_candidates")
        self.connection.execute("DROP TABLE identity_inference_runs")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        self.connection.execute("DROP TABLE segmentation_boundary_decisions")
        self.connection.execute("DROP TABLE candidate_reply_links")
        self.connection.execute("DROP TABLE candidate_span_messages")
        self.connection.execute("DROP TABLE candidate_spans")
        self.connection.execute("DROP TABLE segmentation_runs")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 17")
        self.connection.execute("DROP TABLE event_boundary_candidates")
        self.connection.execute("DROP TABLE boundary_detection_run_messages")
        self.connection.execute("DROP TABLE boundary_detection_runs")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        self.connection.commit()

        self.assertEqual(run_migrations(self.connection), 22)
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'candidate_spans'"
            ).fetchone()
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'provisional_identity_candidates'"
            ).fetchone()
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'attribution_confidence_candidates'"
            ).fetchone()
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_review_migrations_upgrade_a_version_18_database(self) -> None:
        run_migrations(self.connection)
        self._drop_pipeline_migration()
        self.connection.execute("DROP TABLE attribution_review_evidence_overrides")
        self.connection.execute("DROP TABLE attribution_review_input_changes")
        self._drop_attribution_review_input_triggers()
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        self.connection.execute("DROP TABLE attribution_review_decisions")
        self.connection.execute("DROP TABLE attribution_review_events")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        self.connection.execute("DROP TABLE attribution_confidence_evidence")
        self.connection.execute("DROP TABLE attribution_confidence_candidates")
        self.connection.execute("DROP TABLE attribution_confidence_runs")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        self.connection.commit()

        self.assertEqual(run_migrations(self.connection), 22)
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'attribution_confidence_runs'"
            ).fetchone()
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'attribution_review_events'"
            ).fetchone()
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_hardening_migration_upgrades_version_19_directly(self) -> None:
        run_migrations(self.connection)
        self._drop_pipeline_migration()
        self.connection.execute("DROP TABLE attribution_review_evidence_overrides")
        self.connection.execute("DROP TABLE attribution_review_input_changes")
        self._drop_attribution_review_input_triggers()
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        self.connection.execute("DROP TABLE attribution_review_decisions")
        self.connection.execute("DROP TABLE attribution_review_events")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        self.connection.commit()

        self.assertEqual(run_migrations(self.connection), 22)
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM pragma_table_info('attribution_review_events') "
                "WHERE name = 'previous_state_json'"
            ).fetchone()
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def _drop_attribution_review_input_triggers(self) -> None:
        for trigger in (
            "attribution_review_alias_insert",
            "attribution_review_alias_update",
            "attribution_review_alias_delete",
            "attribution_review_player_identity_insert",
            "attribution_review_player_identity_update",
            "attribution_review_player_identity_delete",
            "attribution_review_player_character_insert",
            "attribution_review_player_character_update",
            "attribution_review_player_character_delete",
        ):
            self.connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    def _drop_pipeline_migration(self) -> None:
        self.connection.execute("DROP TRIGGER pipeline_runs_no_delete")
        self.connection.execute("DROP TRIGGER pipeline_runs_finalize_once")
        self.connection.execute("DROP TABLE pipeline_runs")
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 22")

    def test_recaptures_preserve_observations_and_advance_accepted_capture(self) -> None:
        from archive.provenance import (
            accept_import_run,
            current_accepted_source_id,
            finish_import_run,
            record_message_observation,
            register_source_capture,
            start_import_run,
        )

        run_migrations(self.connection)
        source_a = register_source_capture(
            self.connection,
            logical_source_key="discord:channel-export",
            source_kind="discord_json",
            source_path="01_Raw/export.json",
            sha256="capture-a",
            byte_size=42,
            scope_json='{"channel_id":"ch1"}',
            registered_at="2026-01-01T00:00:00+00:00",
        )
        import_a = start_import_run(
            self.connection,
            source_id=source_a,
            started_at="2026-01-01T00:01:00+00:00",
            importer_name="test",
            importer_version="1",
        )
        record_message_observation(
            self.connection,
            source_id=source_a,
            import_id=import_a,
            observation={
                "discord_message_id": "123",
                "content_original": "Hello",
                "content_search": "hello",
                "source_json": '{"content":"Hello"}',
            },
        )
        finish_import_run(
            self.connection,
            import_id=import_a,
            status="succeeded",
            finished_at="2026-01-01T00:02:00+00:00",
            records_seen=1,
            records_written=1,
        )
        accept_import_run(
            self.connection,
            import_id=import_a,
            accepted_at="2026-01-01T00:03:00+00:00",
        )
        self.assertEqual(current_accepted_source_id(self.connection, "discord:channel-export"), source_a)

        source_b = register_source_capture(
            self.connection,
            logical_source_key="discord:channel-export",
            source_kind="discord_json",
            source_path="01_Raw/export.json",
            sha256="capture-b",
            byte_size=49,
            scope_json='{"channel_id":"ch1"}',
            registered_at="2026-01-02T00:00:00+00:00",
        )
        import_b = start_import_run(
            self.connection,
            source_id=source_b,
            started_at="2026-01-02T00:01:00+00:00",
            importer_name="test",
            importer_version="1",
        )
        record_message_observation(
            self.connection,
            source_id=source_b,
            import_id=import_b,
            observation={
                "discord_message_id": "123",
                "content_original": "Hello, world",
                "content_search": "hello world",
                "source_json": '{"content":"Hello, world"}',
            },
        )
        finish_import_run(
            self.connection,
            import_id=import_b,
            status="succeeded",
            finished_at="2026-01-02T00:02:00+00:00",
            records_seen=1,
            records_written=1,
        )
        accept_import_run(
            self.connection,
            import_id=import_b,
            accepted_at="2026-01-02T00:03:00+00:00",
        )

        observations = self.connection.execute(
            """SELECT source_id, discord_message_id, content_original
               FROM messages WHERE discord_message_id = '123' ORDER BY source_id"""
        ).fetchall()
        self.assertEqual(
            [(row["source_id"], row["discord_message_id"], row["content_original"]) for row in observations],
            [(source_a, "123", "Hello"), (source_b, "123", "Hello, world")],
        )
        self.assertEqual(current_accepted_source_id(self.connection, "discord:channel-export"), source_b)
        self.assertEqual(
            self.connection.execute(
                "SELECT supersedes_source_id FROM sources WHERE source_id = ?", (source_b,)
            ).fetchone()[0],
            source_a,
        )

        stale_import = start_import_run(
            self.connection,
            source_id=source_a,
            started_at="2026-01-03T00:01:00+00:00",
            importer_name="test",
            importer_version="1",
        )
        finish_import_run(
            self.connection,
            import_id=stale_import,
            status="succeeded",
            finished_at="2026-01-03T00:02:00+00:00",
            records_seen=1,
            records_written=1,
        )
        with self.assertRaises(ValueError):
            accept_import_run(
                self.connection,
                import_id=stale_import,
                accepted_at="2026-01-03T00:03:00+00:00",
            )

        source_c = register_source_capture(
            self.connection,
            logical_source_key="discord:channel-export",
            source_kind="discord_json",
            source_path="01_Raw/export.json",
            sha256="capture-c",
            byte_size=50,
            scope_json='{"channel_id":"ch1"}',
            registered_at="2026-01-04T00:00:00+00:00",
        )
        failed_import = start_import_run(
            self.connection,
            source_id=source_c,
            started_at="2026-01-04T00:01:00+00:00",
            importer_name="test",
            importer_version="1",
        )
        finish_import_run(
            self.connection,
            import_id=failed_import,
            status="failed",
            finished_at="2026-01-04T00:02:00+00:00",
            records_seen=1,
            records_written=0,
        )
        with self.assertRaises(ValueError):
            accept_import_run(
                self.connection,
                import_id=failed_import,
                accepted_at="2026-01-04T00:03:00+00:00",
            )
        self.assertEqual(current_accepted_source_id(self.connection, "discord:channel-export"), source_b)

    def test_legacy_captures_share_logical_identity_when_path_and_scope_match(self) -> None:
        run_migrations(self.connection)
        keys = self.connection.execute(
            "SELECT logical_source_key FROM sources WHERE source_kind = 'discord_json' ORDER BY source_id"
        ).fetchall()
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0][0], keys[1][0])


if __name__ == "__main__":
    unittest.main()