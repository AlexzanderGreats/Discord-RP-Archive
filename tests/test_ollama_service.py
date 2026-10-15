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
            answer, details = OllamaClient().generate_with_details(
                "test-model", "What did Winter find?", evidence
            )

        request = open_url.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(answer, "Winter found it [E1].")
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["num_predict"], 384)
        self.assertIn('"reference": "E1"', payload["messages"][1]["content"])
        self.assertIn('"relation": "query match"', payload["messages"][1]["content"])
        self.assertIn("capture.json", payload["messages"][1]["content"])
        self.assertIn("cite each factual claim", payload["messages"][0]["content"].casefold())
        self.assertIn("concise paragraph, usually 2-5 sentences", payload["messages"][0]["content"])
        self.assertIn("Do not interpret proximity as chronology or event completion", payload["messages"][0]["content"])
        self.assertIn("not necessarily after the event described", payload["messages"][0]["content"])
        self.assertIn("do not infer change from a single scene", payload["messages"][0]["content"])
        self.assertIn("multiple temporally distinct states", payload["messages"][0]["content"])
        self.assertIn("Do not combine separate encounters", payload["messages"][0]["content"])
        self.assertEqual(details.user_context, payload["messages"][1]["content"])
        self.assertEqual(details.system_instructions, payload["messages"][0]["content"])
        self.assertEqual(details.model, payload["model"])
        self.assertEqual(details.settings, {
            "temperature": payload["options"]["temperature"],
            "thinking": "disabled (think=false)",
            "context": "model default (num_ctx not sent)",
            "max tokens": 384,
        })

    def test_answer_does_not_call_model_without_retrieved_evidence(self) -> None:
        search = MagicMock()
        search.search_relevant.return_value = []
        ollama = MagicMock()
        service = ArchiveAnswerService(search, ollama)

        result = service.answer("What happened?", "test-model")

        self.assertEqual(result.evidence, [])
        self.assertIn("no model answer was generated", result.answer)
        ollama.generate.assert_not_called()

    def test_output_token_budget_follows_question_type(self) -> None:
        questions_and_budgets = [
            ("What color was the compass?", 384),
            ("Explain what happened at the meeting.", 768),
            ("How did their relationship evolve across several events?", 1024),
            ("Compare their relationship across scenes.", 1024),
            ("Give an overview of all interactions across the archive.", 1536),
            ("What are all arguments about Chaos?", 1536),
            ("What were all arguments on 9/18/2025 from 1:31 AM to 3:02 PM?", 1024),
            ("Who played Winter?", 512),
        ]
        client = OllamaClient()

        with patch.object(
            client, "_request", return_value={"message": {"content": "Answer."}}
        ) as request:
            for question, expected_budget in questions_and_budgets:
                _answer, details = client.generate_with_details("test-model", question, [])

                request_body = request.call_args.args[2]
                self.assertEqual(request_body["options"]["num_predict"], expected_budget)
                self.assertEqual(details.settings["max tokens"], expected_budget)

    def test_gpt_oss_uses_explicit_context_and_evidence_is_capped(self) -> None:
        evidence = [
            {
                "message_observation_id": index,
                "timestamp_utc": f"2026-01-01T00:{index:02}:00Z",
                "channel_name": "Channel",
                "context_relation": "event match",
                "context_group": f"Channel / 2026-01-{index:02}",
                "content_original": "Chaos argument " + ("evidence " * 150),
                "relevance_score": 1,
            }
            for index in range(1, 40)
        ]

        client = OllamaClient()
        with patch.object(
            client,
            "_request",
            return_value={"message": {"content": "There were multiple arguments [E1]."}},
        ) as request:
            _answer, details = client.generate_with_details(
                "gpt-oss:20b",
                "What were all arguments about Chaos on 9/18/2025 from 1:31 AM to 3:02 PM?",
                evidence,
            )

        payload = request.call_args.args[2]
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(payload["options"]["num_predict"], 3072)
        self.assertEqual(details.settings["context"], "8192 tokens (num_ctx)")
        self.assertIn("group distinct positions by the provided channel/day labels", details.system_instructions)
        evidence_json = details.user_context.split("Retrieved archive evidence (JSON): ", 1)[1]
        serialized_evidence = json.loads(evidence_json)
        self.assertLessEqual(len(json.dumps(serialized_evidence, ensure_ascii=False)), 12000)
        self.assertTrue(all(item["group"].startswith("Channel / ") for item in serialized_evidence))

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