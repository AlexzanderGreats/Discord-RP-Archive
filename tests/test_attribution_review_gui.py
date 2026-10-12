from __future__ import annotations

import sqlite3
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from archive.attribution_review_gui import (
    AttributionReviewService,
    AttributionReviewTab,
    format_attribution_audit,
    format_attribution_evidence,
    sort_review_queue,
)


class AttributionReviewGuiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.service = AttributionReviewService(lambda: nullcontext(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_queue_uses_f2_filters_and_bounded_page(self) -> None:
        queue = [{"message_observation_id": index} for index in range(3)]
        with patch(
            "archive.attribution_review_gui.list_attribution_review_queue",
            return_value=queue,
        ) as list_queue:
            result = self.service.load_queue(
                offset=100,
                limit=2,
                filters={
                    "priority": "high",
                    "state": "stale",
                    "classification": "conflicted",
                    "kind": "character",
                    "label": "Winter",
                    "server": "Test server",
                    "channel": "rp",
                    "stale": "Stale",
                },
            )

        self.assertEqual(result, {"items": queue[:2], "has_more": True})
        kwargs = list_queue.call_args.kwargs
        self.assertEqual(kwargs["offset"], 100)
        self.assertEqual(kwargs["priorities"], ("high",))
        self.assertEqual(kwargs["states"], ("stale",))
        self.assertEqual(kwargs["classifications"], ("conflicted",))
        self.assertEqual(kwargs["candidate_kinds"], ("character",))
        self.assertEqual(kwargs["label_query"], "Winter")
        self.assertEqual(kwargs["server_name"], "Test server")
        self.assertEqual(kwargs["channel_name"], "rp")
        self.assertTrue(kwargs["stale_only"])
        self.assertFalse(kwargs["include_evidence"])
        self.assertFalse(kwargs["include_message_content"])

    def test_review_action_calls_f2_then_reloads_detail_and_queue(self) -> None:
        with (
            patch(
                "archive.attribution_review_gui.review_attribution_candidate",
                return_value=[88],
            ) as review,
            patch(
                "archive.attribution_review_gui.get_attribution_review_candidate",
                return_value={"candidate": {"attribution_confidence_candidate_id": 7}},
            ) as get_candidate,
            patch(
                "archive.attribution_review_gui.list_attribution_review_queue",
                return_value=[{"message_observation_id": 2}],
            ) as list_queue,
        ):
            result = self.service.perform_action(
                confidence_run_id=3,
                confidence_candidate_id=7,
                action="confirm",
                actor="reviewer",
                note="Reviewed in F3",
                character_id=12,
                target_candidate_id=None,
                message_ids=[2],
                expected_latest_event_id=5,
                queue_offset=0,
                filters={},
            )

        self.assertEqual(result["event_ids"], [88])
        self.assertEqual(result["detail"]["candidate"]["attribution_confidence_candidate_id"], 7)
        self.assertEqual(result["queue"]["items"], [{"message_observation_id": 2}])
        self.assertEqual(review.call_args.kwargs["expected_latest_event_id"], 5)
        self.assertEqual(review.call_args.kwargs["message_observation_ids"], [2])
        self.assertTrue(get_candidate.called)
        self.assertTrue(list_queue.called)

    def test_review_action_variants_are_delegated_to_f2(self) -> None:
        actions = (
            "confirm", "assign_character", "reject", "leave_unresolved",
            "link_provisional", "exclude_evidence", "restore_evidence",
            "split_evidence", "reassign_evidence", "merge_candidates",
        )
        for action in actions:
            with self.subTest(action=action):
                with (
                    patch(
                        "archive.attribution_review_gui.review_attribution_candidate",
                        return_value=[1],
                    ) as review,
                    patch(
                        "archive.attribution_review_gui.get_attribution_review_candidate",
                        return_value={"candidate": {}},
                    ),
                    patch(
                        "archive.attribution_review_gui.list_attribution_review_queue",
                        return_value=[],
                    ),
                ):
                    self.service.perform_action(
                        confidence_run_id=3,
                        confidence_candidate_id=7,
                        action=action,  # type: ignore[arg-type]
                        actor="reviewer",
                        note=None,
                        character_id=12 if action in {"confirm", "assign_character", "link_provisional"} else None,
                        target_candidate_id=19 if action in {"reassign_evidence", "merge_candidates"} else None,
                        message_ids=[2],
                        expected_latest_event_id=0,
                        queue_offset=0,
                        filters={},
                    )
                self.assertEqual(review.call_args.kwargs["action"], action)
                self.assertEqual(review.call_args.kwargs["confidence_candidate_id"], 7)

    def test_failed_review_action_rolls_back_without_reloading_as_success(self) -> None:
        self.connection.execute("CREATE TABLE marker (value INTEGER)")
        self.connection.execute("INSERT INTO marker VALUES (1)")
        with (
            patch(
                "archive.attribution_review_gui.review_attribution_candidate",
                side_effect=ValueError("stale candidate"),
            ),
            patch("archive.attribution_review_gui.get_attribution_review_candidate") as get_candidate,
            patch("archive.attribution_review_gui.list_attribution_review_queue") as list_queue,
        ):
            with self.assertRaisesRegex(ValueError, "stale candidate"):
                self.service.perform_action(
                    confidence_run_id=3,
                    confidence_candidate_id=7,
                    action="confirm",
                    actor="reviewer",
                    note=None,
                    character_id=12,
                    target_candidate_id=None,
                    message_ids=[2],
                    expected_latest_event_id=5,
                    queue_offset=0,
                    filters={},
                )

        self.assertFalse(self.connection.in_transaction)
        get_candidate.assert_not_called()
        list_queue.assert_not_called()


class AttributionReviewPresentationTests(unittest.TestCase):
    def test_evidence_formatter_preserves_source_text_and_distinguishes_identity(self) -> None:
        formatted = format_attribution_evidence({
            "discord_message_id": "discord-1",
            "message_observation_id": 42,
            "timestamp_utc": "2026-10-01T12:00:00Z",
            "server_name": "Server",
            "channel_name": "rp",
            "author_display_name_original": "Discord User",
            "author_discord_user_id_original": "user-7",
            "source_id": 9,
            "content_original": "[Hero]\n  Original text ",
            "candidate_kind": "character",
            "classification": "review_required",
            "score": 2,
            "is_excluded": True,
            "evidence": [{
                "direction": "contradictory",
                "evidence_strength": "explicit",
                "evidence_type": "speaker_label",
                "weight": 1,
                "contribution": -1,
                "evidence": {"speaker_label": "Villain"},
            }],
        })

        self.assertIn("Discord author: Discord User (ID user-7)", formatted)
        self.assertIn("Original message text (unchanged):\n[Hero]\n  Original text ", formatted)
        self.assertIn("CONTRADICTORY / explicit / speaker_label", formatted)
        self.assertIn("operational score 2", formatted)
        self.assertIn("Evidence state: excluded", formatted)

    def test_audit_formatter_displays_human_action_and_previous_state(self) -> None:
        formatted = format_attribution_audit({
            "audit_history": [{
                "happened_at": "2026-10-01T12:00:00Z",
                "action": "assign_character",
                "outcome": "accepted",
                "actor": "reviewer",
                "note": "Verified manually",
                "previous_state_json": '{"status":"unresolved"}',
                "detail_json": '{"affected_message_observation_ids":[42]}',
            }],
            "current_authoritative_attributions": [{
                "character_name": "Hero",
                "player_name": None,
                "status": "accepted",
            }],
        })

        self.assertIn("assign_character / accepted", formatted)
        self.assertIn("Reviewer: reviewer", formatted)
        self.assertIn('{"status":"unresolved"}', formatted)
        self.assertIn('"character_name": "Hero"', formatted)
        self.assertIn('"player_name": null', formatted)

    def test_stale_queue_item_is_visibly_distinguished(self) -> None:
        state = AttributionReviewTab._display_state({
            "stale": True,
            "state": "review_required",
            "reviewed": False,
            "human_decision_status": None,
        })

        self.assertEqual(state, "stale")

    def test_queue_sorting_uses_existing_f2_values(self) -> None:
        low = {
            "message_observation_id": 1,
            "candidates": [{
                "character_name": "B",
                "score": 1,
                "evidence_count": 2,
            }],
        }
        high = {
            "message_observation_id": 2,
            "candidates": [{
                "character_name": "A",
                "score": 4,
                "evidence_count": 5,
            }],
        }

        self.assertEqual([item["message_observation_id"] for item in sort_review_queue(
            [low, high], "Label"
        )], [2, 1])
        self.assertEqual([item["message_observation_id"] for item in sort_review_queue(
            [low, high], "Operational score"
        )], [2, 1])
        self.assertEqual([item["message_observation_id"] for item in sort_review_queue(
            [low, high], "Evidence count"
        )], [2, 1])

    def test_review_state_display_distinguishes_reviewed_unresolved(self) -> None:
        self.assertEqual(
            AttributionReviewTab._display_state({
                "stale": False,
                "state": "review_required",
                "reviewed": False,
                "human_decision_status": None,
            }),
            "Never reviewed · review_required",
        )
        self.assertEqual(
            AttributionReviewTab._display_state({
                "stale": False,
                "state": "unresolved",
                "reviewed": True,
                "human_decision_status": "unresolved",
            }),
            "Reviewed — unresolved",
        )


if __name__ == "__main__":
    unittest.main()
