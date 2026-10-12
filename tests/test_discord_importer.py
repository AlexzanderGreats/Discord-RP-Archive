from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.provenance import current_accepted_source_id
from src.build_archive import (
    DATABASE_PATH,
    MANIFEST_PATH,
    create_database,
    import_file,
    merge_manifest_results,
    manifest_path_for_database,
)


class DiscordImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "archive.sqlite3"
        self.export_path = self.root / "channel.json"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_database(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _write_export(self, content: str, timestamp: str = "2026-01-01T00:00:00Z") -> None:
        self.export_path.write_text(
            json.dumps(
                {
                    "guild": {"id": "guild-1", "name": "Server"},
                    "channel": {
                        "id": "channel-1",
                        "name": "Channel",
                        "categoryId": "category-1",
                        "category": "Category",
                    },
                    "messageCount": 1,
                    "messages": [
                        {
                            "id": "123",
                            "timestamp": timestamp,
                            "author": {"id": "user-1", "name": "Author", "isBot": False},
                            "content": content,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_manifest_path_follows_custom_database(self) -> None:
        custom_database = self.root / "acknowledgment-original.sqlite3"

        self.assertEqual(manifest_path_for_database(DATABASE_PATH), MANIFEST_PATH)
        self.assertEqual(
            manifest_path_for_database(custom_database),
            self.root / "acknowledgment-original-import-manifest.json",
        )

    def test_manifest_results_merge_by_path_and_hash(self) -> None:
        manifest_path = self.root / "manifest.json"
        previous = {"file": "old.json", "sha256": "old-hash", "status": "succeeded"}
        manifest_path.write_text(
            json.dumps({"results": [previous]}),
            encoding="utf-8",
        )
        refreshed = {"file": "old.json", "sha256": "old-hash", "status": "partial"}
        added = {"file": "new.json", "sha256": "new-hash", "status": "succeeded"}

        merged = merge_manifest_results(manifest_path, [refreshed, added])

        self.assertEqual(merged, [refreshed, added])

    def test_recapture_preserves_changed_message_and_rejects_invalid_capture(self) -> None:
        self._write_export("Hello")
        first = import_file(self.connection, self.export_path)

        self._write_export("Hello, world", "2026-01-02T00:00:00Z")
        second = import_file(self.connection, self.export_path)

        self._write_export("Broken timestamp", "not-a-timestamp")
        third = import_file(self.connection, self.export_path)

        observations = self.connection.execute(
            """SELECT source_id, import_id, discord_message_id, content_original
               FROM messages WHERE discord_message_id = '123' ORDER BY import_id"""
        ).fetchall()
        self.assertEqual(
            [(row["content_original"], row["source_id"]) for row in observations],
            [("Hello", first["source_id"]), ("Hello, world", second["source_id"])],
        )
        self.assertEqual(len({row["import_id"] for row in observations}), 2)

        current_source_id = current_accepted_source_id(
            self.connection,
            self.connection.execute(
                "SELECT logical_source_key FROM sources WHERE source_id = ?",
                (second["source_id"],),
            ).fetchone()[0],
        )
        self.assertEqual(current_source_id, second["source_id"])
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(third["status"], "partial")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM imports WHERE accepted_at IS NOT NULL"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()