import os
import unittest
from unittest.mock import patch

from frontends import slash_cmds


class UpdatePromptTests(unittest.TestCase):
    def build(self, lang, note=""):
        with patch.dict(os.environ, {"GA_LANG": lang}, clear=False):
            return slash_cmds.build_update_prompt(note)

    def test_english_workflow_is_transactional_and_two_remote(self):
        prompt = self.build("en", "preserve my local command")
        for phrase in (
            "memory/git_sync_sop.md",
            "`origin/main`",
            "`myfork/main`",
            "backup branch",
            "binary patch",
            "three-way",
            "blindly choose ours/theirs",
            "tracked-Python `py_compile`",
            "--force-with-lease=<expected-tip>",
            "Never push official `origin`",
            "update ledger",
            "preserve my local command",
        ):
            self.assertIn(phrase, prompt)
        self.assertIn("use bare `git pull`", prompt)

    def test_chinese_workflow_requires_semantic_resolution_and_approval(self):
        prompt = self.build("zh", "保留本地快捷键")
        for phrase in (
            "backup 分支",
            "binary patch",
            "做语义合并",
            "盲选 ours/theirs",
            "向用户请示",
            "绝不 push 官方 `origin`",
            "最终更新账单",
            "保留本地快捷键",
        ):
            self.assertIn(phrase, prompt)

    def test_dispatch_uses_update_builder_and_unknown_is_ignored(self):
        with patch.dict(os.environ, {"GA_LANG": "en"}, clear=False):
            prompt = slash_cmds.prompt_for("/update", "audit note")
        self.assertIn("audit note", prompt)
        self.assertIn("official source", prompt)
        self.assertIsNone(slash_cmds.prompt_for("/not-a-command", ""))

    def test_palette_describes_llm_merge_not_bare_pull(self):
        entry = next(item for item in slash_cmds.PALETTE_ENTRIES if item[0] == "/update")
        self.assertIn("LLM", entry[2])
        self.assertIn("myfork", entry[2])
        self.assertNotIn("git pull", entry[2])


if __name__ == "__main__":
    unittest.main()
