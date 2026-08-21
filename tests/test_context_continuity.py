import unittest
from types import SimpleNamespace

import llmcore
from ga import GenericAgentHandler


class _Parent:
    task_dir = ""

    def get_ctx_multiplier(self):
        return 1.0


class ContextContinuityTests(unittest.TestCase):
    def test_compression_cadence_is_isolated_per_session(self):
        first = SimpleNamespace(cut_msg_interval=2, context_win=1000, trim_keep_prefix=1)
        second = SimpleNamespace(cut_msg_interval=2, context_win=1000, trim_keep_prefix=1)
        history = [{"role": "user", "content": [{"type": "text", "text": "plain"}]}]

        llmcore.trim_messages_history(history, first)
        self.assertEqual(first._history_compress_cd, 1)
        self.assertFalse(hasattr(second, "_history_compress_cd"))

        llmcore.trim_messages_history(history, second)
        self.assertEqual(second._history_compress_cd, 1)
        self.assertEqual(first._history_compress_cd, 1)

        llmcore.trim_messages_history(history, first)
        self.assertEqual(first._history_compress_cd, 2)
        self.assertEqual(second._history_compress_cd, 1)

    def test_original_task_is_bounded_and_stable_in_anchor_prompt(self):
        task = "OCR this image and preserve the table layout. " * 80
        handler = GenericAgentHandler(_Parent(), cwd="/tmp", original_task=task)
        handler.current_turn = 3
        handler.history_info = ["[USER]: later request", "[Agent]: later progress"]

        prompt = handler._get_anchor_prompt()

        self.assertIn("<task_anchor>", prompt)
        self.assertIn("OCR this image", prompt)
        self.assertLessEqual(len(handler.original_task), 1200)
        self.assertIn("[USER]: later request", prompt)
        self.assertEqual(prompt.count("<task_anchor>"), 1)


if __name__ == "__main__":
    unittest.main()
