from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.attribution import create_character, propose_attribution, review_attribution
from archive.attribution_confidence import (
    AttributionConfidenceConfig,
    score_attribution_evidence,
)
from archive.attribution_confidence_persistence import (
    list_attribution_confidence_candidates,
    list_attribution_confidence_evidence,
    run_attribution_confidence,
)
from archive.identity_candidate_persistence import (
    review_identity_candidate,
    run_identity_inference,
)
from src.build_archive import create_database, import_file


def identity_evidence(
    observation_id: int,
    *,
    label: str,
    kind: str = "registered_character_match",
    character_ids: tuple[int, ...] = (10,),
    candidate_id: int | None = None,
    server_id: int | None = 1,
    evidence_id: int | None = None,
    run_id: int | None = 2,
    linked_character_id: int | None = None,
    review_event_ids: tuple[int, ...] = (),
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id or observation_id,
        "identity_inference_run_id": run_id,
        "message_observation_id": observation_id,
        "server_id": server_id,
        "normalized_label_key": label,
        "evidence_kind": kind,
        "candidate_id": candidate_id,
        "registered_character_ids_json": json.dumps(character_ids),
        "linked_character_id": linked_character_id,
        "candidate_link_review_event_ids_json": json.dumps(review_event_ids),
        "provisional_label": label,
        "character_label": f"Character {linked_character_id}" if linked_character_id else None,
    }


def reviewed_attribution(
    observation_id: int,
    character_id: int,
    *,
    status: str,
    attribution_id: int,
    review_event_ids: tuple[int, ...] = (100,),
) -> dict[str, object]:
    return {
        "message_observation_id": observation_id,
        "character_id": character_id,
        "character_name": f"Character {character_id}",
        "status": status,
        "attribution_id": attribution_id,
        "method": "manual",
        "review_event_ids_json": json.dumps(review_event_ids),
    }


class AttributionConfidenceModelTests(unittest.TestCase):
    def test_single_exact_alias_is_review_required_with_explicit_contribution(self) -> None:
        result = score_attribution_evidence([
            identity_evidence(1, label="alpha"),
        ])

        candidate = result.candidates[0]
        self.assertEqual(candidate.candidate_kind, "character")
        self.assertEqual(candidate.character_id, 10)
        self.assertEqual(candidate.score, 1)
        self.assertEqual(candidate.classification, "review_required")
        self.assertTrue(candidate.mandatory_review)
        self.assertEqual(candidate.evidence[0].evidence_type, "exact_registered_character_alias_match")
        self.assertEqual(candidate.evidence[0].source_message_ids, (1,))
        self.assertEqual(candidate.evidence[0].contribution, 1)

    def test_repeated_consistent_exact_labels_aggregate_distinct_messages(self) -> None:
        rows = [
            identity_evidence(index, label="alpha", evidence_id=index)
            for index in (1, 2, 3)
        ]
        result = score_attribution_evidence(rows)

        self.assertEqual(len(result.candidates), 3)
        for candidate in result.candidates:
            self.assertEqual(candidate.score, 3)
            self.assertEqual(candidate.score_margin, 3)
            self.assertEqual(candidate.classification, "high_confidence")
            self.assertFalse(candidate.mandatory_review)
            self.assertEqual(
                tuple(item.source_message_ids[0] for item in candidate.evidence),
                (1, 2, 3),
            )

    def test_ambiguous_alias_preserves_all_candidates_with_zero_margin(self) -> None:
        result = score_attribution_evidence([
            identity_evidence(
                1,
                label="shared",
                kind="ambiguous_registered_character_match",
                character_ids=(20, 21),
            ),
        ])

        self.assertEqual(
            [(item.character_id, item.score, item.rank) for item in result.candidates],
            [(20, 1, 1), (21, 1, 2)],
        )
        self.assertTrue(all(item.classification == "ambiguous" for item in result.candidates))
        self.assertTrue(all(item.score_margin == 0 for item in result.candidates))
        self.assertTrue(all(item.mandatory_review for item in result.candidates))

    def test_provisional_unknown_and_missing_evidence_remain_unresolved(self) -> None:
        result = score_attribution_evidence(
            [
                identity_evidence(
                    1, label="unknown", kind="provisional_label", character_ids=(),
                    candidate_id=50,
                ),
                identity_evidence(
                    2, label="unknown", kind="provisional_label", character_ids=(),
                    candidate_id=50,
                ),
            ],
            message_observation_ids=(3, 4),
        )

        by_message = {
            item.message_observation_id: item
            for item in result.candidates
        }
        self.assertEqual(by_message[1].candidate_kind, "provisional_identity")
        self.assertEqual(by_message[1].score, None)
        self.assertEqual(by_message[1].classification, "unresolved")
        self.assertEqual(len(by_message[1].evidence), 2)
        self.assertEqual(by_message[1].evidence[0].contribution, 0)
        self.assertEqual(by_message[3].candidate_kind, "unresolved")
        self.assertEqual(by_message[3].score, None)
        self.assertIn("no_usable_character_identity_evidence", by_message[3].review_reasons)
        self.assertTrue(by_message[3].mandatory_review)

    def test_missing_server_scope_does_not_aggregate_provisional_labels(self) -> None:
        result = score_attribution_evidence([
            identity_evidence(
                1, label="mira", kind="provisional_label", character_ids=(),
                candidate_id=60, server_id=None,
            ),
            identity_evidence(
                2, label="mira", kind="provisional_label", character_ids=(),
                candidate_id=61, server_id=None,
            ),
        ])

        self.assertEqual(
            {item.provisional_identity_candidate_id for item in result.candidates},
            {60, 61},
        )
        self.assertTrue(all(len(item.evidence) == 1 for item in result.candidates))

    def test_reviewed_decisions_are_contradiction_evidence_not_overwritten(self) -> None:
        result = score_attribution_evidence(
            [identity_evidence(1, label="beta", character_ids=(20,))],
            reviewed_attributions=[
                reviewed_attribution(1, 10, status="accepted", attribution_id=101),
                reviewed_attribution(1, 20, status="rejected", attribution_id=102),
            ],
        )

        by_character = {item.character_id: item for item in result.candidates}
        self.assertEqual(by_character[20].classification, "conflicted")
        self.assertEqual(by_character[20].decision_status, "rejected")
        self.assertEqual(by_character[20].score, -1)
        self.assertTrue(any(
            item.direction == "contradictory"
            and item.evidence_type == "reviewed_message_attribution_rejection"
            for item in by_character[20].evidence
        ))
        self.assertEqual(by_character[10].decision_status, "accepted")
        self.assertEqual(by_character[10].classification, "conflicted")
        self.assertTrue(any(
            item.evidence_type == "conflicting_human_accepted_attribution"
            and item.contribution == -1
            for item in by_character[20].evidence
        ))

    def test_pending_and_player_only_claims_do_not_increase_character_score(self) -> None:
        result = score_attribution_evidence(
            [identity_evidence(1, label="alpha")],
            reviewed_attributions=[
                {
                    **reviewed_attribution(1, 10, status="pending", attribution_id=105),
                    "review_event_ids_json": "[]",
                },
                {
                    **reviewed_attribution(1, 10, status="accepted", attribution_id=106),
                    "character_id": None,
                    "player_id": 44,
                },
            ],
        )

        candidate = result.candidates[0]
        self.assertEqual(candidate.score, 1)
        self.assertEqual(candidate.decision_status, "pending")
        self.assertEqual(candidate.classification, "review_required")
        self.assertTrue(candidate.mandatory_review)

    def test_thresholds_are_configurable_and_results_ignore_input_order(self) -> None:
        rows = [
            identity_evidence(2, label="alpha", evidence_id=2),
            identity_evidence(1, label="alpha", evidence_id=1),
        ]
        config = AttributionConfidenceConfig(
            high_confidence_min_score=2,
            high_confidence_min_margin=1,
        )
        forward = score_attribution_evidence(rows, config=config)
        reverse = score_attribution_evidence(list(reversed(rows)), config=config)

        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertTrue(all(item.classification == "high_confidence" for item in forward.candidates))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            AttributionConfidenceConfig(high_confidence_min_score=0)

    def test_malformed_phase_e_evidence_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "registered_character_ids_json must be valid JSON"):
            score_attribution_evidence([
                {
                    **identity_evidence(1, label="alpha"),
                    "registered_character_ids_json": "[10",
                }
            ])


class AttributionConfidencePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_database(self.connection)
        self.export_path = Path(self.temp_dir.name) / "confidence.json"
        messages = [
            ("alpha-1", "[Alpha]\nFirst"),
            ("alpha-2", "[Alpha]\nSecond"),
            ("alpha-3", "[Alpha]\nThird"),
            ("beta", "[Beta]\nCompeting character"),
            ("shared", "[Shared]\nAmbiguous alias"),
            ("unknown", "[Mira]\nProvisional identity"),
            ("unknown-2", "[ Mira ]\nRepeated provisional identity"),
            ("plain", "No speaker marker"),
        ]
        self.export_path.write_text(
            json.dumps({
                "guild": {"id": "server", "name": "Server"},
                "channel": {"id": "channel", "name": "Channel"},
                "messages": [
                    {
                        "id": discord_id,
                        "timestamp": f"2026-01-01T00:0{index}:00Z",
                        "author": {"id": "author", "name": "Author"},
                        "content": content,
                    }
                    for index, (discord_id, content) in enumerate(messages)
                ],
            }),
            encoding="utf-8",
        )
        import_file(self.connection, self.export_path)
        self.alpha_id = create_character(self.connection, "Alpha")
        self.beta_id = create_character(self.connection, "Beta")
        self.shared_ids = (
            create_character(self.connection, "Shared One"),
            create_character(self.connection, "Shared Two"),
        )
        from archive.attribution import add_character_alias
        add_character_alias(self.connection, self.shared_ids[0], "Shared")
        add_character_alias(self.connection, self.shared_ids[1], "Shared")
        self.messages = self.connection.execute(
            """SELECT m.message_observation_id, m.discord_message_id,
                      m.content_original, m.author_identity_id,
                      m.author_discord_user_id_original, m.webhook_id_original,
                      c.server_id
               FROM messages m LEFT JOIN channels c ON c.channel_id = m.channel_id
               ORDER BY m.message_observation_id"""
        ).fetchall()
        self.observation_ids = {
            row["discord_message_id"]: row["message_observation_id"]
            for row in self.messages
        }
        self.identity_run_id, self.identity_result = run_identity_inference(
            self.connection,
            self.messages,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_persists_reproducible_run_candidates_and_explainable_evidence(self) -> None:
        selected_ids = tuple(row["message_observation_id"] for row in self.messages)
        first_run, first = run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=selected_ids,
            input_scope={"server_id": self.messages[0]["server_id"]},
        )
        second_run, second = run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=selected_ids,
            input_scope={"server_id": self.messages[0]["server_id"]},
        )

        self.assertEqual(first.to_dict(), second.to_dict())
        first_fingerprint = self.connection.execute(
            "SELECT input_fingerprint FROM attribution_confidence_runs WHERE attribution_confidence_run_id = ?",
            (first_run,),
        ).fetchone()[0]
        second_fingerprint = self.connection.execute(
            "SELECT input_fingerprint FROM attribution_confidence_runs WHERE attribution_confidence_run_id = ?",
            (second_run,),
        ).fetchone()[0]
        self.assertEqual(first_fingerprint, second_fingerprint)
        run = self.connection.execute(
            """SELECT ruleset_version, configuration_json, status, statistics_json
               FROM attribution_confidence_runs WHERE attribution_confidence_run_id = ?""",
            (first_run,),
        ).fetchone()
        self.assertEqual(run["ruleset_version"], "attribution_confidence_v1")
        self.assertEqual(run["status"], "completed")
        self.assertFalse(json.loads(run["configuration_json"])["automatic_acceptance"])

        alpha_message_id = self.observation_ids["alpha-1"]
        alpha_rows = list_attribution_confidence_candidates(
            self.connection, first_run, alpha_message_id
        )
        alpha_candidate = next(row for row in alpha_rows if row["character_id"] == self.alpha_id)
        self.assertEqual(alpha_candidate["score"], 3)
        self.assertEqual(alpha_candidate["classification"], "high_confidence")
        contribution_rows = list_attribution_confidence_evidence(
            self.connection,
            alpha_candidate["attribution_confidence_candidate_id"],
        )
        self.assertEqual(len(contribution_rows), 3)
        self.assertEqual(
            {row["source_identity_inference_run_id"] for row in contribution_rows},
            {self.identity_run_id},
        )

        ambiguous_rows = list_attribution_confidence_candidates(
            self.connection,
            first_run,
            self.observation_ids["shared"],
        )
        self.assertEqual(
            {row["character_id"] for row in ambiguous_rows if row["character_id"] is not None},
            set(self.shared_ids),
        )
        self.assertTrue(all(row["classification"] == "ambiguous" for row in ambiguous_rows))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM message_attributions").fetchone()[0],
            0,
        )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scenes").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scene_messages").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_human_decision_is_retained_and_conflicting_inference_is_surfaced(self) -> None:
        message_id = self.observation_ids["beta"]
        human_attribution_id = propose_attribution(
            self.connection,
            message_id,
            character_id=self.alpha_id,
            method="manual",
            evidence={"source": "review"},
        )
        review_attribution(
            self.connection,
            human_attribution_id,
            decision="accepted",
            actor="reviewer",
            note="Reviewed before F1 run.",
        )

        run_id, _ = run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=[message_id],
        )
        candidate_rows = list_attribution_confidence_candidates(
            self.connection, run_id, message_id
        )
        beta = next(row for row in candidate_rows if row["character_id"] == self.beta_id)
        alpha = next(row for row in candidate_rows if row["character_id"] == self.alpha_id)
        self.assertEqual(beta["classification"], "conflicted")
        self.assertEqual(beta["decision_status"], "none")
        self.assertEqual(alpha["classification"], "conflicted")
        self.assertEqual(alpha["decision_status"], "accepted")
        contradiction_rows = list_attribution_confidence_evidence(
            self.connection, beta["attribution_confidence_candidate_id"]
        )
        self.assertTrue(any(
            row["direction"] == "contradictory"
            and row["source_attribution_id"] == human_attribution_id
            for row in contradiction_rows
        ))

        run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=[message_id],
        )
        saved_decision = self.connection.execute(
            "SELECT status FROM message_attributions WHERE attribution_id = ?",
            (human_attribution_id,),
        ).fetchone()[0]
        self.assertEqual(saved_decision, "accepted")

    def test_no_evidence_and_pending_proposal_are_persisted_as_review_states(self) -> None:
        message_id = self.observation_ids["plain"]
        pending_id = propose_attribution(
            self.connection,
            message_id,
            character_id=self.alpha_id,
            method="rule",
            evidence={"source": "legacy pending proposal"},
        )
        run_id, _ = run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=[message_id],
        )

        row = list_attribution_confidence_candidates(self.connection, run_id, message_id)[0]
        self.assertEqual(row["candidate_kind"], "character")
        self.assertEqual(row["score"], 0)
        self.assertEqual(row["classification"], "review_required")
        self.assertEqual(row["decision_status"], "pending")
        contribution = list_attribution_confidence_evidence(
            self.connection, row["attribution_confidence_candidate_id"]
        )[0]
        self.assertEqual(contribution["evidence_type"], "pending_message_attribution_proposal")
        self.assertEqual(contribution["contribution"], 0)
        self.assertEqual(contribution["source_attribution_id"], pending_id)

        empty_message = self.observation_ids["unknown"]
        provisional_run, _ = run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=[empty_message],
        )
        provisional = list_attribution_confidence_candidates(
            self.connection, provisional_run, empty_message
        )[0]
        self.assertEqual(provisional["candidate_kind"], "provisional_identity")
        self.assertIsNone(provisional["score"])
        self.assertEqual(provisional["classification"], "unresolved")

    def test_reviewed_provisional_character_link_is_evidence_not_message_attribution(self) -> None:
        provisional_id = self.connection.execute(
            """SELECT candidate_id FROM provisional_identity_candidates
               WHERE normalized_label_key = 'mira'"""
        ).fetchone()[0]
        observations = [
            self.observation_ids["unknown"],
            self.observation_ids["unknown-2"],
        ]
        review_identity_candidate(
            self.connection,
            int(provisional_id),
            decision="link_to_character",
            actor="reviewer",
            character_id=self.alpha_id,
            note="Reviewed provisional label mapping.",
        )

        run_id, _ = run_attribution_confidence(
            self.connection,
            self.identity_run_id,
            message_observation_ids=observations,
        )
        for message_id in observations:
            candidate = list_attribution_confidence_candidates(
                self.connection, run_id, message_id
            )[0]
            self.assertEqual(candidate["candidate_kind"], "character")
            self.assertEqual(candidate["character_id"], self.alpha_id)
            self.assertEqual(candidate["classification"], "review_required")
            self.assertEqual(candidate["decision_status"], "none")
            evidence = list_attribution_confidence_evidence(
                self.connection, candidate["attribution_confidence_candidate_id"]
            )
            self.assertEqual(len(evidence), 2)
            self.assertTrue(all(
                item["evidence_type"] == "reviewed_provisional_identity_link"
                and item["direction"] == "supporting"
                for item in evidence
            ))
            self.assertTrue(all(
                item["source_review_event_ids_json"] != "[]"
                for item in evidence
            ))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM message_attributions"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
