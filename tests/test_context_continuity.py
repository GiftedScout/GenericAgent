import json
import unittest
from types import SimpleNamespace

import llmcore
from ga import GenericAgentHandler, resolve_task_anchor


class _Parent:
    task_dir = ""

    def get_ctx_multiplier(self):
        return 1.0


class ContextContinuityTests(unittest.TestCase):
    def test_compression_cadence_is_isolated_per_session(self):
        # cadence 计数器按 session(counter_owner)隔离。trim 在 cadence 调用后紧接
        # force=True 压缩（计数器复位为 0），故断言压缩行为而非计数器终值。
        # 需足够多消息才能被截断（keep_recent 保留旧消息窗口 + 9 条最小保留），
        # 且 trim_keep_prefix 保护前缀，须为 0。
        def make_sess():
            return SimpleNamespace(cut_msg_interval=1, context_win=500, trim_keep_prefix=0)
        def make_history():
            return [{"role": "user", "content": [{"type": "text", "text":
                     "<tool_result>" + "x" * 500 + "</tool_result>"}]} for _ in range(14)]
        cost = lambda ms: sum(len(json.dumps(m, ensure_ascii=False)) for m in ms)

        first, second = make_sess(), make_sess()
        h1 = make_history(); c0 = cost(h1)
        llmcore.trim_messages_history(h1, first)
        self.assertTrue(hasattr(first, "_history_compress_cd"))
        self.assertFalse(hasattr(second, "_history_compress_cd"))   # 隔离：second 未受影响
        self.assertLess(cost(h1), c0)                               # first 历史被压缩

        h2 = make_history()
        llmcore.trim_messages_history(h2, second)
        self.assertLess(cost(h2), c0)                               # second 独立触发压缩
        self.assertTrue(hasattr(first, "_history_compress_cd"))

        h3 = make_history()
        llmcore.trim_messages_history(h3, first)
        self.assertLess(cost(h3), c0)                               # first 再次独立压缩

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
        # ced532a unified history_char_limit: context_win*3 chars (~3 chars/token),
        # ssh-tunnel backends no longer special-cased.
        self.assertEqual(qwen.history_char_limit, 393216)
        self.assertEqual(qwen.cut_msg_interval, 30)
        # SSH-tunnel llama.cpp models default to a 0.6 retention floor
        # (their large windows tolerate keeping more reasoning history).
        self.assertEqual(qwen.trim_keep_rate, 0.6)
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
        # Display stream wraps reasoning in the <thinking> envelope
        # (see tests/test_thinking_envelope.py); history blocks stay clean.
        self.assertEqual(displayed, ["\n<thinking>\n", "inspect files", "\n</thinking>\n", "completed"])
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


    def test_task_anchor_survives_retry_and_ask_user_answer(self):
        """/retry 与 ask_user 回答继承锚点；实质新指令覆盖锚点。"""
        task = "部署 pde2 到云端并验证 ACR 推送"
        # /retry（网络中断后原样重发）继承锚点
        self.assertEqual(resolve_task_anchor("/retry", task, None), task)
        # ask_user 退出后的回答 = 对提问的独立回复，无论长短都不覆盖锚点
        human = {'result': 'EXITED', 'data': {'status': 'INTERRUPT', 'intent': 'HUMAN_INTERVENTION'}}
        self.assertEqual(resolve_task_anchor("选A", task, human), task)
        self.assertEqual(resolve_task_anchor("可以", task, human), task)
        self.assertEqual(resolve_task_anchor("选方案B，但注意保留回滚脚本", task, human), task)
        # 正常完成后的新实质任务 → 覆盖（锚点跟随最近实质任务，而非会话首条）
        new_task = "改为部署到自建 k8s 集群并写回滚脚本和文档"
        self.assertEqual(resolve_task_anchor(new_task, task, {'result': 'CURRENT_TASK_DONE'}), new_task)
        # 异常退出(_last_exit缺失)时的实质输入 → 覆盖（"继续"不再是特判，普通文本即新任务）
        self.assertEqual(resolve_task_anchor("换个话题", task, None), "换个话题")

    def test_prepare_retry_pops_interrupted_user_message(self):
        """/retry: 尾部是未响应的 user 消息→弹出原样重发(str)；
        尾部是 assistant（/continue 恢复 / Ctrl+C 打断在工具中）→ nudge 兜底；空历史→拒绝。"""
        from types import SimpleNamespace
        import agentmain
        agent = object.__new__(agentmain.GenericAgent)
        dq = []
        display_queue = SimpleNamespace(put=dq.append)
        hist = [
            {"role": "user", "content": [{"type": "text", "text": "原始任务"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        agent.llmclient = SimpleNamespace(backend=SimpleNamespace(history=hist))
        # 中断残留：尾部是未响应的 user 消息（文本）→ 重发原文
        hist.append({"role": "user", "content": [{"type": "text", "text": "[ERROR] 继续上次"}]})
        r = agent._prepare_retry(display_queue)
        self.assertEqual(r, "[ERROR] 继续上次")
        self.assertEqual(len(hist), 2)                     # 中断消息已弹出
        # 上一轮正常结束（尾部是 assistant，如 /continue 恢复）→ nudge 兜底，不 pop
        hist.append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})
        r = agent._prepare_retry(display_queue)
        self.assertIsInstance(r, str)
        self.assertIn("断点", r)
        self.assertEqual(len(hist), 3)                     # 未被弹出
        # 尾部 user 为 blocks（tool_result + text）→ 重组为 str 重发
        hist.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "OUT"},
            {"type": "text", "text": "next step"}]})
        r = agent._prepare_retry(display_queue)
        self.assertIn("OUT", r)
        self.assertIn("next step", r)
        self.assertEqual(len(hist), 3)
        # 空历史 → 拒绝
        hist.clear()
        self.assertIsNone(agent._prepare_retry(display_queue))

    def test_compress_history_tags_protects_task_anchor_block(self):
        """task_anchor 标签整体保护（替换为[...]），不被 800 字符截断成孤儿标签。"""
        anchor = "<task_anchor>" + "部署任务" * 200 + "</task_anchor>"
        msg = {"role": "user", "content": [{"type": "text", "text": anchor + " 后续对话内容" * 200}]}
        llmcore.compress_history_tags([msg], keep_recent=0, force=True)
        text = msg["content"][0]["text"]
        self.assertNotIn("部署任务" * 5, text)          # 锚点内容被折叠
        self.assertIn("<task_anchor>[...]</task_anchor>", text)


if __name__ == "__main__":
    unittest.main()
