from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.reader_service import ReaderService
from archive.search_service import SearchService
from src.build_archive import create_database, import_file


class SearchReaderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.export_path = Path(self.temp_dir.name) / "channel.json"
        self.connection = sqlite3.connect(":memory:")
        create_database(self.connection)
        self._write_export(
            [
                {"id": "one", "timestamp": "2026-01-01T00:00:00Z", "content": "Opening scene"},
                {"id": "two", "timestamp": "2026-01-01T00:01:00Z", "content": "A silver compass"},
                {"id": "three", "timestamp": "2026-01-01T00:02:00Z", "content": "Closing scene"},
            ]
        )
        import_file(self.connection, self.export_path)
        self.search_service = SearchService(self.connection)
        self.reader_service = ReaderService(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _write_export(self, messages: list[dict[str, str]]) -> None:
        self.export_path.write_text(
            json.dumps(
                {
                    "guild": {"id": "server", "name": "Server"},
                    "channel": {"id": "channel", "name": "Channel"},
                    "messages": [
                        {
                            **message,
                            "author": {"id": "user", "name": "Author"},
                        }
                        for message in messages
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_search_returns_current_capture_results_and_metadata(self) -> None:
        result = self.search_service.search("SILVER COMPASS")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content_original"], "A silver compass")
        self.assertEqual(result[0]["channel_name"], "Channel")
        self.assertEqual(result[0]["author_discord_name_original"], "Author")

    def test_reader_returns_chronological_channel_context(self) -> None:
        middle = self.connection.execute(
            "SELECT message_observation_id FROM messages WHERE discord_message_id = 'two'"
        ).fetchone()[0]

        context = self.reader_service.get_message_context(middle, before=1, after=1)

        self.assertEqual(context["message"]["discord_message_id"], "two")
        self.assertEqual([row["discord_message_id"] for row in context["before"]], ["one"])
        self.assertEqual([row["discord_message_id"] for row in context["after"]], ["three"])

    def test_recapture_hides_superseded_observations(self) -> None:
        self._write_export(
            [{"id": "one", "timestamp": "2026-01-01T00:00:00Z", "content": "Revised scene"}]
        )
        import_file(self.connection, self.export_path)

        self.assertEqual(self.search_service.search("opening"), [])
        current = self.search_service.search("revised")
        self.assertEqual([row["discord_message_id"] for row in current], ["one"])


if __name__ == "__main__":
    unittest.main()