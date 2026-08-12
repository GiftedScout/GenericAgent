import hashlib
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import llmcore
from llmcore import LLMSession, MixinSession, _TASK_ANCHOR_REFERENCE


class _Response:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.text = ""
        self.headers = {}

    def json(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _chat_response():
    return _Response({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 100, "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    })


def _responses_response():
    return _Response({
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "ok"},
        ]}],
        "usage": {
            "input_tokens": 100, "output_tokens": 3,
            "input_tokens_details": {"cached_tokens": 80},
        },
    })


class OpenAIPromptCacheTests(unittest.TestCase):
    def _session(self, mode="chat_completions", **cfg):
        return LLMSession({
            "apikey": "test", "apibase": "https://example.invalid",
            "model": "gpt-5.6-test", "stream": False, "api_mode": mode,
            **cfg,
        })

    def _capture_calls(self, response):
        calls = []

        @contextmanager
        def post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            yield response()

        return calls, post

    def test_chat_explicit_cache_is_sent_at_stable_task_anchor(self):
        session = self._session()
        anchor = "Retain this task instruction exactly."
        session.begin_task(anchor)
        calls, post = self._capture_calls(_chat_response)

        with patch.object(llmcore.requests, "post", post):
            self.assertEqual(list(session.ask(anchor)), ["ok"])
            self.assertEqual(list(session.ask("continue with step two")), ["ok"])

        self.assertEqual(len(calls), 2)
        key = hashlib.sha256(anchor.encode()).hexdigest()
        for call in calls:
            payload = call["json"]
            self.assertEqual(payload["prompt_cache_key"], key)
            self.assertEqual(payload["prompt_cache_options"], {"mode": "explicit"})
            self.assertEqual(payload["messages"][0]["content"][0], {
                "type": "text", "text": anchor,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            })
        self.assertEqual(llmcore.STATS["cached"], 80)
        self.assertEqual(llmcore.STATS["inp"], 100)

    def test_responses_explicit_cache_is_sent_and_anchor_stays_out_of_history(self):
        session = self._session("responses")
        anchor = "Stable external task for the responses endpoint."
        session.begin_task(anchor)
        calls, post = self._capture_calls(_responses_response)

        with patch.object(llmcore.requests, "post", post):
            self.assertEqual(list(session.ask(anchor)), ["ok"])

        payload = calls[0]["json"]
        self.assertEqual(payload["prompt_cache_key"], hashlib.sha256(anchor.encode()).hexdigest())
        self.assertEqual(payload["prompt_cache_options"], {"mode": "explicit"})
        self.assertEqual(payload["input"][0]["content"][0], {
            "type": "input_text", "text": anchor,
            "prompt_cache_breakpoint": {"mode": "explicit"},
        })
        self.assertEqual(session.history[0]["content"][0]["text"], _TASK_ANCHOR_REFERENCE)
        self.assertNotIn(anchor, str(session.history))

    def test_new_task_gets_a_new_key_and_opt_out_sends_no_protocol_fields(self):
        session = self._session()
        calls, post = self._capture_calls(_chat_response)
        with patch.object(llmcore.requests, "post", post):
            session.begin_task("task alpha")
            list(session.ask("task alpha"))
            session.begin_task("task beta")
            list(session.ask("task beta"))
        self.assertNotEqual(calls[0]["json"]["prompt_cache_key"], calls[1]["json"]["prompt_cache_key"])

        opt_out = self._session(openai_prompt_cache=False)
        calls, post = self._capture_calls(_chat_response)
        with patch.object(llmcore.requests, "post", post):
            opt_out.begin_task("must remain compatible")
            list(opt_out.ask("must remain compatible"))
        self.assertNotIn("prompt_cache_key", calls[0]["json"])
        self.assertNotIn("prompt_cache_options", calls[0]["json"])
        self.assertNotIn("prompt_cache_breakpoint", str(calls[0]["json"]))

    def test_mixin_propagates_anchor_to_the_child_that_sends(self):
        primary = self._session()
        secondary = self._session()
        # Runtime registrations wrap each backend; MixinSession selects their
        # numeric positions from `llm_nos`.
        registrations = [
            type("Registration", (), {"backend": primary})(),
            type("Registration", (), {"backend": secondary})(),
        ]
        mixin = MixinSession(registrations, {"llm_nos": [0, 1]})
        anchor = "Stable routed task"
        mixin.begin_task(anchor)
        self.assertTrue(all(s.task_anchor == anchor for s in mixin._sessions))
        self.assertTrue(all(s._task_anchor_pending for s in mixin._sessions))


if __name__ == "__main__":
    unittest.main()
