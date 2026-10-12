from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.attribution import create_character, create_player, propose_attribution, review_attribution
from archive.scenes import (
    add_message_to_scene,
    add_scene_participant,
    create_arc,
    create_scene,
    list_scene_messages,
    remove_message_from_scene,
)
from src.build_archive import create_database, import_file


class SceneCurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_database(self.connection)
        self.message_ids: dict[str, int] = {}
        for channel_id, channel_name, message_id, minute in (
            ("channel-a", "Channel A", "message-a", 0),
            ("channel-b", "Channel B", "message-b", 1),
            ("channel-a", "Channel A", "message-c", 2),
        ):
            export_path = Path(self.temp_dir.name) / f"{message_id}.json"
            export_path.write_text(
                json.dumps(
                    {
                        "guild": {"id": "server", "name": "Server"},
                        "channel": {"id": channel_id, "name": channel_name},
                        "messages": [
                            {
                                "id": message_id,
                                "timestamp": f"2026-01-01T00:{minute:02d}:00Z",
                                "author": {"id": "discord-user", "name": "account"},
                                "content": f"Message {message_id}",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            import_file(self.connection, export_path)
            self.message_ids[message_id] = int(
                self.connection.execute(
                    "SELECT message_observation_id FROM messages WHERE discord_message_id = ?",
                    (message_id,),
                ).fetchone()[0]
            )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_membership_is_explicit_ordered_and_reports_attribution_and_channels(self) -> None:
        arc_id = create_arc(self.connection, "The Arrival")
        scene_id = create_scene(
            self.connection,
            "The meeting",
            arc_id=arc_id,
            fictional_time="Year 4, evening",
        )
        character_id = create_character(self.connection, "Winter")
        player_id = create_player(self.connection, "Player One")
        attribution_id = propose_attribution(
            self.connection,
            self.message_ids["message-a"],
            character_id=character_id,
            player_id=player_id,
        )
        review_attribution(self.connection, attribution_id, decision="accepted", actor="reviewer")

        first_id = add_message_to_scene(
            self.connection,
            scene_id,
            self.message_ids["message-a"],
            added_by="curator",
        )
        second_id = add_message_to_scene(self.connection, scene_id, self.message_ids["message-b"])
        self.assertEqual(
            add_message_to_scene(self.connection, scene_id, self.message_ids["message-a"]),
            first_id,
        )

        messages = list_scene_messages(self.connection, scene_id)
        self.assertEqual([row["discord_message_id"] for row in messages], ["message-a", "message-b"])
        self.assertEqual([row["position"] for row in messages], [0, 1])
        self.assertEqual(messages[0]["author_discord_user_id_original"], "discord-user")
        self.assertEqual(messages[0]["character_name"], "Winter")
        self.assertEqual(messages[0]["player_name"], "Player One")
        self.assertEqual(messages[0]["channel_name"], "Channel A")
        self.assertEqual(messages[1]["channel_name"], "Channel B")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM scene_channels WHERE scene_id = ?", (scene_id,)
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT fictional_time, arc_id FROM scenes WHERE scene_id = ?", (scene_id,)
            ).fetchone()[:],
            ("Year 4, evening", arc_id),
        )

        participant_id = add_scene_participant(
            self.connection,
            scene_id,
            character_id=character_id,
            player_id=player_id,
            added_by="curator",
        )
        self.assertEqual(
            add_scene_participant(
                self.connection,
                scene_id,
                character_id=character_id,
                player_id=player_id,
            ),
            participant_id,
        )
        self.assertTrue(remove_message_from_scene(self.connection, scene_id, self.message_ids["message-b"]))
        self.assertFalse(remove_message_from_scene(self.connection, scene_id, self.message_ids["message-b"]))
        self.assertEqual([row["discord_message_id"] for row in list_scene_messages(self.connection, scene_id)], ["message-a"])
        self.assertEqual(second_id, 2)


if __name__ == "__main__":
    unittest.main()