from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.sync_service import synchronize_archive


class StartupSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.raw_directory = self.root / "01_Raw"
        self.raw_directory.mkdir()
        self.export_path = self.raw_directory / "channel.json"
        self.export_path.write_text(
            json.dumps(
                {
                    "guild": {"id": "guild", "name": "Test server"},
                    "channel": {"id": "channel", "name": "Test channel"},
                    "messages": [
                        {
                            "id": "1",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "content": "First capture",
                            "author": {"id": "user", "name": "Writer"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.document_path = self.raw_directory / "notes.txt"
        self.document_path.write_text("Archive note", encoding="utf-8")
        self.connection = sqlite3.connect(":memory:")

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_imports_supported_files_then_skips_unchanged_captures(self) -> None:
        stages: list[str] = []
        first = synchronize_archive(
            self.connection,
            self.raw_directory,
            progress=lambda _current, _total, _path, stage: stages.append(stage),
        )

        self.assertEqual(first.scanned, 2)
        self.assertEqual(first.imported, 2)
        self.assertEqual(first.failed, 0)
        self.assertTrue(first.reindexed)
        self.assertEqual(first.database_health, "ok")
        self.assertLess(stages.index("Validated"), stages.index("Reindexing"))

        second = synchronize_archive(self.connection, self.raw_directory)

        self.assertEqual(second.imported, 0)
        self.assertEqual(second.unchanged, 2)
        self.assertFalse(second.reindexed)

    def test_changed_capture_is_imported_as_a_new_observation(self) -> None:
        synchronize_archive(self.connection, self.raw_directory)
        payload = json.loads(self.export_path.read_text(encoding="utf-8"))
        payload["messages"][0]["content"] = "Revised capture"
        self.export_path.write_text(json.dumps(payload), encoding="utf-8")

        report = synchronize_archive(self.connection, self.raw_directory)

        self.assertEqual(report.imported, 1)
        self.assertEqual(report.unchanged, 1)
        current = self.connection.execute(
            "SELECT content_original FROM messages WHERE content_original = 'Revised capture'"
        ).fetchone()
        self.assertIsNotNone(current)


if __name__ == "__main__":
    unittest.main()