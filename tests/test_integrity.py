from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive.provenance import register_source_capture, start_import_run
from src import build_archive
from src.build_archive import create_database, import_file


class DiscordImportIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.export_path = self.root / "channel.json"
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _write_channel_export(self) -> bytes:
        source = {
            "guild": {"id": "18446744073709551615", "name": "Server"},
            "channel": {
                "id": "9007199254740993123",
                "name": "Channel",
                "categoryId": "9007199254740993124",
                "category": "Category",
            },
            "messageCount": 1,
            "messages": [
                {
                    "id": "9007199254740993999",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "author": {
                        "id": "9007199254740993555",
                        "name": "Historical Name",
                        "isBot": False,
                    },
                    "content": "  Original  content\nExactly as exported.  ",
                }
            ],
        }
        self.export_path.write_text(json.dumps(source), encoding="utf-8")
        return self.export_path.read_bytes()

    def test_one_channel_export_preserves_evidence_ids_and_provenance(self) -> None:
        original_bytes = self._write_channel_export()
        expected_content = "  Original  content\nExactly as exported.  "

        result = import_file(self.connection, self.export_path)

        self.assertEqual(self.export_path.read_bytes(), original_bytes)
        self.assertEqual(hashlib.sha256(original_bytes).hexdigest(), result["sha256"])

        message = self.connection.execute(
            """SELECT m.*, s.sha256, i.status, i.accepted_at,
                      s.source_id AS capture_id, i.source_id AS run_source_id
               FROM messages m
               JOIN sources s ON s.source_id = m.source_id
               JOIN imports i ON i.import_id = m.import_id
               WHERE m.discord_message_id = ?""",
            ("9007199254740993999",),
        ).fetchone()
        self.assertIsNotNone(message)
        self.assertEqual(message["content_original"], expected_content)
        self.assertEqual(
            message["source_json"],
            json.dumps(
                json.loads(original_bytes)["messages"][0],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(message["sha256"], result["sha256"])
        self.assertEqual(message["capture_id"], message["source_id"])
        self.assertEqual(message["run_source_id"], message["source_id"])
        self.assertEqual(message["status"], "succeeded")
        self.assertIsNotNone(message["accepted_at"])

        id_columns = {
            "messages": ("discord_message_id", "author_discord_user_id_original"),
            "servers": ("discord_server_id",),
            "categories": ("discord_category_id",),
            "channels": ("discord_channel_id",),
            "discord_identities": ("discord_user_id",),
        }
        for table, columns in id_columns.items():
            declared_types = {
                row["name"]: row["type"].upper()
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for column in columns:
                self.assertEqual(declared_types[column], "TEXT", f"{table}.{column}")

        stored_ids = self.connection.execute(
            """SELECT m.discord_message_id, m.author_discord_user_id_original,
                      s.discord_server_id, c.discord_category_id, ch.discord_channel_id,
                      d.discord_user_id
               FROM messages m
               JOIN servers s ON s.first_seen_import_id = m.import_id
               JOIN categories c ON c.first_seen_import_id = m.import_id
               JOIN channels ch ON ch.first_seen_import_id = m.import_id
               JOIN discord_identities d ON d.first_seen_import_id = m.import_id"""
        ).fetchone()
        self.assertEqual(
            tuple(stored_ids),
            (
                "9007199254740993999",
                "9007199254740993555",
                "18446744073709551615",
                "9007199254740993124",
                "9007199254740993123",
                "9007199254740993555",
            ),
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_materialization_rolls_back_but_records_failed_run(self) -> None:
        original_bytes = self._write_channel_export()
        insert_observation = build_archive._insert_observation

        def insert_then_fail(*args: object, **kwargs: object) -> int:
            insert_observation(*args, **kwargs)
            raise RuntimeError("injected write failure")

        with patch("src.build_archive._insert_observation", side_effect=insert_then_fail):
            with self.assertRaisesRegex(RuntimeError, "injected write failure"):
                import_file(self.connection, self.export_path)

        self.assertEqual(self.export_path.read_bytes(), original_bytes)
        for table in ("servers", "categories", "channels", "discord_identities", "messages"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
        failed_run = self.connection.execute(
            "SELECT status, records_written, accepted_at FROM imports"
        ).fetchone()
        self.assertEqual(tuple(failed_run), ("failed", 0, None))
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_database_rejects_message_with_mismatched_source_and_import(self) -> None:
        self._write_channel_export()
        first = import_file(self.connection, self.export_path)
        other_source_id = register_source_capture(
            self.connection,
            logical_source_key="discord:other-channel",
            source_kind="discord_json",
            source_path="01_Raw/other-channel.json",
            sha256="other-capture",
            byte_size=1,
            scope_json='{"channel_id":"other-channel"}',
            registered_at="2026-01-02T00:00:00+00:00",
        )
        other_import_id = start_import_run(
            self.connection,
            source_id=other_source_id,
            started_at="2026-01-02T00:01:00+00:00",
            importer_name="test",
            importer_version="1",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO messages(
                       source_id, import_id, discord_message_id,
                       content_original, content_search, source_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (first["source_id"], other_import_id, "unrelated-message", "Raw", "Raw", "{}"),
            )


if __name__ == "__main__":
    unittest.main()