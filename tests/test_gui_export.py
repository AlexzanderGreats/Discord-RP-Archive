from __future__ import annotations

import unittest

from archive.gui import ArchiveApp


class GuiExportTests(unittest.TestCase):
    def test_test_export_includes_model_question_answer_and_evidence(self) -> None:
        report = ArchiveApp._format_test_export(
            "What did Winter find?",
            "test-model",
            "Winter found a compass.",
            "[E1] 2026-01-01 | Channel | Author\nSource: capture.json\nA silver compass.",
        )

        self.assertIn("Model: test-model", report)
        self.assertIn("What did Winter find?", report)
        self.assertIn("Winter found a compass.", report)
        self.assertIn("[E1] 2026-01-01 | Channel | Author", report)
        self.assertIn("Source: capture.json", report)


if __name__ == "__main__":
    unittest.main()