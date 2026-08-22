import json
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import ga

from agent_loop import exhaust
from ga import GenericAgentHandler, web_scan


class _Parent:
    task_dir = ""

    def get_ctx_multiplier(self):
        return 1.0


class _Response:
    def __init__(self, payload, ok=True, status_code=200, text=""):
        self.payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handler = GenericAgentHandler(_Parent(), cwd=self.tmp.name)
        self.response = SimpleNamespace(content="", thinking="")

    def test_tavily_request_is_normalized_and_result_does_not_expose_key(self):
        response = _Response({
            "answer": "summary",
            "results": [{"title": "Result", "url": "https://example.test", "content": "text", "score": 0.8}],
        })
        with patch("ga._web_search_config", return_value=("tavily", "secret-key", {"timeout": 8})), \
             patch("ga.requests.post", return_value=response) as post:
            result = web_scan("  climate  ", max_results=99, search_depth="advanced", topic="news", time_range="week")

        self.assertEqual(result, {
            "status": "success", "provider": "tavily", "query": "climate",
            "results": [{"title": "Result", "url": "https://example.test", "content": "text", "score": 0.8}],
            "answer": "summary",
        })
        self.assertNotIn("secret-key", json.dumps(result))
        post.assert_called_once_with(
            "https://api.tavily.com/search",
            json={
                "api_key": "secret-key", "query": "climate", "max_results": 20,
                "search_depth": "advanced", "topic": "news", "include_answer": True,
                "time_range": "week",
            },
            timeout=8,
        )

    def test_exa_uses_published_date_cutoff_and_normalizes_results(self):
        response = _Response({"results": [{
            "title": "Result", "url": "https://example.test", "text": "text", "score": 0.8,
            "publishedDate": "2026-01-01T00:00:00Z", "author": "Author",
        }]})
        with patch("ga._web_search_config", return_value=("exa", "secret-key", {"timeout": "7"})), \
             patch("ga.requests.post", return_value=response) as post:
            result = web_scan("climate", max_results=2, time_range="year")

        self.assertEqual(result["results"], [{
            "title": "Result", "url": "https://example.test", "content": "text", "score": 0.8,
            "published_date": "2026-01-01T00:00:00Z",
        }])
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"], {"x-api-key": "secret-key", "Content-Type": "application/json"})
        self.assertEqual(kwargs["timeout"], 7)
        self.assertEqual(kwargs["json"]["num_results"], 2)
        cutoff = datetime.fromisoformat(kwargs["json"]["startPublishedDate"])
        self.assertLessEqual(cutoff, datetime.now().astimezone())
        self.assertGreater(cutoff, datetime.now().astimezone() - timedelta(days=367))

    def test_missing_or_blank_configuration_fails_without_http_request(self):
        with patch("ga._web_search_config", return_value=("", None, {})), \
             patch("ga.requests.post") as post, patch("ga.requests.get") as get:
            no_config = web_scan("test")
        with patch("ga._web_search_config", return_value=("exa", None, {})), \
             patch("ga.requests.post") as post, patch("ga.requests.get") as get:
            no_key = web_scan("test")

        self.assertEqual(no_config["status"], "error")
        self.assertIn("not configured", no_config["msg"])
        self.assertEqual(no_key["status"], "error")
        self.assertIn("API key is missing", no_key["msg"])
        post.assert_not_called()
        get.assert_not_called()

    def test_handler_routes_web_search_arguments(self):
        result = {"status": "success", "provider": "exa", "query": "test", "results": []}
        with patch("ga.web_scan", return_value=result) as search:
            outcome = exhaust(self.handler.dispatch("web_scan", {
                "query": "test", "max_results": "4", "search_depth": "advanced", "topic": "news", "time_range": "day", "provider": "exa",
            }, self.response))

        self.assertEqual(json.loads(outcome.data), result)
        search.assert_called_once_with(query="test", max_results=4, search_depth="advanced", topic="news", time_range="day", provider="exa")

    def test_provider_configs_are_discovered_by_value_not_variable_name(self):
        keys = {
            "anything": {"provider": "exa", "api_key": "exa-key"},
            "a_different_name": {"provider": "tavily", "api_key": "tavily-key"},
            "not_a_search_config": {"api_key": "ignored"},
        }
        with patch("llmcore._load_mykeys", return_value=keys), \
             patch.dict("os.environ", {}, clear=True):
            configs = ga._web_search_configs()
            provider, api_key, _ = ga._web_search_config()

        self.assertEqual(set(configs), {"tavily", "exa"})
        self.assertEqual((provider, api_key), ("tavily", "tavily-key"))

    def test_explicit_exa_never_calls_tavily(self):
        response = _Response({"results": []})
        with patch("ga._web_search_config", return_value=("exa", "exa-key", {})), \
             patch("ga.requests.post", return_value=response) as post:
            result = web_scan("obscure physics", provider="exa")

        self.assertEqual(result["provider"], "exa")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.exa.ai/search")

    def test_tavily_connection_failure_falls_back_once_to_exa(self):
        exa_response = _Response({"results": []})
        with patch("ga._web_search_config", side_effect=[
            ("tavily", "tavily-key", {}), ("exa", "exa-key", {}),
        ]), patch("ga.requests.post", side_effect=[requests.ConnectionError("offline"), exa_response]) as post:
            result = web_scan("query")

        self.assertEqual(result["provider"], "exa")
        self.assertEqual(result["fallback_from"], "tavily")
        self.assertEqual(post.call_count, 2)

    def test_tavily_http_error_and_empty_result_do_not_fall_back_to_exa(self):
        http_error = _Response({"error": "rate limited"}, ok=False, status_code=429)
        empty_result = _Response({"results": []})
        with patch("ga._web_search_config", return_value=("tavily", "tavily-key", {})), \
             patch("ga.requests.post", side_effect=[http_error, empty_result]) as post:
            rate_limited = web_scan("query")
            empty = web_scan("query")

        self.assertEqual(rate_limited["status"], "error")
        self.assertEqual(empty["provider"], "tavily")
        self.assertEqual(post.call_count, 2)

    def test_browser_scan_remains_available_via_web_execute_js(self):
        browser_result = {"status": "success", "tabs": [], "content": "page"}
        with patch("ga.web_browser_scan", return_value=browser_result) as scan:
            outcome = exhaust(self.handler.dispatch("web_execute_js", {
                "scan": True, "tabs_only": True, "text_only": False, "switch_tab_id": "tab-1",
            }, self.response))

        self.assertEqual(json.loads(outcome.data), {"status": "success", "tabs": [], "scan_content": "page"})
        scan.assert_called_once_with(tabs_only=True, switch_tab_id="tab-1", text_only=False, maxlen=35000)


if __name__ == "__main__":
    unittest.main()
