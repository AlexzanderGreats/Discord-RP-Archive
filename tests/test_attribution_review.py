from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.attribution import add_character_alias, create_character
from archive.attribution_confidence_persistence import run_attribution_confidence
from archive.attribution_review import (
    list_attribution_review_history,
    list_attribution_review_queue,
    review_attribution_candidate,
)
from archive.identity_candidate_persistence import run_identity_inference
from src.build_archive import create_database, import_file


class AttributionReviewWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_database(self.connection)
        self.path = Path(self.temp_dir.name) / "review.json"
        messages = [
            ("hero-one", "[Hero]\nFirst"),
            ("hero-two", "[Hero]\nSecond"),
            ("champion", "[Champion]\nAlternate alias"),
            ("mira-one", "[Mira]\nUnregistered"),
            ("mira-two", "[ mira ]\nRepeated unregistered"),
            ("other-label", "[Other Label]\nAnother provisional identity"),
            ("plain", "No speaker evidence"),
        ]
        self.path.write_text(
            json.dumps({
                "guild": {"id": "server", "name": "Server"},
                "channel": {"id": "channel", "name": "Channel"},
                "messages": [
                    {
                        "id": discord_id,
                        "timestamp": f"2026-01-01T00:0{index}:00Z",
                        "author": {"id": f"user-{index}", "name": f"User {index}"},
                        "content": content,
                    }
                    for index, (discord_id, content) in enumerate(messages)
                ],
            }),
            encoding="utf-8",
        )
        import_file(self.connection, self.path)
        self.hero_id = create_character(self.connection, "Hero")
        self.other_id = create_character(self.connection, "Other")
        add_character_alias(self.connection, self.hero_id, "Champion")
        rows = self.connection.execute(
            """SELECT message.message_observation_id, message.discord_message_id,
                      message.content_original, channel.server_id,
                      message.author_identity_id, message.author_discord_user_id_original,
                      message.webhook_id_original
               FROM messages message JOIN channels channel ON channel.channel_id = message.channel_id
               ORDER BY message.message_observation_id"""
        ).fetchall()
        self.messages = rows
        self.message_ids = {row["discord_message_id"]: row["message_observation_id"] for row in rows}
        self.identity_run_id, _ = run_identity_inference(self.connection, rows)
        self.confidence_run_id = self._confidence_run()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _confidence_run(self) -> int:
        run_id, _ = run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=tuple(self.message_ids.values()),
        )
        return run_id

    def _candidate(self, discord_message_id: str, kind: str, value: int | None = None) -> int:
        message_id = self.message_ids[discord_message_id]
        row = self.connection.execute(
            """SELECT attribution_confidence_candidate_id
               FROM attribution_confidence_candidates
               WHERE attribution_confidence_run_id = ? AND message_observation_id = ?
                 AND candidate_kind = ?
                 AND (character_id IS ? OR provisional_identity_candidate_id IS ?)
               ORDER BY candidate_rank LIMIT 1""",
            (self.confidence_run_id, message_id, kind, value, value),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row[0])

    def _review(self, discord_message_id: str, action: str, **kwargs: object) -> list[int]:
        return review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=self._candidate(
                discord_message_id,
                str(kwargs.pop("candidate_kind", "character")),
                kwargs.pop("candidate_ref", self.hero_id if action in {"confirm", "reject"} else None),
            ),
            action=action,  # type: ignore[arg-type]
            actor="reviewer",
            **kwargs,  # type: ignore[arg-type]
        )

    def _provisional_id(self, discord_message_id: str) -> int:
        candidate_id = self._candidate(discord_message_id, "provisional_identity")
        return int(self.connection.execute(
            """SELECT provisional_identity_candidate_id
               FROM attribution_confidence_candidates
               WHERE attribution_confidence_candidate_id = ?""",
            (candidate_id,),
        ).fetchone()[0])

    def test_queue_contains_evidence_prioritization_and_current_decision(self) -> None:
        queue = list_attribution_review_queue(self.connection, confidence_run_id=self.confidence_run_id)
        by_message = {row["discord_message_id"]: row for row in queue}
        self.assertIn("hero-one", by_message)
        self.assertIn("plain", by_message)
        self.assertEqual(by_message["hero-one"]["state"], "queued")
        self.assertEqual(by_message["hero-one"]["candidates"][0]["character_id"], self.hero_id)
        self.assertTrue(by_message["hero-one"]["candidates"][0]["evidence"])
        self.assertEqual(by_message["hero-one"]["review_priority"], "normal")

    def test_bulk_confirm_requires_identical_evidence_and_survives_recomputation(self) -> None:
        first_message = self.message_ids["hero-one"]
        second_message = self.message_ids["hero-two"]
        candidate_id = self._candidate("hero-one", "character", self.hero_id)
        event_ids = review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=candidate_id,
            action="confirm",
            actor="reviewer",
            note="Reviewed identical speaker labels.",
            message_observation_ids=[first_message, second_message],
        )
        self.assertEqual(len(event_ids), 2)
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM message_attributions
                   WHERE character_id = ? AND status = 'accepted'""",
                (self.hero_id,),
            ).fetchone()[0],
            2,
        )
        refreshed_run = self._confidence_run()
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM attribution_review_decisions
                   WHERE status = 'accepted' AND character_id = ?""",
                (self.hero_id,),
            ).fetchone()[0],
            2,
        )
        refreshed_queue = list_attribution_review_queue(
            self.connection, confidence_run_id=refreshed_run
        )
        self.assertNotIn("hero-one", {row["discord_message_id"] for row in refreshed_queue})
        decided = list_attribution_review_queue(
            self.connection, confidence_run_id=refreshed_run, include_decided=True
        )
        hero_decision = next(row for row in decided if row["discord_message_id"] == "hero-one")
        self.assertEqual(hero_decision["state"], "accepted")
        history = list_attribution_review_history(self.connection, first_message)
        self.assertEqual(history[0]["action"], "confirm")
        self.assertEqual(history[0]["note"], "Reviewed identical speaker labels.")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """UPDATE attribution_review_events SET actor = 'tampered'
                   WHERE attribution_review_event_id = ?""",
                (event_ids[0],),
            )
        with self.assertRaisesRegex(ValueError, "stale"):
            review_attribution_candidate(
                self.connection,
                confidence_run_id=self.confidence_run_id,
                confidence_candidate_id=candidate_id,
                action="confirm",
                actor="reviewer",
            )

    def test_bulk_review_rejects_different_label_evidence(self) -> None:
        candidate_id = self._candidate("hero-one", "character", self.hero_id)
        with self.assertRaisesRegex(ValueError, "identical evidence"):
            review_attribution_candidate(
                self.connection,
                confidence_run_id=self.confidence_run_id,
                confidence_candidate_id=candidate_id,
                action="confirm",
                actor="reviewer",
                message_observation_ids=[
                    self.message_ids["hero-one"], self.message_ids["champion"]
                ],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM attribution_review_events").fetchone()[0],
            0,
        )

    def test_reject_candidate_and_conflict_with_existing_acceptance(self) -> None:
        self._review("hero-one", "confirm")
        self.confidence_run_id = self._confidence_run()
        event_ids = self._review("hero-one", "reject")
        row = self.connection.execute(
            """SELECT outcome FROM attribution_review_events
               WHERE attribution_review_event_id = ?""",
            (event_ids[0],),
        ).fetchone()
        self.assertEqual(row["outcome"], "conflicted")
        self.assertEqual(
            self.connection.execute(
                """SELECT status FROM message_attributions
                   WHERE message_observation_id = ? AND character_id = ?""",
                (self.message_ids["hero-one"], self.hero_id),
            ).fetchone()[0],
            "accepted",
        )

    def test_competing_human_acceptance_raises_review_priority(self) -> None:
        from archive.attribution import propose_attribution, review_attribution

        claim_id = propose_attribution(
            self.connection,
            self.message_ids["hero-one"],
            character_id=self.other_id,
            method="manual",
            evidence={"source": "independent review"},
        )
        review_attribution(
            self.connection,
            claim_id,
            decision="accepted",
            actor="reviewer",
            note="Conflicting accepted character.",
        )
        self.confidence_run_id = self._confidence_run()
        queue = list_attribution_review_queue(
            self.connection, confidence_run_id=self.confidence_run_id
        )
        conflicted = next(item for item in queue if item["discord_message_id"] == "hero-one")
        self.assertEqual(conflicted["state"], "conflicted")
        self.assertEqual(conflicted["review_priority"], "high")
        self.assertTrue(any(
            item["classification"] == "conflicted"
            for item in conflicted["candidates"]
        ))

    def test_assign_different_character_supersedes_prior_acceptance(self) -> None:
        self._review("hero-one", "confirm")
        self.confidence_run_id = self._confidence_run()
        event_ids = self._review(
            "hero-one", "assign_character", character_id=self.other_id
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT character_id FROM message_attributions
                   WHERE message_observation_id = ? AND status = 'accepted'""",
                (self.message_ids["hero-one"],),
            ).fetchone()[0],
            self.other_id,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT status FROM attribution_review_decisions
                   WHERE message_observation_id = ? AND character_id = ?""",
                (self.message_ids["hero-one"], self.hero_id),
            ).fetchone()[0],
            "superseded",
        )
        self.assertEqual(len(event_ids), 1)

    def test_leave_unresolved_is_durable_and_link_does_not_attribute_message(self) -> None:
        plain_candidate = self._candidate("plain", "unresolved")
        review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=plain_candidate,
            action="leave_unresolved",
            actor="reviewer",
            note="Not enough identity evidence.",
        )
        mira_candidate = self._candidate("mira-one", "provisional_identity")
        review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=mira_candidate,
            action="link_provisional",
            actor="reviewer",
            character_id=self.other_id,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM message_attributions
                   WHERE message_observation_id = ?""",
                (self.message_ids["mira-one"],),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT resolution_status, linked_character_id
                   FROM provisional_identity_candidates WHERE candidate_id = ?""",
                (self._provisional_id("mira-one"),),
            ).fetchone()[:],
            ("linked", self.other_id),
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT status FROM attribution_review_decisions
                   WHERE message_observation_id = ?""",
                (self.message_ids["plain"],),
            ).fetchone()[0],
            "unresolved",
        )

    def test_split_and_merge_reuse_durable_phase_e_overrides(self) -> None:
        split_candidate = self._candidate("mira-one", "provisional_identity")
        split_id = review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=split_candidate,
            action="split_evidence",
            actor="reviewer",
            message_observation_ids=[self.message_ids["mira-one"]],
        )
        self.assertEqual(len(split_id), 1)
        split_event = self.connection.execute(
            """SELECT identity_review_event_id, outcome FROM attribution_review_events
               WHERE attribution_review_event_id = ?""",
            (split_id[0],),
        ).fetchone()
        self.assertEqual(split_event["outcome"], "evidence_adjusted")
        self.assertIsNotNone(split_event["identity_review_event_id"])
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM identity_candidate_evidence_overrides
                   WHERE message_observation_id = ?""",
                (self.message_ids["mira-one"],),
            ).fetchone()[0],
            1,
        )
        split_target = self.connection.execute(
            """SELECT target_candidate_id FROM attribution_review_events
               WHERE attribution_review_event_id = ?""",
            (split_id[0],),
        ).fetchone()[0]
        self.identity_run_id, _ = run_identity_inference(self.connection, self.messages)
        self.confidence_run_id = self._confidence_run()
        split_result = self.connection.execute(
            """SELECT provisional_identity_candidate_id
               FROM attribution_confidence_candidates
               WHERE attribution_confidence_run_id = ? AND message_observation_id = ?
               ORDER BY candidate_rank LIMIT 1""",
            (self.confidence_run_id, self.message_ids["mira-one"]),
        ).fetchone()[0]
        self.assertEqual(split_result, split_target)

    def test_reassignment_and_exclusion_are_respected_by_recomputed_f1(self) -> None:
        source_candidate_id = self._provisional_id("mira-one")
        target_candidate_id = self._provisional_id("other-label")
        reassign_candidate = self._candidate("mira-one", "provisional_identity", source_candidate_id)
        reassign_events = review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=reassign_candidate,
            action="reassign_evidence",
            actor="reviewer",
            target_candidate_id=target_candidate_id,
            message_observation_ids=[self.message_ids["mira-one"]],
        )
        self.assertEqual(len(reassign_events), 1)

        self.identity_run_id, _ = run_identity_inference(
            self.connection,
            self.messages,
        )
        self.confidence_run_id = self._confidence_run()
        reassigned = self.connection.execute(
            """SELECT candidate_kind, provisional_identity_candidate_id
               FROM attribution_confidence_candidates
               WHERE attribution_confidence_run_id = ? AND message_observation_id = ?
               ORDER BY candidate_rank LIMIT 1""",
            (self.confidence_run_id, self.message_ids["mira-one"]),
        ).fetchone()
        self.assertEqual(tuple(reassigned), ("provisional_identity", target_candidate_id))

        source_candidate_id = self._provisional_id("mira-two")
        exclude_candidate = self._candidate(
            "mira-two", "provisional_identity", source_candidate_id
        )
        review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=exclude_candidate,
            action="exclude_evidence",
            actor="reviewer",
            message_observation_ids=[self.message_ids["mira-two"]],
        )
        self.identity_run_id, _ = run_identity_inference(
            self.connection,
            self.messages,
        )
        self.confidence_run_id = self._confidence_run()
        excluded = self.connection.execute(
            """SELECT candidate_kind FROM attribution_confidence_candidates
               WHERE attribution_confidence_run_id = ? AND message_observation_id = ?
               ORDER BY candidate_rank LIMIT 1""",
            (self.confidence_run_id, self.message_ids["mira-two"]),
        ).fetchone()
        self.assertEqual(excluded["candidate_kind"], "unresolved")

    def test_merge_moves_all_active_evidence_to_target_identity(self) -> None:
        source_candidate_id = self._provisional_id("mira-one")
        target_candidate_id = self._provisional_id("other-label")
        selected_candidate = self._candidate(
            "mira-one", "provisional_identity", source_candidate_id
        )
        event_ids = review_attribution_candidate(
            self.connection,
            confidence_run_id=self.confidence_run_id,
            confidence_candidate_id=selected_candidate,
            action="merge_candidates",
            actor="reviewer",
            target_candidate_id=target_candidate_id,
        )
        self.assertEqual(len(event_ids), 2)
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM identity_candidate_evidence_overrides
                   WHERE candidate_id = ? AND is_excluded = 0""",
                (target_candidate_id,),
            ).fetchone()[0],
            2,
        )
        self.identity_run_id, _ = run_identity_inference(self.connection, self.messages)
        self.confidence_run_id = self._confidence_run()
        for discord_message_id in ("mira-one", "mira-two"):
            merged = self.connection.execute(
                """SELECT provisional_identity_candidate_id
                   FROM attribution_confidence_candidates
                   WHERE attribution_confidence_run_id = ? AND message_observation_id = ?
                   ORDER BY candidate_rank LIMIT 1""",
                (self.confidence_run_id, self.message_ids[discord_message_id]),
            ).fetchone()[0]
            self.assertEqual(merged, target_candidate_id)

    def test_stale_after_identity_review_is_rejected_before_manual_change(self) -> None:
        candidate_id = self._candidate("mira-one", "provisional_identity")
        provisional_id = self._provisional_id("mira-one")
        from archive.identity_candidate_persistence import review_identity_candidate

        review_identity_candidate(
            self.connection,
            provisional_id,
            decision="leave_unresolved",
            actor="reviewer",
            note="Reviewed in Phase E.",
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            review_attribution_candidate(
                self.connection,
                confidence_run_id=self.confidence_run_id,
                confidence_candidate_id=candidate_id,
                action="link_provisional",
                actor="reviewer",
                character_id=self.other_id,
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM attribution_review_events").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
