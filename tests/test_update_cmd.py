import queue
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frontends import update_cmd


class _Runner:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, argv, cwd):
        command = tuple(argv)
        self.calls.append(command)
        key = command[3:] if command[:1] == ("git",) else command
        value = self.replies.get(key, (0, ""))
        if isinstance(value, list):
            value = value.pop(0)
        code, output = value
        return subprocess.CompletedProcess(command, code, output)

    def used(self, *tail):
        return any(call[3:] == tail for call in self.calls if call[:1] == ("git",))


class UpdateCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.top = str(self.root.resolve())

    def replies(self, extra=None):
        base = {
            ("rev-parse", "--show-toplevel"): (0, self.top),
            ("branch", "--show-current"): (0, "main"),
            ("status", "--porcelain"): (0, ""),
            ("rev-parse", "-q", "--verify", "MERGE_HEAD"): (1, ""),
            ("rev-parse", "HEAD"): [(0, "oldhead"), (0, "newhead")],
            ("fetch", "--prune", "origin"): (0, ""),
            ("fetch", "--prune", "myfork"): (0, ""),
            ("rev-parse", "origin/main"): (0, "upstream"),
            ("rev-parse", "myfork/main"): [(0, "mirrorhead"), (0, "newhead")],
            ("log", "--format=%s", "-6", "oldhead..origin/main"): (0, "useful change"),
            ("merge-base", "--is-ancestor", "HEAD", "origin/main"): (0, ""),
            ("merge-base", "--is-ancestor", "origin/main", "HEAD"): (1, ""),
            ("merge", "--ff-only", "origin/main"): (0, ""),
            ("branch", "backup/update-auto-20260101-000000", "HEAD"): (0, ""),
            ("stash", "push", "-m", "/update auto-stash 20260101-000000"): (0, ""),
            ("stash", "pop"): (0, ""),
            ("diff", "--name-only", "--diff-filter=U"): (0, ""),
            ("ls-files", "-z", "--", "*.py"): (0, "module.py\0"),
            ("merge-base", "--is-ancestor", "myfork/main", "HEAD"): (0, ""),
            ("push", "myfork", "HEAD:main"): (0, ""),
            ("fetch", "myfork", "main"): (0, ""),
        }
        base.update(extra or {})
        return base

    def test_clean_fast_forward_validates_and_fast_forwards_mirror(self):
        runner = _Runner(self.replies())
        outcome = update_cmd.run_update(root=self.root, runner=runner)
        self.assertIsNone(outcome.prompt)
        self.assertIn("fast-forwarded", outcome.report)
        self.assertIn("useful change", outcome.report)
        self.assertTrue(runner.used("push", "myfork", "HEAD:main"))
        self.assertFalse(any("--force" in part for call in runner.calls for part in call))

    def test_dirty_tree_is_stashed_merged_and_restored(self):
        with patch("frontends.update_cmd.time.strftime", return_value="20260101-000000"):
            runner = _Runner(self.replies({("status", "--porcelain"): (0, " M file.py")}))
            outcome = update_cmd.run_update("keep my edit", root=self.root, runner=runner)
        self.assertIsNone(outcome.prompt)
        self.assertIn("fast-forwarded", outcome.report)
        self.assertIn("stash", outcome.report)
        self.assertTrue(runner.used("branch", "backup/update-auto-20260101-000000", "HEAD"))
        self.assertTrue(runner.used("stash", "push", "-m", "/update auto-stash 20260101-000000"))
        self.assertTrue(runner.used("stash", "pop"))
        self.assertTrue(runner.used("push", "myfork", "HEAD:main"))
        self.assertFalse(any("-u" in part for call in runner.calls for part in call if call[3:][:1] == ("stash",)))

    def test_untracked_only_tree_is_never_stashed(self):
        runner = _Runner(self.replies({("status", "--porcelain"): (0, "?? secret.txt")}))
        outcome = update_cmd.run_update(root=self.root, runner=runner)
        self.assertIsNone(outcome.prompt)
        self.assertIn("fast-forwarded", outcome.report)
        self.assertFalse(any(call[3:][:1] == ("stash",) for call in runner.calls))
        self.assertFalse(any(call[3:][:2] == ("branch", "backup") for call in runner.calls))

    def test_stash_pop_conflict_is_handed_to_agent_without_push(self):
        with patch("frontends.update_cmd.time.strftime", return_value="20260101-000000"):
            runner = _Runner(self.replies({
                ("status", "--porcelain"): (0, " M file.py"),
                ("stash", "pop"): (1, "CONFLICT (content): Merge conflict in file.py"),
                ("diff", "--name-only", "--diff-filter=U"): (0, "file.py"),
            }))
            outcome = update_cmd.run_update("keep my edit", root=self.root, runner=runner)
        self.assertIn("stash pop", outcome.prompt)
        self.assertIn("file.py", outcome.system)
        self.assertIn("然后必须调用 `ask_user`", outcome.prompt)
        self.assertTrue(runner.used("stash", "push", "-m", "/update auto-stash 20260101-000000"))
        self.assertFalse(runner.used("push", "myfork", "HEAD:main"))

    def test_non_main_branch_is_rejected_without_remote_changes(self):
        runner = _Runner(self.replies({("branch", "--show-current"): (0, "feature")}))
        outcome = update_cmd.run_update(root=self.root, runner=runner)
        self.assertIn("requires the checked-out branch to be main", outcome.report)
        self.assertFalse(any(call[:1] == ("git",) and "fetch" in call for call in runner.calls))

    def test_merge_conflict_returns_short_repair_prompt_without_push(self):
        runner = _Runner(self.replies({
            ("merge", "--ff-only", "origin/main"): (1, "CONFLICT"),
            ("diff", "--name-only", "--diff-filter=U"): (0, "module.py"),
        }))
        outcome = update_cmd.run_update(root=self.root, runner=runner)
        self.assertIn("merge has conflicts", outcome.prompt)
        self.assertIn("```diff", outcome.system)
        self.assertIn("module.py", outcome.system)
        self.assertIn("然后必须调用 `ask_user`", outcome.prompt)
        self.assertIn("收到用户回答后", outcome.prompt)
        self.assertFalse(runner.used("push", "myfork", "HEAD:main"))

    def test_validation_failure_never_pushes_mirror(self):
        runner = _Runner(self.replies())
        original = runner

        def failing_tests(argv, cwd):
            if tuple(argv)[1:] == ("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"):
                original.calls.append(tuple(argv))
                return subprocess.CompletedProcess(argv, 1, "failure")
            return original(argv, cwd)

        outcome = update_cmd.run_update(root=self.root, runner=failing_tests)
        self.assertIn("unit tests failed", outcome.prompt)
        self.assertFalse(runner.used("push", "myfork", "HEAD:main"))

    def test_divergent_mirror_never_force_pushes(self):
        runner = _Runner(self.replies({
            ("merge-base", "--is-ancestor", "myfork/main", "HEAD"): (1, ""),
        }))
        outcome = update_cmd.run_update(root=self.root, runner=runner)
        self.assertIn("would require a force push", outcome.prompt)
        self.assertFalse(runner.used("push", "myfork", "HEAD:main"))
        self.assertFalse(any("--force" in part for call in runner.calls for part in call))

    def test_handle_success_enters_one_toolless_summary_turn(self):
        class Agent:
            pass

        agent = Agent()
        display = queue.Queue()
        report = "✅ 更新完成 · fast-forward\n上游更新：\n- 修复同步逻辑\n验证：通过"
        with patch("frontends.update_cmd.run_update", return_value=update_cmd.UpdateOutcome(report=report)):
            prompt = update_cmd.handle(agent, "", display)
        self.assertIn("最终的简洁中文汇报", prompt)
        self.assertTrue(agent._update_single_turn)
        self.assertIsNone(agent.__dict__.get("_pending_update_prompt"))
        self.assertTrue(display.empty())

    def test_handle_conflict_shows_notice_and_defers_prompt_consumption(self):
        class Agent:
            pass

        agent = Agent()
        display = queue.Queue()
        outcome = update_cmd.UpdateOutcome(
            system="冲突快照", diff="<<<<<<< ours", prompt="请解释冲突并询问我"
        )
        with patch("frontends.update_cmd.run_update", return_value=outcome):
            prompt = update_cmd.handle(agent, "保留语义", display)
        self.assertEqual(prompt, "请解释冲突并询问我")
        self.assertEqual(agent._pending_update_prompt, prompt)
        self.assertTrue(agent._update_conflict_just_shown)
        notice = display.get_nowait()
        self.assertEqual(notice["update_notice"], "冲突快照")
        self.assertEqual(notice["diff"], "<<<<<<< ours")

    def test_install_intercepts_only_literal_update(self):
        class App:
            def _handle_slash_cmd(self, raw_query, display_queue):
                return "original:" + raw_query

        app_queue = object()
        update_cmd.install(App)
        with patch("frontends.update_cmd.handle", return_value="repair prompt") as handle:
            self.assertEqual(App()._handle_slash_cmd("/update preserve", app_queue), "repair prompt")
        handle.assert_called_once()
        self.assertEqual(App()._handle_slash_cmd("/updates", app_queue), "original:/updates")


if __name__ == "__main__":
    unittest.main()
