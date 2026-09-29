from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from archive.ollama_service import ArchiveAnswerService, OllamaClient


class OllamaServiceTests(unittest.TestCase):
    def test_generate_sends_only_retrieved_evidence_and_returns_model_text(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"message": {"content": "Winter found it [E1]."}}
        ).encode("utf-8")
        evidence = [{
            "message_observation_id": 42,
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "channel_name": "Channel",
            "author_discord_name_original": "Author",
            "source_path": "capture.json",
            "content_original": "Winter found a silver compass.",
        }]

        with patch("archive.ollama_service.urlopen", return_value=response) as open_url:
            answer = OllamaClient().generate("test-model", "What did Winter find?", evidence)

        request = open_url.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(answer, "Winter found it [E1].")
        self.assertIn('"reference": "E1"', payload["messages"][1]["content"])
        self.assertIn("capture.json", payload["messages"][1]["content"])
        self.assertIn("cite each factual claim", payload["messages"][0]["content"].casefold())

    def test_answer_does_not_call_model_without_retrieved_evidence(self) -> None:
        search = MagicMock()
        search.search_relevant.return_value = []
        ollama = MagicMock()
        service = ArchiveAnswerService(search, ollama)

        result = service.answer("What happened?", "test-model")

        self.assertEqual(result.evidence, [])
        self.assertIn("no model answer was generated", result.answer)
        ollama.generate.assert_not_called()

    def test_list_models_reads_local_tags(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"models": [{"name": "model-a:latest"}, {"name": "model-b"}]}
        ).encode("utf-8")

        with patch("archive.ollama_service.urlopen", return_value=response):
            models = OllamaClient().list_models()

        self.assertEqual(models, ["model-a:latest", "model-b"])


if __name__ == "__main__":
    unittest.main()