from __future__ import annotations

import unittest
from types import SimpleNamespace

from sxsvn_intel.errors import (
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRetriesExhausted,
    LLMTimeoutError,
)
from sxsvn_intel.llm.client import LLMClient
from sxsvn_intel.llm.schemas import FactsResponse
from tests.helpers import make_settings


def sdk_response(content, model="test-model"):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(model_dump=lambda **_: {"prompt_tokens": 10, "completion_tokens": 5}),
    )


class ProviderError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class APITimeoutError(Exception):
    pass


class FakeOpenAIClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create_completion)
        )

    def create_completion(self, **request):
        self.calls.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class LLMClientTests(unittest.TestCase):
    def client(self, responses, **overrides):
        settings = make_settings(llm_max_retries=2, llm_backoff_base=0.0, **overrides)
        sleeps = []
        sdk = FakeOpenAIClient(responses)
        client = LLMClient(settings, client=sdk, sleep=sleeps.append)
        return client, sdk, sleeps

    def test_timeout_is_retried_and_then_succeeds(self):
        client, sdk, sleeps = self.client(
            [APITimeoutError("timeout"), sdk_response('{"ok": true}')]
        )
        response = client.complete_json(
            system_prompt="s", user_prompt="u", prompt_version="news-facts.v1"
        )
        self.assertEqual(response.content, {"ok": True})
        self.assertEqual(response.attempts, 2)
        self.assertEqual(len(sdk.calls), 2)
        self.assertEqual(len(sleeps), 1)

    def test_provider_500_exhausts_bounded_retries(self):
        client, sdk, _ = self.client(
            [ProviderError(500, "server error") for _ in range(3)]
        )
        with self.assertRaises(LLMRetriesExhausted) as ctx:
            client.complete_json(system_prompt="s", user_prompt="u", prompt_version="v")
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(len(sdk.calls), 3)

    def test_non_retryable_400_raises_immediately(self):
        client, sdk, _ = self.client([ProviderError(400, "bad request")])
        with self.assertRaises(LLMProviderError) as ctx:
            client.complete_json(system_prompt="s", user_prompt="u", prompt_version="v")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(sdk.calls), 1)

    def test_unsupported_response_format_is_disabled_and_retried(self):
        client, sdk, _ = self.client(
            [
                ProviderError(400, "response_format json_object is unsupported"),
                sdk_response('{"ok": true}'),
            ]
        )
        client.complete_json(system_prompt="s", user_prompt="u", prompt_version="v")
        self.assertIn("response_format", sdk.calls[0])
        self.assertNotIn("response_format", sdk.calls[1])

    def test_invalid_json_is_retried(self):
        client, sdk, _ = self.client(
            [sdk_response("not json at all"), sdk_response('{"ok": 1}')]
        )
        response = client.complete_json(system_prompt="s", user_prompt="u", prompt_version="v")
        self.assertEqual(response.content, {"ok": 1})
        self.assertEqual(len(sdk.calls), 2)

    def test_invalid_json_exhaustion(self):
        client, _, _ = self.client([sdk_response("nope") for _ in range(3)])
        with self.assertRaises(LLMRetriesExhausted) as ctx:
            client.complete_json(system_prompt="s", user_prompt="u", prompt_version="v")
        self.assertIsInstance(ctx.exception.last_error, LLMInvalidResponseError)

    def test_pydantic_response_model_checks_structure(self):
        client, _, _ = self.client([sdk_response('{"facts": [], "general_relevance": "KEEP"}')])
        response = client.complete_json(
            system_prompt="s",
            user_prompt="u",
            prompt_version="facts.v1",
            response_model=FactsResponse,
        )
        self.assertEqual(response.content["facts"], [])
        self.assertEqual(response.content["general_relevance"], "KEEP")

    def test_pydantic_validation_errors_are_retried(self):
        client, sdk, _ = self.client(
            [
                sdk_response('{"facts": "not-a-list", "general_relevance": "KEEP"}'),
                sdk_response('{"facts": [], "general_relevance": "KEEP"}'),
            ]
        )
        response = client.complete_json(
            system_prompt="s",
            user_prompt="u",
            prompt_version="facts.v1",
            response_model=FactsResponse,
        )
        self.assertEqual(response.content["facts"], [])
        self.assertEqual(len(sdk.calls), 2)

    def test_api_key_is_stripped_from_errors_and_settings_repr(self):
        secret = "super-secret-key"
        settings = make_settings(llm_api_key=secret, llm_max_retries=1, llm_backoff_base=0.0)
        sdk = FakeOpenAIClient([ProviderError(401, f"invalid {secret}")])
        client = LLMClient(settings, client=sdk, sleep=lambda _: None)
        with self.assertRaises(LLMProviderError) as ctx:
            client.complete_json(system_prompt="s", user_prompt="u", prompt_version="v")
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(secret, repr(settings))
        self.assertEqual(len(sdk.calls), 1)

    def test_markdown_fenced_json_is_accepted(self):
        client, _, _ = self.client([sdk_response('```json\n{"facts": []}\n```')])
        response = client.complete_json(system_prompt="s", user_prompt="u", prompt_version="v")
        self.assertEqual(response.content, {"facts": []})


if __name__ == "__main__":
    unittest.main()
