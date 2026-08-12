import unittest
from types import SimpleNamespace

from ga import GenericAgentHandler
from llmcore import BaseSession, _TASK_ANCHOR_REFERENCE


class _Parent:
    task_dir = None


class _CapturingSession(BaseSession):
    def __init__(self):
        super().__init__({"apikey": "test", "apibase": "https://example.invalid"})
        self.sent = []
        self.reply = ''

    def make_messages(self, messages):
        return messages

    def raw_ask(self, messages):
        self.sent.append(messages)
        if self.reply:
            yield self.reply
        return []


class ContextTransportRecoveryTests(unittest.TestCase):
    def test_transport_error_does_not_request_model_completion(self):
        handler = GenericAgentHandler(_Parent())
        response = SimpleNamespace(
            content='\n\n!!!Error: HTTP 503: temporarily unavailable', thinking=''
        )

        gen = handler.do_no_tool({}, response)
        with self.assertRaises(StopIteration) as stopped:
            next(gen)
        outcome = stopped.exception.value

        self.assertTrue(outcome.should_exit)
        self.assertEqual(outcome.data, response)
        self.assertIsNone(outcome.next_prompt)
        self.assertFalse(hasattr(handler, '_empty_ct'))

    def test_task_anchor_is_projected_but_not_persisted_in_history(self):
        session = _CapturingSession()
        anchor = 'Keep this exact user task stable across every request.'
        session.begin_task(anchor)

        list(session.ask(anchor))

        self.assertEqual(session.history, [{'role': 'user', 'content': [{
            'type': 'text', 'text': _TASK_ANCHOR_REFERENCE
        }]}])
        self.assertEqual(session.sent[0][0], {
            'role': 'user', 'content': [{'type': 'text', 'text': anchor}]
        })
        self.assertEqual(session.sent[0][1:], session.history)
        self.assertNotIn(anchor, str(session.history))

    def test_transport_error_is_not_persisted_in_base_history(self):
        session = _CapturingSession()
        session.reply = '\n!!!Error: HTTP 503: temporarily unavailable'

        self.assertEqual(list(session.ask('continue safely')), [session.reply])
        self.assertEqual(session.history, [{'role': 'user', 'content': [{
            'type': 'text', 'text': 'continue safely'
        }]}])


if __name__ == '__main__':
    unittest.main()
