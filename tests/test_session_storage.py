import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frontends.session_storage import (
    LONG_AGE_SECONDS, SHORT_AGE_SECONDS, SessionStore, promote_agent_session,
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

    def test_promote_agent_session_records_user_value_signal(self):
        sid = self.store.create_session(title="initial")["session_id"]
        agent = SimpleNamespace(session_store=self.store, session_id=sid)
        row = promote_agent_session(agent, "rename", title="important", workspace="/work/demo")
        self.assertEqual(row["class"], "long")
        self.assertEqual(row["promotion_reason"], "rename")
        self.assertEqual(row["title"], "important")
        self.assertEqual(row["workspace_history"], ["/work/demo"])

    def test_promote_agent_session_ignores_legacy_agent(self):
        self.assertIsNone(promote_agent_session(SimpleNamespace(log_path="legacy.log"), "rename", title="old"))

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

    def test_janitor_dry_run_plans_only_retention_eligible_rows(self):
        now = 2_000_000_000
        short = self.store.create_session(title="old short")["session_id"]
        long = self.store.create_session(title="old long")["session_id"]
        recent = self.store.create_session(title="recent")["session_id"]
        self.store.promote(long, "rename")
        with self.store._connect() as conn:
            conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (now - SHORT_AGE_SECONDS, short))
            conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (now - LONG_AGE_SECONDS, long))
            conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (now - 1, recent))
        result = self.store.janitor(now=now)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual({row["session_id"] for row in result["sessions"]}, {short, long})
        self.assertTrue(all(row["status"] == "planned" for row in result["sessions"]))
        self.assertTrue(all(self.store.get(sid)["state"] == "hot" for sid in (short, long, recent)))

    def test_janitor_apply_archives_independent_candidates_and_skips_active(self):
        now = 2_000_000_000
        active = self.store.create_session(title="active")["session_id"]
        eligible = self.store.create_session(title="eligible")["session_id"]
        for sid in (active, eligible):
            self.store.transcript_path(sid).write_text("transcript", encoding="utf-8")
        with self.store._connect() as conn:
            for sid in (active, eligible):
                conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (now - SHORT_AGE_SECONDS, sid))
        with self.store.lock(active):
            result = self.store.janitor(apply=True, now=now)
        by_id = {row["session_id"]: row for row in result["sessions"]}
        self.assertEqual(result["archived_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(by_id[eligible]["status"], "archived")
        self.assertEqual(by_id[active]["status"], "skipped")
        self.assertIn("active/locked", by_id[active]["reason"])
        self.assertEqual(self.store.get(active)["state"], "hot")
        self.assertEqual(self.store.get(eligible)["state"], "archived")
        self.assertEqual((self.store.restore(eligible) / "transcript.txt").read_text(encoding="utf-8"), "transcript")

    def test_session_janitor_cli_defaults_to_dry_run_and_emits_json(self):
        from frontends.session_janitor import main
        sid = self.store.create_session()["session_id"]
        now = int(time.time())
        with self.store._connect() as conn:
            conn.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?", (now - SHORT_AGE_SECONDS, sid))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([], store=self.store), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["sessions"][0]["session_id"], sid)
        self.assertEqual(self.store.get(sid)["state"], "hot")


if __name__ == "__main__":
    unittest.main()
