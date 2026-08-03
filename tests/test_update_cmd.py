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

    def test_dirty_tree_is_handed_to_agent_without_fetch(self):
        runner = _Runner(self.replies({("status", "--porcelain"): (0, " M file.py")}))
        outcome = update_cmd.run_update("keep my edit", root=self.root, runner=runner)
        self.assertIn("uncommitted changes", outcome.prompt)
        self.assertIn("keep my edit", outcome.prompt)
        self.assertFalse(runner.used("fetch", "--prune", "origin"))

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

    def test_install_intercepts_only_literal_update(self):
        class App:
            def _handle_slash_cmd(self, raw_query, display_queue):
                return "original:" + raw_query

        update_cmd.install(App)
        queue = object()
        with patch("frontends.update_cmd.handle", return_value="repair prompt") as handle:
            self.assertEqual(App()._handle_slash_cmd("/update preserve", queue), "repair prompt")
        handle.assert_called_once()
        self.assertEqual(App()._handle_slash_cmd("/updates", queue), "original:/updates")


if __name__ == "__main__":
    unittest.main()
