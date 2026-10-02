"""Runnable regression: python3 -m unittest discover -s tests -p test_compact.py."""
import ast
import copy
import json
import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import llmcore
from agentmain import GenericAgent

ROOT = Path(__file__).resolve().parents[1]


def cost(history):
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in history)


def history(n=20):
    return [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': [{'type': 'text', 'text': f'{i:02d}' + 'x' * 1000}]}
            for i in range(n)]


def session(cap=1000000, rate=0.6, prefix=0):
    return SimpleNamespace(history_char_limit=cap, context_win=cap // 3,
                           trim_keep_rate=rate, trim_keep_prefix=prefix)


def frontend_method(path, name):
    # Execute the actual frontend handler without initializing terminal/GUI state.
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {'_t': lambda key: key}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
    return ns[name]


class CompactTests(unittest.TestCase):
    def test_current_size_target_independent_of_window(self):
        for cap in (1000, 1000000):
            for rate in (0.6, 0.4):
                with self.subTest(cap=cap, rate=rate):
                    h = history(); tail = copy.deepcopy(h[-4:]); before = cost(h)
                    llmcore.trim_messages_history(h, session(cap, rate), force=True)
                    self.assertLessEqual(cost(h), int(before * rate))
                    self.assertGreater(cost(h), int(before * rate) - 1200)
                    self.assertEqual(h[-4:], tail)

    def test_auto_under_limit_unchanged_and_over_limit_uses_cap(self):
        h = history(); original = copy.deepcopy(h)
        llmcore.trim_messages_history(h, session())
        self.assertEqual(h, original)
        llmcore.trim_messages_history(h, session(cap=10000))
        self.assertLessEqual(cost(h), 6000)
        self.assertGreater(cost(h), 4800)

    def test_native_transactions_and_prefix_preserved(self):
        h = history(2)
        for i in range(10):
            h.extend([
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': str(i),
                 'name': 'test', 'input': {'data': 'x' * 500}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(i),
                 'content': 'y' * 500}]},
            ])
        original = copy.deepcopy(h); tail = copy.deepcopy(h[-4:])
        llmcore.trim_messages_history(h, session(prefix=2), force=True)
        self.assertLess(cost(h), cost(original))
        self.assertEqual(h[:2], original[:2]); self.assertEqual(h[-4:], tail)
        pending = set()
        for msg in h:
            for block in msg['content']:
                if block['type'] == 'tool_use': pending.add(block['id'])
                if block['type'] == 'tool_result':
                    self.assertIn(block['tool_use_id'], pending)
                    pending.remove(block['tool_use_id'])
        self.assertFalse(pending)

    def test_tag_only_compression_updates_stats(self):
        h = history(20)
        tag = 'tool_' + 'result'
        for m in h[:-4]:
            m['content'][0]['text'] = '<' + tag + '>' + 'x' * 5000 + '</' + tag + '>'
        llmcore.trim_messages_history(h, session(), force=True)
        self.assertEqual(len(h), 20)
        self.assertEqual(llmcore.STATS['ctx'], cost(h))

    def test_real_agent_queue_no_llm_or_anchor_change(self):
        agent = GenericAgent.__new__(GenericAgent)
        be = session(); be.history = history()
        agent.llmclient = SimpleNamespace(backend=be)
        agent.task_anchor = 'unchanged'; agent.handler = SimpleNamespace(working={'key_info': 'keep'})
        agent.history = ['keep']; agent.all_outputs = []
        agent.task_queue = queue.Queue(); output = queue.Queue(); before = cost(be.history)
        agent.task_queue.put({'query': '/compact', 'source': 'user', 'output': output})
        agent.task_queue.put('quit')
        thread = threading.Thread(target=agent.run); thread.start(); thread.join(5)
        self.assertFalse(thread.is_alive())
        reply = output.get_nowait()
        self.assertEqual(reply['source'], 'system')
        self.assertIn(f'{int(before * 0.6):,}', reply['done'])
        self.assertLessEqual(cost(be.history), int(before * 0.6))
        self.assertEqual(agent.task_anchor, 'unchanged')
        self.assertEqual(agent.handler.working, {'key_info': 'keep'})
        self.assertEqual(agent.history, ['keep']); self.assertEqual(agent.all_outputs, [])

    def test_recent_floor_and_empty_report(self):
        for h in ([], history(4)):
            agent = GenericAgent.__new__(GenericAgent); be = session(); be.history = h
            agent.llmclient = SimpleNamespace(backend=be); out = queue.Queue()
            original = copy.deepcopy(h)
            self.assertIsNone(agent._handle_slash_cmd('/compact', out))
            reply = out.get_nowait()['done']
            self.assertEqual(h, original)
            if h: self.assertIn('未达到目标', reply)

    def test_frontends_forward_and_block_when_busy(self):
        v3 = frontend_method('frontends/tui_v3.py', '_cmd')
        v2 = frontend_method('frontends/tuiapp_v2.py', '_cmd_compact')
        for busy in (False, True):
            a = SimpleNamespace(_bridge=SimpleNamespace(agent=object()), _running=busy,
                                commit=Mock(), _submit=Mock())
            v3(a, '/compact')
            if busy: a._submit.assert_not_called(); a.commit.assert_called_once()
            else: a._submit.assert_called_once_with('/compact', [])
            b = SimpleNamespace(current=SimpleNamespace(status='running' if busy else 'idle'),
                                _system=Mock(), submit_user_message=Mock())
            v2(b, [], '/compact')
            if busy: b.submit_user_message.assert_not_called(); b._system.assert_called_once()
            else: b.submit_user_message.assert_called_once_with('/compact', display_text='/compact')


if __name__ == '__main__':
    unittest.main()
