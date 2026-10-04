"""Run: python3 -m unittest discover -s tests -p test_trim_abandoned.py."""
import copy
import io
import json
import contextlib
import unittest
from types import SimpleNamespace

import llmcore
from frontends import continue_cmd


def msg(role, *blocks):
    return {'role': role, 'content': list(blocks)}


def text(s):
    return {'type': 'text', 'text': s}


def call(tid):
    return {'type': 'tool_use', 'id': tid, 'name': 'write', 'input': {'value': 'unchanged'}}


def result(tid):
    return {'type': 'tool_result', 'tool_use_id': tid, 'content': 'real result'}


def sess(cap=10000, prefix=0):
    return SimpleNamespace(context_win=cap // 3, history_char_limit=cap,
                           trim_keep_prefix=prefix, trim_keep_rate=.25)


def cost(h):
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in h)


class AbandonedTrimTests(unittest.TestCase):
    def test_parallel_partial_real_results_and_idempotence(self):
        h = [msg('assistant', call('a'), call('b')), msg('user', result('a'), text('new task'))]
        original = copy.deepcopy(h)
        self.assertEqual(llmcore.repair_missing_tool_results(h), 1)
        self.assertEqual(h[0], original[0])
        self.assertEqual(h[1]['content'][1:], original[1]['content'])
        missing = h[1]['content'][0]
        self.assertEqual(missing['tool_use_id'], 'b')
        self.assertTrue(missing['is_error'])
        self.assertIn('unknown', missing['content'])
        self.assertEqual(llmcore.repair_missing_tool_results(h), 0)

    def test_delayed_real_result_never_replaced_and_live_tail_preserved(self):
        for h in ([msg('assistant', call('a')), msg('user', text('wait')),
                   msg('assistant', text('waiting')), msg('user', result('a'))],
                  [msg('user', text('task')), msg('assistant', call('a'))]):
            original = copy.deepcopy(h)
            self.assertEqual(llmcore.repair_missing_tool_results(h), 0)
            self.assertEqual(h, original)

    def test_string_user_and_consecutive_assistants(self):
        h = [msg('assistant', call('a')), msg('assistant', call('b')),
             {'role': 'user', 'content': 'new task'}]
        self.assertEqual(llmcore.repair_missing_tool_results(h), 2)
        self.assertEqual(h[-1]['content'][-1], text('new task'))
        self.assertEqual([b['tool_use_id'] for b in h[-1]['content'][:-1]], ['a', 'b'])

    def test_under_budget_unchanged_except_one_time_protocol_repair(self):
        h = [msg('assistant', call('a')), msg('user', text('new task'))]
        llmcore.trim_messages_history(h, sess())
        original = copy.deepcopy(h)
        for _ in range(10): llmcore.trim_messages_history(h, sess())
        self.assertEqual(h, original)
        self.assertEqual(len(h[-1]['content']), 2)

    def test_abandoned_call_no_longer_pins_history(self):
        h = [msg('assistant', call('old')), msg('user', text('new task'))]
        for i in range(30): h.append(msg('user' if i % 2 == 0 else 'assistant', text(str(i) + 'x' * 1000)))
        tail = copy.deepcopy(h[-4:])
        llmcore.trim_messages_history(h, sess())
        self.assertLessEqual(cost(h), 10000)
        self.assertEqual(h[-4:], tail)
        self.assertFalse(any(b.get('id') == 'old' for m in h for b in m['content']))

    def test_unachievable_recent_floor_is_relaxed_to_fit_budget(self):
        tag = 'key_' + 'info'
        h = [msg('user', text('<' + tag + '>' + 'x' * 6000 + '</' + tag + '>'))]
        h.extend(msg('user' if i % 2 else 'assistant', text('z' * 4000)) for i in range(5))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            llmcore.trim_messages_history(h, sess(cap=1000))
        self.assertLessEqual(cost(h), 1000)
        self.assertIn('preference relaxed', out.getvalue())
        original = copy.deepcopy(h)
        llmcore.trim_messages_history(h, sess(cap=1000))
        self.assertEqual(h, original)
        self.assertEqual(llmcore.STATS['ctx'], cost(h))

    def test_huge_escaped_text_result_and_image_preserved(self):
        image = {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/a'}}
        h = [msg('assistant', call('a')), msg('user', result('a'), image, text('"\\\n' * 4000))]
        llmcore.trim_messages_history(h, sess(cap=1200))
        self.assertLessEqual(cost(h), 1200)
        self.assertIn(image, h[-1]['content'])
        self.assertFalse(any(b.get('type') == 'tool_result' for b in h[-1]['content']))

    def test_live_tail_keeps_arguments_during_manual_compact(self):
        h = [msg('user', text('x' * 8000)), msg('assistant', text('old answer')),
             msg('user', text('current task')), msg('assistant', call('live'))]
        live = copy.deepcopy(h[-1])
        llmcore.trim_messages_history(h, sess(cap=1000), force=True)
        self.assertEqual(h[-1], live)
        self.assertLessEqual(cost(h), 1000)

    def test_protected_prefix_signed_and_native_payloads_preserved(self):
        tag = 'key_' + 'info'
        prefix = msg('user', text('<' + tag + '>' + 'p' * 3000 + '</' + tag + '>'))
        h = [prefix]
        h.extend(msg('user' if i % 2 else 'assistant', text('x' * 2000)) for i in range(20))
        signed = {'type': 'thinking', 'thinking': 'secret', 'signature': 'signed'}
        h.extend([msg('assistant', signed, call('live')), msg('user', result('live'))])
        tail = copy.deepcopy(h[-4:]); oldprefix = copy.deepcopy(prefix)
        llmcore.trim_messages_history(h, sess(cap=20000, prefix=1))
        self.assertEqual(h[0], oldprefix)
        self.assertEqual(h[-4:], tail)
        self.assertLessEqual(cost(h), 20000)

    def test_restore_all_entry_points_share_repair_without_log_mutation(self):
        pairs = [(json.dumps(msg('user', text('task'))), repr([call('old')])),
                 (json.dumps(msg('user', text('new task'))), repr([text('answer')]))]
        original = copy.deepcopy(pairs)
        h = continue_cmd._parse_native_history(pairs)
        self.assertEqual(pairs, original)
        self.assertTrue(h[2]['content'][0]['is_error'])
        self.assertEqual(h[2]['content'][0]['tool_use_id'], 'old')


if __name__ == '__main__': unittest.main()
