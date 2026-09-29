from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive.provenance import current_accepted_source_id
from src.document_parser import create_document_tables, import_document


class DocumentImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source_path = self.root / "scene.txt"
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_document_tables(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_recapture_preserves_typed_documents_chunks_and_entries(self) -> None:
        self.source_path.write_bytes(b"[Winter]\nThe first scene.\n")
        first = import_document(self.connection, self.source_path, input_root=self.root)

        self.source_path.write_bytes(b"[Winter]\nThe revised scene.\n")
        second = import_document(self.connection, self.source_path, input_root=self.root)

        rows = self.connection.execute(
            """SELECT d.document_id, d.source_id, d.import_id, d.content_text,
                      i.accepted_at, s.supersedes_source_id
               FROM documents d
               JOIN imports i ON i.import_id = d.import_id
               JOIN sources s ON s.source_id = d.source_id
               ORDER BY d.import_id"""
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["content_text"] for row in rows], [
            "[Winter]\nThe first scene.\n",
            "[Winter]\nThe revised scene.\n",
        ])
        self.assertEqual(rows[0]["supersedes_source_id"], None)
        self.assertEqual(rows[1]["supersedes_source_id"], rows[0]["source_id"])
        self.assertIsNotNone(rows[0]["accepted_at"])
        self.assertIsNotNone(rows[1]["accepted_at"])
        self.assertEqual(
            current_accepted_source_id(
                self.connection,
                self.connection.execute(
                    "SELECT logical_source_key FROM sources WHERE source_id = ?",
                    (second.source_id,),
                ).fetchone()[0],
            ),
            second.source_id,
        )
        self.assertGreater(
            self.connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0], 0
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM document_entries").fetchone()[0], 2
        )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(first.import_status, "succeeded")
        self.assertEqual(second.import_status, "succeeded")

    def test_extraction_failure_retains_capture_and_failed_import(self) -> None:
        self.source_path.write_bytes(b"source bytes")
        with patch("src.document_parser.parse_supported_file", side_effect=RuntimeError("parse failed")):
            result = import_document(self.connection, self.source_path, input_root=self.root)

        self.assertEqual(result.import_status, "failed")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        self.assertEqual(
            tuple(self.connection.execute("SELECT status, accepted_at FROM imports").fetchone()),
            ("failed", None),
        )


if __name__ == "__main__":
    unittest.main()