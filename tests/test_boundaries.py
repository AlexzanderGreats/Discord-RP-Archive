from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from archive.boundaries import (
    BoundaryScoringConfig,
    analyze_message_boundaries,
    score_boundary_candidates,
)
from archive.boundary_persistence import run_boundary_detection
from archive.signals import extract_signals
from src.build_archive import create_database, import_file


def message(
    observation_id: int,
    discord_message_id: str,
    timestamp: float,
    *,
    content: str = "Roleplay",
    channel_id: int = 1,
    author_id: str = "player-1",
    reply_to: str | None = None,
    source_id: int = 1,
    import_id: int = 1,
) -> dict[str, object]:
    return {
        "message_observation_id": observation_id,
        "discord_message_id": discord_message_id,
        "timestamp_unix": timestamp,
        "timestamp_utc": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "channel_id": channel_id,
        "author_discord_user_id_original": author_id,
        "webhook_id_original": None,
        "reply_to_discord_message_id": reply_to,
        "content_original": content,
        "source_json": "{}",
        "source_id": source_id,
        "import_id": import_id,
    }


class BoundaryScoringTests(unittest.TestCase):
    def test_scene_start_and_end_are_hard_on_the_correct_transition_edge(self) -> None:
        _, start_candidates = analyze_message_boundaries([
            message(1, "one", 1),
            message(2, "two", 2, content="[[SCENE START]]\nText"),
            message(3, "three", 3),
        ])
        self.assertEqual(
            [(item.left_message_observation_id, item.right_message_observation_id, item.classification)
             for item in start_candidates],
            [(1, 2, "hard_candidate"), (2, 3, "none")],
        )

        _, end_candidates = analyze_message_boundaries([
            message(1, "one", 1, content="[[SCENE END]]\nText"),
            message(2, "two", 2),
        ])
        self.assertEqual(end_candidates[0].classification, "hard_candidate")
        self.assertEqual(end_candidates[0].boundary_type, "hard")

    def test_timeskip_and_transition_markers_are_hard_candidates(self) -> None:
        for marker in ("TIME SKIP", "TRANSITION", "SCENE BREAK"):
            with self.subTest(marker=marker):
                _, candidates = analyze_message_boundaries([
                    message(1, "one", 1),
                    message(2, "two", 2, content=f"[[{marker}]]"),
                ])
                self.assertEqual(candidates[0].classification, "hard_candidate")

    def test_hard_marker_keeps_contradictory_reply_continuity(self) -> None:
        signals = extract_signals([
            message(1, "one", 1, content="[[SCENE END]]"),
            message(2, "two", 2, reply_to="one"),
        ])
        candidate = score_boundary_candidates((1, 2), signals)[0]

        self.assertEqual(candidate.classification, "hard_candidate")
        self.assertAlmostEqual(candidate.raw_score, 0.7 + 0.25 / 4443.177)
        self.assertEqual(len(candidate.supporting_evidence), 2)
        self.assertEqual(len(candidate.contradictory_evidence), 1)
        self.assertEqual(candidate.contradictory_evidence[0].signal_type, "reply_relationship")
        hard_evidence = next(item for item in candidate.supporting_evidence if item.hard_boundary)
        self.assertEqual(hard_evidence.signal_type, "explicit_scene_marker")
        self.assertEqual(
            next(item for item in candidate.contributions if item.signal_type == "timestamp_gap").role,
            "supporting",
        )

    def test_collection_boundary_is_hard_but_import_run_change_alone_is_not(self) -> None:
        source_signals = extract_signals([
            message(1, "one", 1, source_id=1, import_id=1),
            message(2, "two", 2, source_id=2, import_id=1),
        ])
        source_candidate = score_boundary_candidates((1, 2), source_signals)[0]
        self.assertEqual(source_candidate.classification, "hard_candidate")

        import_signals = extract_signals([
            message(1, "one", 1, source_id=1, import_id=1),
            message(2, "two", 2, source_id=1, import_id=2),
        ])
        import_candidate = score_boundary_candidates((1, 2), import_signals)[0]
        self.assertEqual(import_candidate.classification, "none")

    def test_timestamp_normalization_and_configured_thresholds(self) -> None:
        config = BoundaryScoringConfig(
            timestamp_gap_reference_seconds=100,
            soft_candidate_threshold=0.2,
            strong_candidate_threshold=0.4,
        )
        _, candidates = analyze_message_boundaries([
            message(1, "one", 1),
            message(2, "two", 51),
        ], config=config)
        candidate = candidates[0]

        self.assertEqual(candidate.raw_score, 0.125)
        self.assertEqual(candidate.classification, "none")
        contribution = candidate.contributions[0]
        self.assertEqual(contribution.signal_type, "timestamp_gap")
        self.assertEqual(contribution.strength, 0.5)
        self.assertEqual(contribution.contribution, 0.125)

    def test_inconsistent_timestamp_fields_are_not_scored_as_a_gap(self) -> None:
        _, candidates = analyze_message_boundaries([
            message(1, "one", 1),
            {
                **message(2, "two", 50),
                "timestamp_utc": "not-a-timestamp",
            },
        ])

        self.assertFalse(any(item.signal_type == "timestamp_gap" for item in candidates[0].contributions))

    def test_soft_signals_combine_and_classify_with_configured_thresholds(self) -> None:
        _, candidates = analyze_message_boundaries([
            message(1, "one", 1, content="Location: The Hall", channel_id=1, author_id="one"),
            message(2, "two", 1, content="Location: The Garden", channel_id=2, author_id="two"),
        ])
        candidate = candidates[0]

        self.assertEqual(candidate.raw_score, 0.55)
        self.assertEqual(candidate.classification, "soft_candidate")
        self.assertEqual(candidate.boundary_type, "soft")
        self.assertEqual(
            {contribution.signal_type for contribution in candidate.contributions},
            {"timestamp_gap", "channel_change", "author_change", "narrative_context_transition"},
        )

    def test_continuity_and_small_gaps_are_negative_or_weak_evidence(self) -> None:
        _, candidates = analyze_message_boundaries([
            message(1, "one", 1),
            message(2, "two", 2, reply_to="one"),
        ])
        candidate = candidates[0]
        self.assertEqual(candidate.classification, "none")
        self.assertAlmostEqual(candidate.raw_score, -0.3 + 0.25 / 4443.177)
        self.assertEqual(len(candidate.contradictory_evidence), 1)

        _, small_gap_candidates = analyze_message_boundaries([
            message(1, "a", 1),
            message(2, "b", 1.1),
        ])
        self.assertLess(small_gap_candidates[0].raw_score, 0.01)
        self.assertEqual(small_gap_candidates[0].classification, "none")

    def test_saturated_gap_with_reply_continuity_retains_both_signals(self) -> None:
        _, candidates = analyze_message_boundaries([
            message(1, "one", 1),
            message(2, "two", 100000, reply_to="one"),
        ])
        candidate = candidates[0]

        self.assertAlmostEqual(candidate.raw_score, -0.05)
        self.assertEqual(
            {item.signal_type for item in candidate.contributions},
            {"timestamp_gap", "reply_relationship"},
        )
        self.assertEqual(len(candidate.supporting_evidence), 1)
        self.assertEqual(len(candidate.contradictory_evidence), 1)

    def test_channel_change_with_direct_reply_keeps_both_conflicting_signals(self) -> None:
        _, candidates = analyze_message_boundaries([
            message(1, "one", 1, channel_id=1, author_id="one"),
            message(2, "two", 1, channel_id=2, author_id="two", reply_to="one"),
        ])
        candidate = candidates[0]

        self.assertAlmostEqual(candidate.raw_score, 0.0)
        self.assertEqual(candidate.classification, "none")
        self.assertEqual(
            {item.signal_type for item in candidate.supporting_evidence},
            {"channel_change", "author_change"},
        )
        self.assertEqual(
            [item.signal_type for item in candidate.contradictory_evidence],
            ["reply_relationship"],
        )

    def test_weight_and_threshold_changes_are_configuration_driven(self) -> None:
        signals = extract_signals([
            message(1, "one", 1, channel_id=1),
            message(2, "two", 2, channel_id=2),
        ])
        config = BoundaryScoringConfig(
            soft_candidate_threshold=0.2,
            strong_candidate_threshold=0.3,
            weights={"channel_change": 0.5, "timestamp_gap": 0.0},
        )
        candidate = score_boundary_candidates((1, 2), signals, config=config)[0]

        self.assertEqual(candidate.raw_score, 0.5)
        self.assertEqual(candidate.classification, "strong_candidate")
        channel = next(item for item in candidate.contributions if item.signal_type == "channel_change")
        self.assertEqual(channel.weight, 0.5)

    def test_configuration_json_can_be_loaded_and_reproduced(self) -> None:
        config = BoundaryScoringConfig(
            algorithm_version="event_boundaries_test",
            timestamp_gap_reference_seconds=120,
            weights={"channel_change": 0.4, "reply_relationship": -0.2},
            hard_scene_markers=("scene_start",),
        )
        loaded = BoundaryScoringConfig.from_json(json.dumps(config.to_dict()))

        self.assertEqual(loaded.to_dict(), config.to_dict())

    def test_fails_for_invalid_chronological_order_and_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "ordered by timestamp"):
            analyze_message_boundaries([message(1, "one", 2), message(2, "two", 1)])
        with self.assertRaisesRegex(ValueError, "thresholds"):
            BoundaryScoringConfig(soft_candidate_threshold=0.7, strong_candidate_threshold=0.6)

    def test_results_and_fingerprints_are_deterministic(self) -> None:
        rows = [
            message(1, "one", 1),
            message(2, "two", 5000, channel_id=2),
        ]
        first_signals, first = analyze_message_boundaries(rows)
        second_signals, second = analyze_message_boundaries(rows)
        self.assertEqual([item.to_dict() for item in first], [item.to_dict() for item in second])
        self.assertEqual(
            [item.signal_type for item in first_signals],
            [item.signal_type for item in second_signals],
        )


class BoundaryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_database(self.connection)
        export = Path(self.temp_dir.name) / "channel.json"
        export.write_text(json.dumps({
            "guild": {"id": "guild", "name": "Server"},
            "channel": {"id": "channel", "name": "Channel"},
            "messages": [
                {
                    "id": "one",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "author": {"id": "player", "name": "Player"},
                    "content": "First",
                },
                {
                    "id": "two",
                    "timestamp": "2026-01-01T03:00:00Z",
                    "author": {"id": "player", "name": "Player"},
                    "content": "[[SCENE START]]\nSecond",
                },
            ],
        }), encoding="utf-8")
        import_file(self.connection, export)
        self.messages = self.connection.execute(
            """SELECT message_observation_id, discord_message_id, timestamp_unix, timestamp_utc,
                      channel_id, author_identity_id, author_discord_user_id_original,
                      webhook_id_original, reply_to_discord_message_id, content_original,
                      source_id, import_id, source_json
               FROM messages ORDER BY timestamp_unix, message_observation_id"""
        ).fetchall()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_persists_runs_and_candidates_without_writing_curated_scene_data(self) -> None:
        run_id, candidates = run_boundary_detection(
            self.connection,
            self.messages,
            input_scope={"channel": "channel"},
        )
        run = self.connection.execute(
            "SELECT * FROM boundary_detection_runs WHERE detection_run_id = ?", (run_id,)
        ).fetchone()
        stored = self.connection.execute(
            "SELECT * FROM event_boundary_candidates WHERE detection_run_id = ?", (run_id,)
        ).fetchall()

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["algorithm_version"], "event_boundaries_v1")
        self.assertEqual(run["input_scope_json"], '{"channel":"channel"}')
        self.assertTrue(run["input_fingerprint"])
        self.assertEqual(
            [
                row[0] for row in self.connection.execute(
                    """SELECT message_observation_id FROM boundary_detection_run_messages
                       WHERE detection_run_id = ? ORDER BY position""",
                    (run_id,),
                )
            ],
            [row["message_observation_id"] for row in self.messages],
        )
        self.assertEqual(len(stored), sum(item.classification != "none" for item in candidates))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM scenes").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM scene_messages").fetchone()[0],
            0,
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_repeat_runs_are_distinguishable_and_keep_identical_input_fingerprints(self) -> None:
        first_id, _ = run_boundary_detection(
            self.connection, self.messages, input_scope={"channel": "channel"}
        )
        second_id, _ = run_boundary_detection(
            self.connection, self.messages, input_scope={"channel": "channel"}
        )
        rows = self.connection.execute(
            """SELECT detection_run_id, input_fingerprint, configuration_sha256
               FROM boundary_detection_runs ORDER BY detection_run_id"""
        ).fetchall()

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(rows[0]["input_fingerprint"], rows[1]["input_fingerprint"])
        self.assertEqual(rows[0]["configuration_sha256"], rows[1]["configuration_sha256"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_boundary_candidates").fetchone()[0],
            2,
        )

    def test_failed_run_is_recorded_and_original_error_is_raised(self) -> None:
        invalid_messages = [dict(self.messages[0]), dict(self.messages[1])]
        invalid_messages[1]["timestamp_unix"] = 0

        with self.assertRaisesRegex(ValueError, "ordered by timestamp"):
            run_boundary_detection(
                self.connection,
                invalid_messages,
                input_scope={"channel": "channel"},
            )

        run = self.connection.execute(
            "SELECT status, error_details FROM boundary_detection_runs ORDER BY detection_run_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(run["status"], "failed")
        self.assertIn("ordered by timestamp", run["error_details"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_boundary_candidates").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
