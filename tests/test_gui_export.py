from __future__ import annotations

import unittest

from archive.gui import ArchiveApp
from archive.ollama_service import OllamaRequestDetails


class GuiExportTests(unittest.TestCase):
    def test_test_export_includes_model_question_answer_and_evidence(self) -> None:
        report = ArchiveApp._format_test_export(
            "What did Winter find?",
            "Winter found a compass.",
            [{
                "timestamp_utc": "2026-01-01",
                "source_path": "capture.json",
                "relevance_score": 3,
            }],
            OllamaRequestDetails(
                model="test-model",
                user_context='Question: What did Winter find?\n\nRetrieved archive evidence (JSON): [{"reference": "E1"}]',
                system_instructions="Use only the supplied evidence.",
                settings={
                    "temperature": 0.2,
                    "thinking": "disabled (think=false)",
                    "context": "default (Ollama; num_ctx not sent)",
                    "max tokens": 384,
                },
            ),
        )

        self.assertIn("USER QUESTION", report)
        self.assertIn("What did Winter find?", report)
        self.assertIn("Result #1", report)
        self.assertIn("Source: capture.json", report)
        self.assertIn("Date: 2026-01-01", report)
        self.assertIn("Score: 3", report)
        self.assertIn('Retrieved archive evidence (JSON): [{"reference": "E1"}]', report)
        self.assertIn("SYSTEM INSTRUCTIONS", report)
        self.assertIn("MODEL\n----------------------------\ntest-model", report)
        self.assertIn("temperature: 0.2", report)
        self.assertIn("thinking: disabled (think=false)", report)
        self.assertIn("num_ctx not sent", report)
        self.assertIn("max tokens: 384", report)
        self.assertIn("Winter found a compass.", report)


if __name__ == "__main__":
    unittest.main()