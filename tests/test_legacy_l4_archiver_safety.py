import importlib.util
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
COMPRESS_PATH = ROOT / "memory" / "L4_raw_sessions" / "compress_session.py"
SCHEDULER_PATH = ROOT / "reflect" / "scheduler.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegacyL4ArchiverSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / "raw"
        self.l4 = self.root / "l4"
        self.raw.mkdir()
        self.l4.mkdir()
        self.archiver = load_module("l4_archiver_safety", COMPRESS_PATH)

    def tearDown(self):
        self.tmp.cleanup()

    def _raw_file(self, name, body, age_seconds):
        path = self.raw / name
        path.write_text("=== Prompt === 2026-01-02 03:04:05\n" + body, encoding="utf-8")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def _retain_newest_ten(self):
        for index in range(10):
            self._raw_file(f"model_responses_zzz_new_{index}.txt", "recent", 60 - index)

    def test_small_skipped_raw_is_retained(self):
        target = self._raw_file("model_responses_000_old.txt", "too small", 10_000)
        self._retain_newest_ten()

        result = self.archiver.batch_process(self.raw, self.l4, dry_run=False)

        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["deleted_raw"], 0)
        self.assertTrue(target.exists())
        self.assertFalse(list(self.l4.glob("*.zip")))

    def test_only_verified_new_archive_allows_raw_deletion(self):
        target = self._raw_file("model_responses_old.txt", "x" * 6_000, 10_000)
        self._retain_newest_ten()

        result = self.archiver.batch_process(self.raw, self.l4, dry_run=False)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["deleted_raw"], 1)
        self.assertFalse(target.exists())
        archives = list(self.l4.glob("*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            self.assertEqual(archive.testzip(), None)
            self.assertEqual(len(archive.namelist()), 1)

    def test_scheduler_check_never_imports_or_runs_legacy_archiver(self):
        # Loading scheduler must not bind the agent's real singleton port or log.
        with patch("socket.socket"), patch("logging.FileHandler"):
            scheduler = load_module("scheduler_safety", SCHEDULER_PATH)
        with patch.object(scheduler, "TASKS", str(self.root / "no-tasks")), \
             patch("builtins.__import__", side_effect=AssertionError("unexpected import")):
            self.assertIsNone(scheduler.check())


if __name__ == "__main__":
    unittest.main()
