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
    def test_qwen_uses_deepseek_style_context_and_retains_reasoning(self):
        qwen = llmcore.BaseSession({
            "apikey": "", "apibase": "http://127.0.0.1:18080/v1",
            "model": "qwen3.8-27b", "ssh_tunnel": "qwen3-27b",
            "context_win": 131072, "max_tokens": 8192,
        })
        self.assertEqual(qwen.context_win, 131072)
        self.assertEqual(qwen.history_char_limit, 122880)
        self.assertEqual(qwen.cut_msg_interval, 30)
        self.assertEqual(qwen.trim_keep_rate, 0.3)
        self.assertFalse(qwen.omit_thinking)

        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"inspect files"}}]}',
            'data: {"choices":[{"delta":{"content":"completed"}}]}',
            "data: [DONE]",
        ]
        stream = llmcore._parse_openai_sse(lines)
        displayed = []
        try:
            while True:
                displayed.append(next(stream))
        except StopIteration as done:
            blocks = done.value
        self.assertEqual(displayed, ["inspect files", "completed"])
        self.assertEqual(blocks, [
            {"type": "thinking", "thinking": "inspect files"},
            {"type": "text", "text": "completed"},
        ])

        payload = llmcore._msgs_claude2oai([{"role": "assistant", "content": blocks}])
        self.assertEqual(payload[0]["reasoning_content"], "inspect files")
        self.assertEqual(payload[0]["content"], [{"type": "text", "text": "completed"}])

        class StubNativeOAI(llmcore.NativeOAISession):
            def raw_ask(self, messages):
                yield "inspect files"
                yield "completed"
                return blocks

        session = StubNativeOAI({
            "apikey": "", "apibase": "http://127.0.0.1:18080/v1",
            "model": "qwen3.8-27b", "ssh_tunnel": "qwen3-27b",
        })
        list(session.ask({"role": "user", "content": [{"type": "text", "text": "go"}]}))
        self.assertEqual(session.history[-1]["content"], blocks)
        replay = llmcore._msgs_claude2oai(session.history)
        self.assertEqual(replay[-1]["reasoning_content"], "inspect files")


if __name__ == "__main__":
    unittest.main()
