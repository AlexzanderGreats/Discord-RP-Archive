from __future__ import annotations

import json
import sqlite3
import unittest

from archive.signals import extract_signals


def message(
    observation_id: int,
    discord_message_id: str,
    *,
    timestamp_unix: float | str | None = None,
    timestamp_utc: str | None = None,
    channel_id: int | None = 1,
    author_id: str | None = "user-1",
    author_identity_id: int | None = None,
    webhook_id: str | None = None,
    reply_to: str | None = None,
    content: str = "RP content",
    source: object = None,
    source_id: int | None = 1,
    import_id: int | None = 1,
) -> dict[str, object]:
    return {
        "message_observation_id": observation_id,
        "discord_message_id": discord_message_id,
        "timestamp_unix": timestamp_unix,
        "timestamp_utc": timestamp_utc,
        "channel_id": channel_id,
        "author_discord_user_id_original": author_id,
        "author_identity_id": author_identity_id,
        "webhook_id_original": webhook_id,
        "reply_to_discord_message_id": reply_to,
        "content_original": content,
        "source_json": json.dumps(source) if isinstance(source, dict) else source,
        "source_id": source_id,
        "import_id": import_id,
    }


class SignalExtractionTests(unittest.TestCase):
    def test_accepts_sqlite_row_shaped_message_observations(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """SELECT 1 AS message_observation_id, 'message' AS discord_message_id,
                          10.0 AS timestamp_unix, '1970-01-01T00:00:10+00:00' AS timestamp_utc,
                          3 AS channel_id, 'user' AS author_discord_user_id_original,
                          NULL AS webhook_id_original, NULL AS reply_to_discord_message_id,
                          'Content' AS content_original, '{}' AS source_json"""
            ).fetchone()
            signals = extract_signals([row])
        finally:
            connection.close()

        self.assertFalse(any(signal.signal_type == "timestamp_missing" for signal in signals))
        self.assertFalse(any(signal.signal_type == "source_payload_invalid" for signal in signals))

    def test_extracts_timestamp_channel_author_reply_and_thread_evidence(self) -> None:
        signals = extract_signals([
            message(
                1, "discord-a", timestamp_unix=100.0, timestamp_utc="1970-01-01T00:01:40+00:00",
                source={"threadId": "thread-a"},
            ),
            message(
                2, "discord-b", timestamp_unix=460.0, timestamp_utc="1970-01-01T00:07:40+00:00",
                channel_id=2, author_id="user-2", reply_to="discord-a",
                source={"threadId": "thread-b"},
            ),
        ])

        by_type = {signal.signal_type: signal for signal in signals}
        self.assertEqual(by_type["timestamp_gap"].observed_value["gap_seconds"], 360.0)
        self.assertEqual(by_type["channel_change"].observed_value, {"from": 1, "to": 2})
        self.assertEqual(by_type["author_change"].observed_value["from"], ("discord_user", "user-1"))
        self.assertEqual(by_type["thread_change"].observed_value["to"], ("id", "thread-b"))
        self.assertEqual(by_type["reply_relationship"].source_message_ids, (2, 1))
        self.assertEqual(by_type["reply_relationship"].evidence["target_resolution"], "resolved")
        self.assertEqual(by_type["reply_relationship"].evidence_kind, "explicit")
        self.assertEqual(by_type["channel_change"].evidence_kind, "heuristic")

    def test_extracts_explicit_content_markers_without_resolving_identity(self) -> None:
        signals = extract_signals([
            message(
                1, "discord-a",
                content="[[SCENE START]]\n[Winter]\nLocation: North Hall\n(OOC) quick note",
            ),
        ])
        observed = [(signal.signal_type, dict(signal.observed_value)) for signal in signals]

        self.assertIn(("explicit_scene_marker", {"marker": "scene_start"}), observed)
        speaker = next(signal for signal in signals if signal.signal_type == "speaker_marker")
        self.assertEqual(speaker.observed_value["text"], "Winter")
        self.assertFalse(speaker.evidence["identity_resolved"])
        self.assertIn(
            ("narrative_context_marker", {"label": "location", "value": "North Hall"}),
            observed,
        )
        self.assertIn(("ooc_marker", {"content_prefix": "(OOC) quick note"}), observed)

    def test_reports_missing_malformed_and_disagreeing_timestamps(self) -> None:
        signals = extract_signals([
            message(1, "missing"),
            message(2, "malformed", timestamp_unix="not-a-number"),
            message(
                3, "conflict", timestamp_unix=30.0,
                timestamp_utc="1970-01-01T00:00:40+00:00",
            ),
        ])
        timestamp_signals = [signal for signal in signals if signal.signal_type.startswith("timestamp_")]

        self.assertEqual(
            [signal.signal_type for signal in timestamp_signals],
            ["timestamp_missing", "timestamp_invalid", "timestamp_invalid"],
        )
        self.assertEqual(timestamp_signals[-1].observed_value["issue"], "timestamp_fields_disagree")
        self.assertFalse(any(signal.signal_type == "timestamp_gap" for signal in signals))

    def test_malformed_source_payload_is_reported_and_thread_signal_is_omitted(self) -> None:
        signals = extract_signals([
            message(1, "first", source="{"),
            message(2, "second", source={"threadName": "Thread"}),
        ])

        malformed = next(signal for signal in signals if signal.signal_type == "source_payload_invalid")
        self.assertEqual(malformed.observed_value["issue"], "source_json_invalid_json")
        self.assertFalse(any(signal.signal_type == "thread_change" for signal in signals))

    def test_preserves_conflicting_transition_and_continuity_evidence(self) -> None:
        signals = extract_signals([
            message(1, "earlier", timestamp_unix=1.0),
            message(
                2, "later", timestamp_unix=2.0, reply_to="earlier",
                content="[[SCENE START]]\n[New Speaker]\nReplying to the previous scene",
            ),
        ])
        second_message_types = {
            signal.signal_type
            for signal in signals
            if signal.source_message_ids[0] == 2
        }

        self.assertIn("explicit_scene_marker", second_message_types)
        self.assertIn("reply_relationship", second_message_types)

    def test_reports_unresolved_reply_and_ambiguous_duplicate_message_id(self) -> None:
        unresolved = extract_signals([message(1, "one", reply_to="absent")])
        reply = next(signal for signal in unresolved if signal.signal_type == "reply_relationship")
        self.assertEqual(reply.evidence["target_resolution"], "unresolved")
        self.assertEqual(reply.source_message_ids, (1,))

        ambiguous = extract_signals([
            message(1, "duplicate"),
            message(2, "duplicate"),
            message(3, "reply", reply_to="duplicate"),
        ])
        reply = next(signal for signal in ambiguous if signal.signal_type == "reply_relationship")
        ambiguity = next(signal for signal in ambiguous if signal.signal_type == "reply_target_ambiguous")
        self.assertEqual(reply.evidence["target_resolution"], "ambiguous")
        self.assertEqual(ambiguity.observed_value["candidate_count"], 2)

    def test_reports_collection_and_speaker_marker_changes(self) -> None:
        signals = extract_signals([
            message(1, "one", content="[Winter]\nFirst", source_id=1, import_id=1),
            message(2, "two", content="[Mira]\nNext", source_id=2, import_id=2),
        ])
        collection = [signal for signal in signals if signal.signal_type == "collection_boundary"]
        self.assertEqual({signal.observed_value["field"] for signal in collection}, {"source_id", "import_id"})
        speaker_change = next(signal for signal in signals if signal.signal_type == "speaker_marker_change")
        self.assertEqual(speaker_change.observed_value, {"from": "Winter", "to": "Mira"})

    def test_reports_narrative_context_transition_as_heuristic_pair_evidence(self) -> None:
        signals = extract_signals([
            message(1, "one", content="Location: North Hall"),
            message(2, "two", content="Location: Garden"),
        ])
        transition = next(
            signal for signal in signals
            if signal.signal_type == "narrative_context_transition"
        )

        self.assertEqual(transition.source_message_ids, (1, 2))
        self.assertEqual(
            transition.observed_value,
            {"label": "location", "from": "North Hall", "to": "Garden"},
        )
        self.assertEqual(transition.evidence_kind, "heuristic")

    def test_missing_identity_channel_and_thread_fields_do_not_create_change_signals(self) -> None:
        signals = extract_signals([
            message(1, "one", channel_id=None, author_id=None, author_identity_id=7, source={}),
            message(2, "two", channel_id=None, author_id=None, author_identity_id=7, source={}),
        ])

        signal_types = {signal.signal_type for signal in signals}
        self.assertNotIn("channel_change", signal_types)
        self.assertNotIn("author_change", signal_types)
        self.assertNotIn("thread_change", signal_types)
        self.assertNotIn("timestamp_gap", signal_types)

    def test_rejects_records_without_observation_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "message_observation_id"):
            extract_signals([{"discord_message_id": "one"}])


if __name__ == "__main__":
    unittest.main()
