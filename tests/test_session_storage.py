import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from frontends.session_storage import (
    LONG_AGE_SECONDS, SHORT_AGE_SECONDS, SessionStore,
)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SessionStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_activity_and_promotion_are_uuid_backed(self):
        row = self.store.create_session(title="initial")
        sid = row["session_id"]
        transcript = self.store.transcript_path(sid)
        transcript.write_text("=== Prompt === x\nhello\n\n=== Response === x\nworld\n", encoding="utf-8")
        self.store.record_activity(sid, summary="verified summary")
        row = self.store.promote(sid, "rename", title="renamed", workspace="/work/demo")
        self.assertEqual(row["class"], "long")
        self.assertEqual(row["title"], "renamed")
        self.assertEqual(row["workspace_history"], ["/work/demo"])
        self.assertEqual(row["turn_count"], 1)
        sidecar = json.loads((self.store.hot_dir(sid) / "session.json").read_text())
        self.assertEqual(sidecar["summary"], "verified summary")

    def test_eligibility_honours_short_and_long_retention(self):
        short = self.store.create_session()["session_id"]
        long = self.store.create_session()["session_id"]
        self.store.promote(long, "workspace", workspace="/work")
        now = int(time.time())
        with self.store._connect() as conn:
            conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (now - SHORT_AGE_SECONDS - 1, short))
            conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (now - LONG_AGE_SECONDS + 1, long))
        self.assertEqual([x["session_id"] for x in self.store.eligible(now)], [short])

    def test_archive_is_verified_immutable_and_restorable(self):
        sid = self.store.create_session(title="archive me")["session_id"]
        self.store.transcript_path(sid).write_text("=== Prompt === t\nhello\n", encoding="utf-8")
        archived = self.store.archive(sid)
        self.assertEqual(archived["state"], "archived")
        archive = self.root / archived["archive_path"]
        self.assertTrue(archive.is_file())
        self.assertFalse(self.store.hot_dir(sid).exists())
        restored = self.store.restore(sid)
        self.assertEqual((restored / "transcript.txt").read_text(encoding="utf-8"), "=== Prompt === t\nhello\n")
        self.assertEqual(self.store.archive(sid)["archive_path"], archived["archive_path"])

    def test_final_verification_failure_preserves_hot_registry_state(self):
        sid = self.store.create_session()["session_id"]
        original = self.store._verify_archive
        calls = 0

        def fail_final(path, session_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("simulated final verification failure")
            return original(path, session_id)

        with patch.object(self.store, "_verify_archive", side_effect=fail_final):
            with self.assertRaisesRegex(ValueError, "final verification"):
                self.store.archive(sid)
        self.assertEqual(self.store.get(sid)["state"], "hot")
        self.assertTrue(self.store.hot_dir(sid).is_dir())

    def test_active_session_is_not_archived(self):
        sid = self.store.create_session()["session_id"]
        with self.store.lock(sid):
            with self.assertRaisesRegex(RuntimeError, "active/locked"):
                self.store.archive(sid)
        self.assertEqual(self.store.get(sid)["state"], "hot")


if __name__ == "__main__":
    unittest.main()
