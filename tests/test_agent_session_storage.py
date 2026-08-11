import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frontends.session_storage import SessionStore


class GenericAgentSessionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(self.tmp.name)

    def _agent(self):
        with patch("agentmain.SessionStore", return_value=self.store), \
             patch("agentmain.reload_mykeys", return_value=({}, False)):
            from agentmain import GenericAgent
            return GenericAgent()

    def test_new_agent_uses_uuid_backed_hot_transcript(self):
        agent = self._agent()
        row = self.store.get(agent.session_id)
        self.assertIsNotNone(row)
        self.assertEqual(Path(agent.log_path), self.store.transcript_path(agent.session_id))
        self.assertTrue(Path(agent.log_path).is_file())
        self.assertEqual(row["state"], "hot")

    def test_activity_indexes_written_transcript(self):
        agent = self._agent()
        Path(agent.log_path).write_text("=== Prompt === question\nhello\n\n=== Response === answer\nworld\n", encoding="utf-8")
        agent.record_session_activity()
        self.assertEqual(self.store.get(agent.session_id)["turn_count"], 1)

    def test_fresh_session_rotates_to_a_new_uuid(self):
        agent = self._agent()
        old_id = agent.session_id
        agent.create_durable_session(title="new chat")
        self.assertNotEqual(agent.session_id, old_id)
        self.assertEqual(self.store.get(agent.session_id)["title"], "new chat")
        self.assertEqual(self.store.get(old_id)["state"], "hot")


if __name__ == "__main__":
    unittest.main()
