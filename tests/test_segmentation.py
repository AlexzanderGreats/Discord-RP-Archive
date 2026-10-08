from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.boundaries import BoundaryCandidate
from archive.segmentation_persistence import (
    load_current_message_observations,
    run_candidate_segmentation,
)
from archive.segmentation import (
    SegmentationConfig,
    segment_message_observations,
)
from src.build_archive import create_database, import_file


def message(
    observation_id: int,
    discord_message_id: str,
    timestamp: float | None,
    *,
    server_id: int | None = 1,
    channel_id: int | None = 10,
    source_id: int | None = 100,
    reply_to: str | None = None,
) -> dict[str, object]:
    return {
        "message_observation_id": observation_id,
        "discord_message_id": discord_message_id,
        "timestamp_unix": timestamp,
        "timestamp_utc": None,
        "server_id": server_id,
        "channel_id": channel_id,
        "source_id": source_id,
        "reply_to_discord_message_id": reply_to,
    }


def boundary(
    left_id: int,
    right_id: int,
    *,
    boundary_type: str | None,
    classification: str,
    raw_score: float,
) -> BoundaryCandidate:
    return BoundaryCandidate(
        left_message_observation_id=left_id,
        right_message_observation_id=right_id,
        boundary_type=boundary_type,  # type: ignore[arg-type]
        raw_score=raw_score,
        classification=classification,  # type: ignore[arg-type]
        supporting_evidence=(),
        contradictory_evidence=(),
        contributions=(),
        algorithm_version="event_boundaries_v1",
    )


class CandidateSegmentationTests(unittest.TestCase):
    def test_inactivity_gap_splits_a_span_and_retains_boundary_candidate_evidence(self) -> None:
        rows = [
            message(1, "one", 0),
            message(2, "two", 100),
            message(3, "three", 10_000),
        ]
        soft_candidate = boundary(
            2, 3, boundary_type="soft", classification="strong_candidate", raw_score=0.8
        )

        result = segment_message_observations(rows, [soft_candidate])

        self.assertEqual([span.member_observation_ids for span in result.spans], [(1, 2), (3,)])
        second = result.spans[1]
        self.assertEqual(second.start_reason, "inactivity_gap")
        self.assertEqual(
            second.start_boundary_evidence["classification"],
            "strong_candidate",
        )
        split = next(item for item in result.boundary_decisions if item["right_message_observation_id"] == 3)
        self.assertTrue(split["split"])

    def test_soft_boundary_candidate_alone_does_not_split_span(self) -> None:
        rows = [message(1, "one", 0), message(2, "two", 1)]
        soft_candidate = boundary(
            1, 2, boundary_type="soft", classification="strong_candidate", raw_score=0.9
        )

        result = segment_message_observations(rows, [soft_candidate])

        self.assertEqual([span.member_observation_ids for span in result.spans], [(1, 2)])
        self.assertFalse(result.boundary_decisions[0]["split"])
        self.assertEqual(
            result.boundary_decisions[0]["boundary_candidate"]["classification"],
            "strong_candidate",
        )

    def test_hard_boundary_splits_even_when_reply_continues(self) -> None:
        rows = [
            message(1, "one", 0),
            message(2, "two", 100_000, reply_to="one"),
        ]
        hard = boundary(
            1, 2, boundary_type="hard", classification="hard_candidate", raw_score=0.7
        )

        result = segment_message_observations(rows, [hard])

        self.assertEqual([span.member_observation_ids for span in result.spans], [(1,), (2,)])
        self.assertEqual(result.spans[1].start_reason, "hard_boundary")
        self.assertEqual(result.reply_links[0].resolution, "resolved")

    def test_in_scope_direct_reply_bridges_an_inactivity_gap(self) -> None:
        rows = [
            message(1, "one", 0),
            message(2, "two", 100_000, reply_to="one"),
        ]

        result = segment_message_observations(rows)

        self.assertEqual([span.member_observation_ids for span in result.spans], [(1, 2)])
        self.assertEqual(result.reply_links[0].resolution, "resolved")

    def test_channels_and_capture_scopes_never_merge_but_cross_channel_reply_is_linked(self) -> None:
        rows = [
            message(1, "one", 0, channel_id=10, source_id=100),
            message(2, "two", 1, channel_id=11, source_id=101, reply_to="one"),
            message(3, "three", 2, channel_id=10, source_id=102),
        ]

        result = segment_message_observations(rows)

        self.assertEqual(
            {span.member_observation_ids for span in result.spans},
            {(1,), (2,), (3,)},
        )
        cross_channel = next(link for link in result.reply_links if link.source_observation_id == 2)
        self.assertEqual(cross_channel.resolution, "cross_scope")
        self.assertFalse(cross_channel.same_scope)

    def test_missing_scope_and_timestamps_are_retained_conservatively(self) -> None:
        rows = [
            message(1, "one", None, channel_id=None),
            message(2, "two", None, channel_id=None),
            message(3, "three", None, channel_id=10),
            message(4, "four", None, channel_id=10),
        ]

        result = segment_message_observations(rows)

        self.assertEqual(
            {span.member_observation_ids for span in result.spans},
            {(1,), (2,), (3, 4)},
        )

    def test_ids_are_deterministic_and_duplicate_observations_are_rejected(self) -> None:
        rows = [message(1, "one", 1), message(2, "two", 2)]
        first = segment_message_observations(rows)
        second = segment_message_observations(rows)
        self.assertEqual(
            [span.to_dict() for span in first.spans],
            [span.to_dict() for span in second.spans],
        )
        with self.assertRaisesRegex(ValueError, "Duplicate message_observation_id"):
            segment_message_observations([rows[0], rows[0]])

    def test_config_rejects_invalid_gap_threshold(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            SegmentationConfig(inactivity_gap_seconds=0)


class CandidateSegmentationPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_database(self.connection)
        self.export_path = Path(self.temp_dir.name) / "channel.json"
        self.export_path.write_text(json.dumps({
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
                    "content": "Second",
                    "reference": {"messageId": "one"},
                },
            ],
        }), encoding="utf-8")
        import_file(self.connection, self.export_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_persists_draft_spans_memberships_and_reply_evidence_separately(self) -> None:
        messages = load_current_message_observations(self.connection)
        run_id, result = run_candidate_segmentation(
            self.connection,
            messages,
            input_scope={"server_id": messages[0]["server_id"]},
        )

        run = self.connection.execute(
            "SELECT * FROM segmentation_runs WHERE segmentation_run_id = ?",
            (run_id,),
        ).fetchone()
        span_count = self.connection.execute(
            "SELECT COUNT(*) FROM candidate_spans WHERE segmentation_run_id = ?",
            (run_id,),
        ).fetchone()[0]
        memberships = self.connection.execute(
            """SELECT csm.assignment_status, csm.assignment_method, csm.message_observation_id
               FROM candidate_span_messages csm
               JOIN candidate_spans cs ON cs.candidate_span_id = csm.candidate_span_id
               WHERE cs.segmentation_run_id = ?
               ORDER BY cs.span_position, csm.position""",
            (run_id,),
        ).fetchall()
        reply_links = self.connection.execute(
            "SELECT resolution, same_scope FROM candidate_reply_links WHERE segmentation_run_id = ?",
            (run_id,),
        ).fetchall()

        self.assertEqual(run["status"], "completed")
        self.assertEqual(span_count, len(result.spans))
        self.assertEqual(len(memberships), 2)
        self.assertEqual({row["assignment_status"] for row in memberships}, {"candidate"})
        self.assertEqual({row["assignment_method"] for row in memberships}, {"automatic"})
        self.assertEqual(len(reply_links), 1)
        self.assertEqual(reply_links[0]["resolution"], "resolved")
        self.assertEqual(reply_links[0]["same_scope"], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scenes").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scene_messages").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_loader_returns_only_latest_accepted_import_for_current_capture(self) -> None:
        import_file(self.connection, self.export_path)
        rows = load_current_message_observations(self.connection)

        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["import_id"] for row in rows}), 1)
        self.assertEqual(
            [row["discord_message_id"] for row in rows],
            ["one", "two"],
        )

    def test_loader_filters_by_server_channel_and_source_scope(self) -> None:
        row = self.connection.execute(
            """SELECT c.server_id, m.channel_id, m.source_id
               FROM channels c JOIN messages m ON m.channel_id = c.channel_id
               LIMIT 1"""
        ).fetchone()
        result = load_current_message_observations(
            self.connection,
            server_id=row["server_id"],
            channel_id=row["channel_id"],
            source_id=row["source_id"],
        )

        self.assertEqual(len(result), 2)
        self.assertTrue(all(item["channel_id"] == row["channel_id"] for item in result))

    def test_loader_can_select_a_superseded_import_and_message_subset(self) -> None:
        original_import_id = int(self.connection.execute(
            "SELECT import_id FROM imports ORDER BY import_id LIMIT 1"
        ).fetchone()[0])
        original_message_ids = [
            int(row[0])
            for row in self.connection.execute(
                "SELECT message_observation_id FROM messages WHERE import_id = ? "
                "ORDER BY message_observation_id",
                (original_import_id,),
            )
        ]
        replacement = json.loads(self.export_path.read_text(encoding="utf-8"))
        replacement["messages"].append({
            "id": "three",
            "timestamp": "2026-01-01T00:02:00Z",
            "author": {"id": "player", "name": "Player"},
            "content": "Replacement capture",
        })
        self.export_path.write_text(json.dumps(replacement), encoding="utf-8")
        import_file(self.connection, self.export_path)

        current = load_current_message_observations(self.connection)
        historical = load_current_message_observations(
            self.connection,
            import_id=original_import_id,
        )
        subset = load_current_message_observations(
            self.connection,
            import_id=original_import_id,
            message_observation_ids=[original_message_ids[0]],
        )

        self.assertEqual(len(current), 3)
        self.assertEqual(
            [int(row["message_observation_id"]) for row in historical],
            original_message_ids,
        )
        self.assertEqual(
            [int(row["message_observation_id"]) for row in subset],
            [original_message_ids[0]],
        )
        self.assertEqual(load_current_message_observations(
            self.connection,
            message_observation_ids=[],
        ), [])

    def test_cross_channel_reply_is_persisted_as_a_link_without_merging_spans(self) -> None:
        other_path = Path(self.temp_dir.name) / "other-channel.json"
        other_path.write_text(json.dumps({
            "guild": {"id": "guild", "name": "Server"},
            "channel": {"id": "other-channel", "name": "Other"},
            "messages": [
                {
                    "id": "other",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "author": {"id": "player", "name": "Player"},
                    "content": "Cross-channel reply",
                    "reference": {"messageId": "one"},
                },
            ],
        }), encoding="utf-8")
        import_file(self.connection, other_path)
        messages = load_current_message_observations(self.connection)
        run_id, result = run_candidate_segmentation(
            self.connection,
            messages,
            input_scope={"server_id": messages[0]["server_id"]},
        )

        other_row = next(row for row in messages if row["discord_message_id"] == "other")
        target_row = next(row for row in messages if row["discord_message_id"] == "one")
        self.assertNotEqual(other_row["channel_id"], target_row["channel_id"])
        self.assertFalse(any(
            other_row["message_observation_id"] in span.member_observation_ids
            and target_row["message_observation_id"] in span.member_observation_ids
            for span in result.spans
        ))
        stored = self.connection.execute(
            """SELECT resolution, same_scope, target_observation_ids_json
               FROM candidate_reply_links
               WHERE segmentation_run_id = ? AND source_message_observation_id = ?""",
            (run_id, other_row["message_observation_id"]),
        ).fetchone()
        self.assertEqual(stored["resolution"], "cross_scope")
        self.assertEqual(stored["same_scope"], 0)
        self.assertEqual(
            json.loads(stored["target_observation_ids_json"]),
            [target_row["message_observation_id"]],
        )


if __name__ == "__main__":
    unittest.main()
