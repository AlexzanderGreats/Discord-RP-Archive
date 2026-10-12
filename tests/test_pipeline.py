from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive.attribution import add_character_alias, create_character
from archive.attribution_review import review_attribution_candidate
from archive.boundaries import BoundaryScoringConfig
from archive.pipeline import get_pipeline_report, get_pipeline_status, run_pipeline
from src.build_archive import create_database, import_file


class PipelineTests(unittest.TestCase):
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
                    "content": "[Mira]\nFirst roleplay message",
                },
                {
                    "id": "two",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "author": {"id": "player", "name": "Player"},
                    "content": "[Mira]\nSecond roleplay message",
                },
            ],
        }), encoding="utf-8")
        import_file(self.connection, self.export_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _run_ids(self, report: dict[str, object]) -> dict[str, int | None]:
        stages = report["stages"]
        assert isinstance(stages, dict)
        return {
            stage: details["run_id"]
            for stage, details in stages.items()
        }

    def test_incremental_rerun_reuses_matching_stages_and_full_is_safe_noop(self) -> None:
        first = run_pipeline(self.connection)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(
            {stage["status"] for stage in first["stages"].values()},
            {"completed"},
        )
        first_ids = self._run_ids(first)

        incremental = run_pipeline(self.connection)
        self.assertEqual(incremental["status"], "completed")
        self.assertEqual(incremental["stages"]["signals"]["status"], "completed")
        self.assertTrue(all(
            incremental["stages"][name]["status"] == "reused"
            for name in ("boundaries", "spans", "identities", "confidence")
        ))
        self.assertEqual(self._run_ids(incremental), first_ids)

        full = run_pipeline(self.connection, mode="full")
        self.assertEqual(full["status"], "completed")
        self.assertEqual(full["stages"]["signals"]["status"], "completed")
        self.assertTrue(all(
            full["stages"][name]["status"] == "reused"
            for name in ("boundaries", "spans", "identities", "confidence")
        ))
        self.assertTrue(get_pipeline_status(self.connection)["current_for_scope"])
        self.assertEqual(get_pipeline_report(self.connection, first["run_id"]), first)

        for table in (
            "boundary_detection_runs",
            "segmentation_runs",
            "identity_inference_runs",
            "attribution_confidence_runs",
        ):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)

    def test_config_change_invalidates_only_dependent_stages_incrementally(self) -> None:
        first = run_pipeline(self.connection)
        changed = run_pipeline(
            self.connection,
            boundary_config=BoundaryScoringConfig(timestamp_gap_reference_seconds=7200),
        )

        self.assertEqual(changed["status"], "completed")
        self.assertEqual(changed["stages"]["boundaries"]["status"], "completed")
        self.assertEqual(changed["stages"]["spans"]["status"], "completed")
        self.assertEqual(changed["stages"]["identities"]["status"], "reused")
        self.assertEqual(changed["stages"]["confidence"]["status"], "reused", changed["stages"])
        self.assertNotEqual(
            changed["stages"]["boundaries"]["run_id"],
            first["stages"]["boundaries"]["run_id"],
        )
        self.assertEqual(
            changed["stages"]["identities"]["run_id"],
            first["stages"]["identities"]["run_id"],
        )

    def test_alias_change_invalidates_identity_and_confidence_not_boundary_or_spans(self) -> None:
        first = run_pipeline(self.connection)
        character_id = create_character(self.connection, "Known character")
        add_character_alias(self.connection, character_id, "Mira")
        self.connection.commit()
        self.assertFalse(get_pipeline_status(self.connection)["current_for_scope"])

        changed = run_pipeline(self.connection)

        self.assertEqual(changed["status"], "completed")
        self.assertEqual(changed["stages"]["boundaries"]["status"], "reused")
        self.assertEqual(changed["stages"]["spans"]["status"], "reused")
        self.assertEqual(changed["stages"]["identities"]["status"], "completed")
        self.assertEqual(changed["stages"]["confidence"]["status"], "completed")
        self.assertEqual(
            changed["stages"]["boundaries"]["run_id"],
            first["stages"]["boundaries"]["run_id"],
        )
        self.assertNotEqual(
            changed["stages"]["identities"]["run_id"],
            first["stages"]["identities"]["run_id"],
        )

    def test_input_change_recomputes_stages_for_the_new_accepted_capture(self) -> None:
        first = run_pipeline(self.connection)
        replacement = json.loads(self.export_path.read_text(encoding="utf-8"))
        replacement["messages"].append({
            "id": "three",
            "timestamp": "2026-01-01T00:02:00Z",
            "author": {"id": "player", "name": "Player"},
            "content": "[Mira]\nThird roleplay message",
        })
        self.export_path.write_text(json.dumps(replacement), encoding="utf-8")
        import_file(self.connection, self.export_path)

        changed = run_pipeline(self.connection)

        self.assertEqual(changed["status"], "completed")
        self.assertEqual(changed["input"]["messages_examined"], 3)
        self.assertTrue(all(
            changed["stages"][name]["status"] == "completed"
            for name in ("boundaries", "spans", "identities", "confidence")
        ))
        self.assertNotEqual(
            changed["input"]["input_fingerprint"],
            first["input"]["input_fingerprint"],
        )

    def test_failed_stage_is_manifested_and_incremental_retry_reuses_completed_work(self) -> None:
        with patch(
            "archive.pipeline.run_attribution_confidence",
            side_effect=RuntimeError("injected confidence failure"),
        ):
            failed = run_pipeline(self.connection)

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failed_stage"], "confidence")
        self.assertEqual(failed["stages"]["identities"]["status"], "completed")
        self.assertEqual(self.connection.execute(
            "SELECT status FROM pipeline_runs WHERE pipeline_run_id = ?",
            (failed["run_id"],),
        ).fetchone()[0], "failed")

        recovered = run_pipeline(self.connection)

        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(all(
            recovered["stages"][name]["status"] == "reused"
            for name in ("boundaries", "spans", "identities")
        ))
        self.assertEqual(recovered["stages"]["confidence"]["status"], "completed")

    def test_dry_run_persists_manifest_only_and_rejects_empty_or_out_of_scope_input(self) -> None:
        before = {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "boundary_detection_runs",
                "segmentation_runs",
                "identity_inference_runs",
                "attribution_confidence_runs",
            )
        }
        planned = run_pipeline(self.connection, dry_run=True)
        self.assertEqual(planned["status"], "completed")
        self.assertTrue(all(
            stage["status"] == "planned"
            for stage in planned["stages"].values()
        ))
        self.assertEqual({
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }, before)

        failed_empty = run_pipeline(self.connection, channel_id=999999)
        self.assertEqual(failed_empty["status"], "failed")
        self.assertIn("no accepted message observations", failed_empty["errors"][0])
        failed_missing = run_pipeline(self.connection, message_observation_ids=[999999])
        self.assertEqual(failed_missing["status"], "failed")
        self.assertIn("no accepted message observations", failed_missing["errors"][0])

    def test_rerun_preserves_curated_scenes_and_f2_review_audit(self) -> None:
        first = run_pipeline(self.connection)
        confidence_run_id = first["stages"]["confidence"]["run_id"]
        candidate = self.connection.execute(
            """SELECT attribution_confidence_candidate_id, message_observation_id
               FROM attribution_confidence_candidates
               WHERE attribution_confidence_run_id = ?
                 AND candidate_kind = 'provisional_identity'
               ORDER BY message_observation_id LIMIT 1""",
            (confidence_run_id,),
        ).fetchone()
        self.assertIsNotNone(candidate)
        review_attribution_candidate(
            self.connection,
            confidence_run_id=confidence_run_id,
            confidence_candidate_id=candidate["attribution_confidence_candidate_id"],
            action="leave_unresolved",
            actor="test-reviewer",
            note="Preserve this human decision.",
        )

        message_id = int(candidate["message_observation_id"])
        self.connection.execute(
            "INSERT INTO arcs(arc_name, created_at, updated_at) VALUES ('Curated arc', 't', 't')"
        )
        arc_id = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.connection.execute(
            """INSERT INTO scenes(arc_id, scene_name, created_at, updated_at)
               VALUES (?, 'Curated scene', 't', 't')""",
            (arc_id,),
        )
        scene_id = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.connection.execute(
            """INSERT INTO scene_messages(
                   scene_id, message_observation_id, position, added_at, added_by, notes
               ) VALUES (?, ?, 0, 't', 'reviewer', 'curated')""",
            (scene_id, message_id),
        )
        self.connection.commit()
        before = {
            table: [
                tuple(row)
                for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in (
                "arcs",
                "scenes",
                "scene_messages",
                "attribution_review_decisions",
                "attribution_review_events",
            )
        }

        rerun = run_pipeline(self.connection)
        self.assertEqual(rerun["status"], "completed")
        self.assertEqual(rerun["stages"]["confidence"]["status"], "completed")
        after = {
            table: [
                tuple(row)
                for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in before
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
