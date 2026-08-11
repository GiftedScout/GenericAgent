import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frontends import continue_cmd
from frontends.session_storage import SessionStore


class _DurableAgent:
    def __init__(self, store):
        self.session_store = store
        self.session_id = ""
        self.log_path = ""
        self.history = ["old"]
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=["old"]))
        self.llmclients = [self.llmclient]
        self.handler = object()
        self.create_durable_session()

    def abort(self):
        pass

    def create_durable_session(self):
        row = self.session_store.create_session()
        self.session_id = row["session_id"]
        self.log_path = str(self.session_store.transcript_path(self.session_id))


class ContinueSessionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(self.tmp.name)
        self.agent = _DurableAgent(self.store)

    def test_clear_creates_new_uuid_hot_session(self):
        old_id = self.agent.session_id
        with patch("frontends.continue_cmd.release_current"), \
             patch("frontends.continue_cmd.acquire_lock", return_value=True):
            continue_cmd.begin_fresh_session(self.agent)
        self.assertNotEqual(self.agent.session_id, old_id)
        self.assertEqual(Path(self.agent.log_path), self.store.transcript_path(self.agent.session_id))
        self.assertEqual(self.agent.history, [])
        self.assertTrue(self.store.get(old_id))

    def test_inplace_continue_binds_existing_hot_uuid(self):
        target = self.store.create_session()["session_id"]
        path = self.store.transcript_path(target)
        path.write_text("=== Prompt === p\nhello\n\n=== Response === r\nworld\n", encoding="utf-8")
        with patch("frontends.continue_cmd.acquire_lock", return_value=True), \
             patch("frontends.continue_cmd.release_lock"), \
             patch("frontends.continue_cmd._load_history_into", return_value=("ok", True)):
            message, ok = continue_cmd.continue_inplace(self.agent, str(path))
        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertEqual(self.agent.session_id, target)
        self.assertEqual(self.agent.log_path, str(path))

    def test_copy_continue_creates_uuid_hot_copy_without_changing_source(self):
        source = self.store.create_session()["session_id"]
        source_path = self.store.transcript_path(source)
        body = "=== Prompt === p\nhello\n\n=== Response === r\nworld\n"
        source_path.write_text(body, encoding="utf-8")
        old_id = self.agent.session_id
        with patch("frontends.continue_cmd.release_current"), \
             patch("frontends.continue_cmd.acquire_lock", return_value=True), \
             patch("frontends.continue_cmd._load_history_into", return_value=("ok", True)):
            message, ok = continue_cmd.continue_copy(self.agent, str(source_path))
        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertNotEqual(self.agent.session_id, old_id)
        self.assertNotEqual(self.agent.session_id, source)
        self.assertEqual(Path(self.agent.log_path).read_text(encoding="utf-8"), body)
        self.assertEqual(source_path.read_text(encoding="utf-8"), body)


if __name__ == "__main__":
    unittest.main()
