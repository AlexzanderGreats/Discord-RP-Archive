from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.attribution import add_character_alias, create_character
from archive.identity_candidate_persistence import (
    list_identity_candidate_evidence,
    list_provisional_identity_candidates,
    review_identity_candidate,
    run_identity_inference,
)
from archive.identity_inference import infer_identity_candidates
from src.build_archive import create_database, import_file


def message(
    observation_id: int,
    content: object,
    *,
    author_identity_id: int | None = None,
    author_id: str | None = None,
    server_id: int | None = 1,
) -> dict[str, object]:
    return {
        "message_observation_id": observation_id,
        "discord_message_id": f"discord-{observation_id}",
        "server_id": server_id,
        "content_original": content,
        "author_identity_id": author_identity_id,
        "author_discord_user_id_original": author_id,
        "webhook_id_original": None,
    }


class IdentityInferenceTests(unittest.TestCase):
    def test_groups_repeated_unmatched_labels_and_preserves_observed_spelling(self) -> None:
        result = infer_identity_candidates([
            message(2, "[ MIRA ]\nSecond", author_identity_id=5),
            message(1, "[Mira]\nFirst", author_id="discord-user"),
        ])

        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.display_label, "Mira")
        self.assertEqual(candidate.normalized_label, "mira")
        self.assertEqual(candidate.message_observation_ids, (1, 2))
        self.assertEqual([item.observed_label for item in result.evidence], ["Mira", " MIRA "])
        self.assertTrue(all(item.evidence_kind == "provisional_label" for item in result.evidence))
        self.assertTrue(all(item.evidence["player_inferred"] is False for item in result.evidence))

    def test_registered_and_ambiguous_aliases_are_not_provisional_candidates(self) -> None:
        result = infer_identity_candidates(
            [message(1, "[Known]\nText"), message(2, "[Shared]\nText")],
            registered_aliases={"known": [10], "shared": [20, 21]},
        )

        self.assertEqual(result.candidates, ())
        self.assertEqual(
            [(item.evidence_kind, item.registered_character_ids) for item in result.evidence],
            [
                ("registered_character_match", (10,)),
                ("ambiguous_registered_character_match", (20, 21)),
            ],
        )
        self.assertIsNone(result.evidence[1].candidate_key)

    def test_same_label_does_not_merge_across_servers_or_unknown_scope(self) -> None:
        result = infer_identity_candidates([
            message(1, "[Mira]\nOne", server_id=1),
            message(2, "[Mira]\nTwo", server_id=2),
            message(3, "[Mira]\nUnknown scope", server_id=None),
            message(4, "[Mira]\nUnknown scope", server_id=None),
        ])

        self.assertEqual(len(result.candidates), 4)
        self.assertEqual(
            {candidate.server_id for candidate in result.candidates},
            {1, 2, None},
        )
        self.assertEqual(
            sorted(len(candidate.message_observation_ids) for candidate in result.candidates),
            [1, 1, 1, 1],
        )

    def test_missing_and_malformed_content_produce_no_candidate(self) -> None:
        result = infer_identity_candidates([
            message(1, "No speaker marker"),
            message(2, None),
            message(3, 42),
            message(4, "[  ]\nEmpty marker"),
        ])

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.evidence, ())

    def test_malformed_identity_observations_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "integer message_observation_id"):
            infer_identity_candidates([{"discord_message_id": "missing"}])
        row = message(1, "[Name]\nText")
        with self.assertRaisesRegex(ValueError, "Duplicate message_observation_id"):
            infer_identity_candidates([row, row])
        with self.assertRaisesRegex(ValueError, "positive integers"):
            infer_identity_candidates(
                [message(1, "[Name]\nText")],
                registered_aliases={"name": [0]},
            )


class IdentityCandidatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_database(self.connection)
        self.export_path = Path(self.temp_dir.name) / "channel.json"
        self.export_path.write_text(
            json.dumps({
                "guild": {"id": "server", "name": "Server"},
                "channel": {"id": "channel", "name": "Channel"},
                "messages": [
                    {
                        "id": "unknown-one",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "author": {"id": "author-a", "name": "A"},
                        "content": "[Mira]\nFirst",
                    },
                    {
                        "id": "unknown-two",
                        "timestamp": "2026-01-01T00:01:00Z",
                        "author": {"id": "author-b", "name": "B"},
                        "content": "[ mira ]\nSecond",
                    },
                    {
                        "id": "registered",
                        "timestamp": "2026-01-01T00:02:00Z",
                        "author": {"id": "author-a", "name": "A"},
                        "content": "[Known]\nThird",
                    },
                    {
                        "id": "ambiguous",
                        "timestamp": "2026-01-01T00:03:00Z",
                        "author": {"id": "author-a", "name": "A"},
                        "content": "[Shared]\nFourth",
                    },
                    {
                        "id": "other",
                        "timestamp": "2026-01-01T00:04:00Z",
                        "author": {"id": "author-a", "name": "A"},
                        "content": "[Other]\nFifth",
                    },
                ],
            }),
            encoding="utf-8",
        )
        import_file(self.connection, self.export_path)
        self.known_character_id = create_character(self.connection, "Known")
        self.shared_character_ids = (
            create_character(self.connection, "Shared One"),
            create_character(self.connection, "Shared Two"),
        )
        add_character_alias(self.connection, self.shared_character_ids[0], "Shared")
        add_character_alias(self.connection, self.shared_character_ids[1], "Shared")
        self.messages = self.connection.execute(
            """SELECT m.message_observation_id, m.discord_message_id, m.content_original,
                      c.server_id,
                      author_identity_id, author_discord_user_id_original, webhook_id_original
               FROM messages m LEFT JOIN channels c ON c.channel_id = m.channel_id
               ORDER BY m.message_observation_id"""
        ).fetchall()
        self.observation_ids = {
            row["discord_message_id"]: row["message_observation_id"]
            for row in self.messages
        }

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_persists_provisional_registered_and_ambiguous_evidence_without_curation(self) -> None:
        run_id, result = run_identity_inference(self.connection, self.messages)

        self.assertEqual(len(result.candidates), 2)
        run = self.connection.execute(
            "SELECT status, statistics_json FROM identity_inference_runs WHERE identity_inference_run_id = ?",
            (run_id,),
        ).fetchone()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(
            json.loads(run["statistics_json"])["evidence_kind_counts"],
            {
                "ambiguous_registered_character_match": 1,
                "provisional_label": 3,
                "registered_character_match": 1,
            },
        )
        candidates = self.connection.execute(
            "SELECT * FROM provisional_identity_candidates ORDER BY normalized_label_key"
        ).fetchall()
        self.assertEqual(
            len(list_provisional_identity_candidates(self.connection, resolution_status="unresolved")),
            2,
        )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            list_provisional_identity_candidates(self.connection, limit=0)
        self.assertEqual({row["normalized_label_key"] for row in candidates}, {"mira", "other"})
        self.assertTrue(all(row["resolution_status"] == "unresolved" for row in candidates))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM characters").fetchone()[0],
            3,
        )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM message_attributions").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scenes").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scene_messages").fetchone()[0], 0)

        registered = self.connection.execute(
            """SELECT registered_character_ids_json FROM identity_candidate_evidence
               WHERE message_observation_id = ?""",
            (self.observation_ids["registered"],),
        ).fetchone()
        ambiguous = self.connection.execute(
            """SELECT evidence_kind, registered_character_ids_json FROM identity_candidate_evidence
               WHERE message_observation_id = ?""",
            (self.observation_ids["ambiguous"],),
        ).fetchone()
        self.assertEqual(json.loads(registered["registered_character_ids_json"]), [self.known_character_id])
        self.assertEqual(ambiguous["evidence_kind"], "ambiguous_registered_character_match")
        self.assertEqual(
            json.loads(ambiguous["registered_character_ids_json"]),
            sorted(self.shared_character_ids),
        )

    def test_reviewer_split_and_link_survive_repeated_inference_runs(self) -> None:
        _, result = run_identity_inference(self.connection, self.messages)
        mira_key = next(item.candidate_key for item in result.candidates if item.normalized_label == "mira")
        mira_id = int(self.connection.execute(
            "SELECT candidate_id FROM provisional_identity_candidates WHERE candidate_key = ?",
            (mira_key,),
        ).fetchone()[0])
        second_message_id = self.observation_ids["unknown-two"]

        split_id = review_identity_candidate(
            self.connection,
            mira_id,
            decision="split_evidence",
            actor="reviewer",
            message_observation_ids=[second_message_id],
            note="Separate portrayal",
        )
        self.assertIsNotNone(split_id)
        created_character_id = create_character(self.connection, "Mira")
        review_identity_candidate(
            self.connection,
            int(split_id),
            decision="link_to_character",
            actor="reviewer",
            character_id=created_character_id,
        )
        run_identity_inference(self.connection, self.messages)

        parent_evidence = list_identity_candidate_evidence(self.connection, mira_id)
        split_evidence = list_identity_candidate_evidence(self.connection, int(split_id))
        self.assertEqual([row["message_observation_id"] for row in parent_evidence], [
            self.observation_ids["unknown-one"]
        ])
        self.assertEqual([row["message_observation_id"] for row in split_evidence], [second_message_id])
        split_candidate = self.connection.execute(
            "SELECT resolution_status, linked_character_id FROM provisional_identity_candidates WHERE candidate_id = ?",
            (split_id,),
        ).fetchone()
        self.assertEqual(tuple(split_candidate), ("linked", created_character_id))
        event_rows = self.connection.execute(
            """SELECT decision, linked_character_id, target_candidate_id
               FROM identity_candidate_review_events ORDER BY review_event_id"""
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in event_rows],
            [
                ("split_evidence", None, split_id),
                ("link_to_character", created_character_id, None),
            ],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE identity_candidate_review_events SET actor = 'changed' WHERE review_event_id = 1"
            )

    def test_reassignment_exclusion_and_leave_unresolved_are_audited(self) -> None:
        _, result = run_identity_inference(self.connection, self.messages)
        candidates = {
            item.normalized_label: int(self.connection.execute(
                "SELECT candidate_id FROM provisional_identity_candidates WHERE candidate_key = ?",
                (item.candidate_key,),
            ).fetchone()[0])
            for item in result.candidates
        }
        mira_message_id = self.observation_ids["unknown-one"]
        other_message_id = self.observation_ids["other"]

        review_identity_candidate(
            self.connection,
            candidates["mira"],
            decision="reassign_evidence",
            actor="reviewer",
            target_candidate_id=candidates["other"],
            message_observation_ids=[mira_message_id],
        )
        review_identity_candidate(
            self.connection,
            candidates["other"],
            decision="exclude_evidence",
            actor="reviewer",
            message_observation_ids=[mira_message_id],
        )
        review_identity_candidate(
            self.connection,
            candidates["mira"],
            decision="leave_unresolved",
            actor="reviewer",
            note="Insufficient context",
        )
        run_identity_inference(self.connection, self.messages)

        self.assertEqual(
            [row["message_observation_id"] for row in list_identity_candidate_evidence(
                self.connection, candidates["mira"]
            )],
            [self.observation_ids["unknown-two"]],
        )
        self.assertEqual(
            [row["message_observation_id"] for row in list_identity_candidate_evidence(
                self.connection, candidates["other"]
            )],
            [other_message_id],
        )
        decisions = [
            row[0] for row in self.connection.execute(
                "SELECT decision FROM identity_candidate_review_events ORDER BY review_event_id"
            )
        ]
        self.assertEqual(decisions, ["reassign_evidence", "exclude_evidence", "leave_unresolved"])
        self.assertEqual(
            self.connection.execute(
                "SELECT resolution_status FROM provisional_identity_candidates WHERE candidate_id = ?",
                (candidates["mira"],),
            ).fetchone()[0],
            "unresolved",
        )


if __name__ == "__main__":
    unittest.main()
