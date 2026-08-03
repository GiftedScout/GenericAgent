import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_loop import exhaust
from ga import GenericAgentHandler, code_run, get_global_memory, resolve_memory_dir


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

    def test_memory_dir_defaults_to_checkout_memory(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GA_MEMORY_DIR", None)
            self.assertEqual(resolve_memory_dir(root), str(Path(root).resolve() / "memory"))

    def test_memory_dir_uses_main_worktree_for_linked_worktree(self):
        with tempfile.TemporaryDirectory() as top, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GA_MEMORY_DIR", None)
            top = Path(top)
            main = top / "main"
            linked = top / "linked"
            gitdir = main / ".git" / "worktrees" / "linked"
            main.mkdir()
            linked.mkdir()
            gitdir.mkdir(parents=True)
            (linked / ".git").write_text(
                "gitdir: ../main/.git/worktrees/linked\n", encoding="utf-8"
            )
            (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
            self.assertEqual(resolve_memory_dir(linked), str(main / "memory"))

    def test_memory_dir_environment_override_wins(self):
        with tempfile.TemporaryDirectory() as override:
            with patch.dict(os.environ, {"GA_MEMORY_DIR": override}):
                self.assertEqual(resolve_memory_dir("/unused"), str(Path(override).resolve()))

    def test_global_memory_prompt_uses_resolved_accessible_path(self):
        import ga
        prompt = get_global_memory()
        self.assertIn(f"[Memory] ({ga.memory_dir})", prompt)
        self.assertIn(os.path.join(ga.memory_dir, "global_mem.txt"), prompt)
        self.assertNotIn("../memory", prompt)

    def test_code_run_python_imports_from_resolved_memory_dir(self):
        module_dir = Path(self.tmp.name) / "canonical-memory"
        module_dir.mkdir()
        (module_dir / "ga_memory_probe.py").write_text("VALUE = 'shared-memory'\n", encoding="utf-8")
        with patch("ga.memory_dir", str(module_dir)):
            result = exhaust(code_run(
                "import ga_memory_probe; print(ga_memory_probe.VALUE)",
                cwd=self.tmp.name,
                code_cwd=self.tmp.name,
            ))
        self.assertEqual(result["exit_code"], 0, result["stdout"])
        self.assertIn("shared-memory", result["stdout"])

    def test_code_run_schema_distinguishes_python_type_from_shell_command(self):
        expectations = {
            "assets/tools_schema.json": ("type=python", "bash scripts", "python3", "never python"),
            "assets/tools_schema_cn.json": ("type=python", "bash脚本", "python3", "不要调用python"),
        }
        for relative_path, phrases in expectations.items():
            schema = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            code_run = next(
                item["function"] for item in schema
                if item["function"]["name"] == "code_run"
            )
            for phrase in phrases:
                self.assertIn(phrase, code_run["description"], relative_path)

    def test_active_prompts_do_not_navigate_to_retired_plan_sop(self):
        for relative_path in (
            "ga.py",
            "frontends/desktop/static/i18n.js",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("plan_sop", source, relative_path)


if __name__ == "__main__":
    unittest.main()
