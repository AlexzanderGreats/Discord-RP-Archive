from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.attribution import (
    add_character_alias,
    create_character,
    create_player,
    link_identity_to_player,
    link_player_to_character,
    list_pending_attributions,
    propose_attribution,
    propose_message_attributions,
    review_attribution,
)
from src.build_archive import create_database, import_file


class AttributionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.export_path = Path(self.temp_dir.name) / "channel.json"
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_database(self.connection)
        self.export_path.write_text(
            json.dumps(
                {
                    "guild": {"id": "server", "name": "Server"},
                    "channel": {"id": "channel", "name": "Channel"},
                    "messages": [
                        {
                            "id": "message-marker",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "author": {"id": "discord-user", "name": "account"},
                            "content": "[Winter]\nI enter the room.",
                        },
                        {
                            "id": "message-plain",
                            "timestamp": "2026-01-01T00:01:00Z",
                            "author": {"id": "discord-user", "name": "account"},
                            "content": "A plain message.",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        import_file(self.connection, self.export_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _message_id(self, discord_message_id: str) -> int:
        row = self.connection.execute(
            "SELECT message_observation_id FROM messages WHERE discord_message_id = ?",
            (discord_message_id,),
        ).fetchone()
        return int(row[0])

    def _identity_id(self) -> int:
        return int(self.connection.execute("SELECT identity_id FROM discord_identities").fetchone()[0])

    def test_exact_marker_and_identity_link_create_pending_reviewable_claims(self) -> None:
        player_id = create_player(self.connection, "Player One")
        character_id = create_character(self.connection, "Winter")
        link_identity_to_player(self.connection, self._identity_id(), player_id)
        link_player_to_character(self.connection, player_id, character_id)

        marker_message_id = self._message_id("message-marker")
        plain_message_id = self._message_id("message-plain")
        first_ids = propose_message_attributions(self.connection)
        self.assertEqual(len(first_ids), 2)
        self.assertEqual(propose_message_attributions(self.connection), first_ids)
        self.assertEqual(len(list_pending_attributions(self.connection)), 2)

        marker_claim = self.connection.execute(
            "SELECT * FROM message_attributions WHERE message_observation_id = ?",
            (marker_message_id,),
        ).fetchone()
        self.assertEqual(marker_claim["method"], "explicit_marker")
        self.assertEqual(marker_claim["status"], "pending")
        self.assertEqual(marker_claim["character_id"], character_id)
        self.assertEqual(marker_claim["player_id"], player_id)
        self.assertEqual(json.loads(marker_claim["evidence_json"])["rule"], "leading_speaker_marker_exact_alias_v1")

        identity_claim = self.connection.execute(
            "SELECT * FROM message_attributions WHERE message_observation_id = ?",
            (plain_message_id,),
        ).fetchone()
        self.assertEqual(identity_claim["method"], "identity_link")
        self.assertEqual(identity_claim["player_id"], player_id)
        self.assertIsNone(identity_claim["character_id"])

        review_attribution(
            self.connection,
            int(marker_claim["attribution_id"]),
            decision="accepted",
            actor="reviewer",
            note="Marker matches the registered character alias.",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status, reviewed_by, review_note FROM message_attributions WHERE attribution_id = ?",
                (marker_claim["attribution_id"],),
            ).fetchone()[:],
            ("accepted", "reviewer", "Marker matches the registered character alias."),
        )
        events = self.connection.execute(
            "SELECT event_type, previous_status, new_status, actor FROM attribution_events WHERE attribution_id = ? ORDER BY event_id",
            (marker_claim["attribution_id"],),
        ).fetchall()
        self.assertEqual([tuple(row) for row in events], [
            ("proposed", None, "pending", "attribution_rules"),
            ("accepted", "pending", "accepted", "reviewer"),
        ])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE attribution_events SET actor = 'changed' WHERE event_id = 1")

    def test_ambiguous_aliases_remain_separate_pending_claims(self) -> None:
        character_a = create_character(self.connection, "Winter A")
        character_b = create_character(self.connection, "Winter B")
        add_character_alias(self.connection, character_a, "Winter")
        add_character_alias(self.connection, character_b, "Winter")

        message_id = self._message_id("message-marker")
        proposal_ids = propose_message_attributions(self.connection, message_id)
        claims = self.connection.execute(
            "SELECT character_id, status FROM message_attributions WHERE message_observation_id = ? ORDER BY character_id",
            (message_id,),
        ).fetchall()
        self.assertEqual(proposal_ids, [row[0] for row in self.connection.execute(
            "SELECT attribution_id FROM message_attributions WHERE message_observation_id = ? ORDER BY attribution_id",
            (message_id,),
        )])
        self.assertEqual({row["character_id"] for row in claims}, {character_a, character_b})
        self.assertEqual({row["status"] for row in claims}, {"pending"})

    def test_model_claims_are_pending_and_competing_acceptance_is_audited(self) -> None:
        player_id = create_player(self.connection, "Player One")
        message_id = self._message_id("message-plain")
        model_claim_id = propose_attribution(
            self.connection,
            message_id,
            player_id=player_id,
            method="model",
            confidence=0.72,
            evidence={"model": "test", "reason": "sample"},
        )
        manual_claim_id = propose_attribution(
            self.connection,
            message_id,
            player_id=player_id,
            method="manual",
            evidence={"reason": "reviewed source"},
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM message_attributions WHERE attribution_id = ?", (model_claim_id,)
            ).fetchone()[0],
            "pending",
        )

        review_attribution(self.connection, model_claim_id, decision="accepted", actor="reviewer")
        review_attribution(self.connection, manual_claim_id, decision="accepted", actor="reviewer")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM message_attributions WHERE attribution_id = ?", (model_claim_id,)
            ).fetchone()[0],
            "superseded",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM message_attributions WHERE attribution_id = ?", (manual_claim_id,)
            ).fetchone()[0],
            "accepted",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT event_type FROM attribution_events WHERE attribution_id = ? ORDER BY event_id",
                (model_claim_id,),
            ).fetchall()[-1][0],
            "superseded",
        )


if __name__ == "__main__":
    unittest.main()