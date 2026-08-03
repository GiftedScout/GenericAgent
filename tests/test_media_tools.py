import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_loop import exhaust
from ga import GenericAgentHandler


ROOT = Path(__file__).resolve().parents[1]


class _Parent:
    def get_ctx_multiplier(self):
        return 1.0


class MediaToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema = json.loads((ROOT / "assets" / "tools_schema.json").read_text(encoding="utf-8"))
        cls.tools = {item["function"]["name"]: item["function"] for item in schema}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handler = GenericAgentHandler(_Parent(), cwd=self.tmp.name)
        self.response = SimpleNamespace(content="", thinking="")

    def test_media_tools_are_exposed_with_required_inputs(self):
        self.assertEqual(self.tools["ocr"]["parameters"]["required"], ["image_path"])
        self.assertEqual(self.tools["generate_image"]["parameters"]["required"], ["prompt"])
        self.assertTrue(hasattr(self.handler, "do_ocr"))
        self.assertTrue(hasattr(self.handler, "do_generate_image"))

    def test_ocr_dispatch_resolves_relative_path_and_forwards_options(self):
        expected_path = str(Path(self.tmp.name, "sample.png").resolve())
        with patch("media_api.ocr", return_value="recognized") as mocked:
            outcome = exhaust(self.handler.dispatch(
                "ocr",
                {"image_path": "sample.png", "prompt": "tables only", "timeout": 7},
                self.response,
            ))
        self.assertEqual(outcome.data, "recognized")
        mocked.assert_called_once_with(expected_path, prompt="tables only", timeout=7)

    def test_generate_image_dispatch_uses_agent_working_directory(self):
        target = str(Path(self.tmp.name, "image", "generated.png"))
        with patch("media_api.generate_image", return_value=target) as mocked:
            outcome = exhaust(self.handler.dispatch(
                "generate_image",
                {"prompt": "a diagram", "size": "2K", "quality": "high", "timeout": 9},
                self.response,
            ))
        self.assertEqual(outcome.data, target)
        mocked.assert_called_once_with(
            "a diagram", size="2K", quality="high", timeout=9, output_dir=self.tmp.name
        )

    def test_required_inputs_fail_without_calling_media_api(self):
        with patch("media_api.ocr") as ocr_mock:
            ocr_outcome = exhaust(self.handler.dispatch("ocr", {}, self.response))
        with patch("media_api.generate_image") as image_mock:
            image_outcome = exhaust(self.handler.dispatch("generate_image", {}, self.response))
        self.assertIn("image_path is required", ocr_outcome.data)
        self.assertIn("prompt is required", image_outcome.data)
        ocr_mock.assert_not_called()
        image_mock.assert_not_called()

    def test_active_prompts_do_not_navigate_to_retired_plan_sop(self):
        for relative_path in (
            "ga.py",
            "frontends/desktop/static/i18n.js",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("plan_sop", source, relative_path)


if __name__ == "__main__":
    unittest.main()
