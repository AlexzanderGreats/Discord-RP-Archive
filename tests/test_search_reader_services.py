from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.ollama_service import ArchiveAnswerService, QuestionType, classify_question
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

    def test_relevant_search_ranks_question_terms_and_skips_stop_words(self) -> None:
        result = self.search_service.search_relevant("Where is the silver compass?")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content_original"], "A silver compass")
        self.assertGreater(result[0]["relevance_score"], 0)

    def test_relevant_search_prioritizes_rare_question_terms(self) -> None:
        result = self.search_service.search_relevant("scene compass")

        self.assertEqual(result[0]["content_original"], "A silver compass")
        self.assertGreater(result[0]["relevance_score"], result[1]["relevance_score"])

    def test_relevant_search_ignores_question_filler_for_specific_names(self) -> None:
        self._write_export([
            {"id": "generic", "timestamp": "2026-01-01T00:00:00Z",
             "content": "According to the archive, what happened immediately after the fight with the emperor?"},
            {"id": "specific", "timestamp": "2026-01-01T00:01:00Z",
             "content": "Winter and Dravum met."},
        ])
        import_file(self.connection, self.export_path)

        result = self.search_service.search_relevant(
            "According to the archive, what happened immediately after Winter's fight "
            "with Dravum, the Emperor of the Unwritten?"
        )

        self.assertEqual(result[0]["discord_message_id"], "specific")

    def test_relevant_search_returns_empty_when_no_search_terms_remain(self) -> None:
        self.assertEqual(self.search_service.search_relevant("Who is it?"), [])

    def test_reader_returns_chronological_channel_context(self) -> None:
        middle = self.connection.execute(
            "SELECT message_observation_id FROM messages WHERE discord_message_id = 'two'"
        ).fetchone()[0]

        context = self.reader_service.get_message_context(middle, before=1, after=1)

        self.assertEqual(context["message"]["discord_message_id"], "two")
        self.assertEqual([row["discord_message_id"] for row in context["before"]], ["one"])
        self.assertEqual([row["discord_message_id"] for row in context["after"]], ["three"])

    def test_answer_retrieval_includes_adjacent_messages(self) -> None:
        answer_service = ArchiveAnswerService(self.search_service, None)

        evidence = answer_service.retrieve("silver compass", limit=1)

        self.assertEqual([item["discord_message_id"] for item in evidence], ["one", "two", "three"])
        self.assertEqual(
            [item["context_relation"] for item in evidence],
            ["adjacent before", "query match", "adjacent after"],
        )

    def test_question_classifier_selects_retrieval_intent(self) -> None:
        self.assertEqual(
            classify_question("How did Winter's relationship with Dabris change across server events?"),
            QuestionType.CHRONOLOGY,
        )
        self.assertEqual(
            classify_question("What happened immediately after Winter's fight with Dravum?"),
            QuestionType.CHRONOLOGY,
        )
        self.assertEqual(
            classify_question("How did Winter interact with Dabris across server events?"),
            QuestionType.CHRONOLOGY,
        )
        self.assertEqual(classify_question("Who played Winter?"), QuestionType.ATTRIBUTION)
        self.assertEqual(classify_question("What color was the compass?"), QuestionType.EXTRACTION)

    def test_chronology_retrieval_keeps_distinct_event_dates(self) -> None:
        self._write_export([
            {"id": "one", "timestamp": "2026-01-01T00:00:00Z", "content": "Winter confronts Dabris."},
            {"id": "two", "timestamp": "2026-01-01T00:01:00Z", "content": "Winter argues with Dabris again."},
            {"id": "three", "timestamp": "2026-01-08T00:00:00Z", "content": "Winter apologizes to Dabris."},
            {"id": "four", "timestamp": "2026-01-20T00:00:00Z", "content": "Winter and Dabris make peace."},
        ])
        import_file(self.connection, self.export_path)
        answer_service = ArchiveAnswerService(self.search_service, None)

        evidence = answer_service.retrieve(
            "How did Winter's relationship with Dabris change across server events?", limit=3
        )

        event_matches = [item for item in evidence if item["context_relation"] == "event match"]
        self.assertEqual(
            [item["timestamp_utc"][:10] for item in event_matches],
            ["2026-01-01", "2026-01-08", "2026-01-20"],
        )

    def test_explicit_date_time_range_limits_retrieval_and_keeps_range_context(self) -> None:
        self.export_path.write_text(
            json.dumps({
                "guild": {"id": "server", "name": "Server"},
                "channel": {"id": "channel", "name": "Channel"},
                "messages": [
                    {
                        "id": message_id,
                        "timestamp": timestamp,
                        "content": content,
                        "author": {"id": author_id, "name": author_name},
                    }
                    for message_id, timestamp, content, author_id, author_name in [
                        ("before", "2025-09-18T08:30:59+00:00", "Before", "oblivion", "𝕆𝕓𝕝𝕚𝕧𝕚𝕠"),
                        ("start", "2025-09-18T08:31:00+00:00", "The disagreement begins.", "oblivion", "𝕆𝕓𝕝𝕚𝕧𝕚𝕠"),
                        ("middle", "2025-09-18T15:15:00+00:00", "The response.", "dravum", "Kairos Dravum"),
                        ("end", "2025-09-18T22:02:59+00:00", "The disagreement continues.", "oblivion", "𝕆𝕓𝕝𝕚𝕧𝕚𝕠"),
                        ("after", "2025-09-18T22:03:00+00:00", "After the interval.", "dravum", "Kairos Dravum"),
                    ]
                ],
            }),
            encoding="utf-8",
        )
        import_file(self.connection, self.export_path)
        answer_service = ArchiveAnswerService(self.search_service, None)

        evidence = answer_service.retrieve(
            "What argument did Oblivion and Dravum have during "
            "9/18/2025 from 1:31 AM to 3:02 PM?"
        )

        message_ids = {item["discord_message_id"] for item in evidence}
        self.assertIn("start", message_ids)
        self.assertIn("middle", message_ids)
        self.assertIn("end", message_ids)
        self.assertNotIn("before", message_ids)
        self.assertNotIn("after", message_ids)
        self.assertTrue(all(
            "2025-09-18T08:31:00+00:00" <= item["timestamp_utc"]
            < "2025-09-18T22:03:00+00:00"
            for item in evidence
        ))
        self.assertEqual(
            classify_question(
                "What argument did Oblivion and Dravum have during "
                "9/18/2025 from 1:31 AM to 3:02 PM?"
            ),
            QuestionType.CHRONOLOGY,
        )

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