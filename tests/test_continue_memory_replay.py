"""/continue must restore answers, never memory-control cards or chatter."""
import json
import tempfile
import unittest
from pathlib import Path
from frontends import continue_cmd as replay

MARK = '[后台记忆维护]'
CURRENT = '### ' + MARK + ' 这是当前任务中的静默记忆结算步骤，不是新的用户请求。'
LEGACY = '### ' + MARK + ' 这是任务完成后的内部结算阶段。'


def prompt(text='', results=()):
    return json.dumps({'role': 'user', 'content': list(results) +
                       ([{'type': 'text', 'text': text}] if text else [])}, ensure_ascii=False)


def text(value):
    return {'type': 'text', 'text': value}


def tool(name, ident='t'):
    return {'type': 'tool_use', 'id': ident, 'name': name, 'input': {}}


def result(value, ident='t'):
    return {'type': 'tool_result', 'tool_use_id': ident, 'content': value}


class MemoryReplayTests(unittest.TestCase):
    def restore(self, pairs):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'log.txt'
            log.write_text(''.join('=== Prompt ===\n' + p + '\n=== Response ===\n' + repr(r) + '\n'
                                   for p, r in pairs), encoding='utf-8')
            native = replay.parse_native_log(str(log))
            messages = replay.extract_ui_messages(str(log))
        self.assertEqual(len(native), 2 * len(pairs))  # display filters must not alter audit/history
        return messages, '\n'.join(m['content'] for m in messages)

    def test_current_final_answer_and_later_task(self):
        messages, shown = self.restore([
            (prompt('task one'), [text('progress'), tool('start_long_term_update')]),
            (prompt(CURRENT, [result('SECRET L0')]), [text('SECRET maintenance'), tool('file_read', 'r')]),
            (prompt('', [result('SECRET memory', 'r')]), [text('SECRET patch'), tool('file_patch', 'p')]),
            (prompt('', [result('ok', 'p')]), [text('COMPLETE FINAL ANSWER')]),
            (prompt('task two'), [text('SECOND ANSWER')]),
        ])
        self.assertIn('COMPLETE FINAL ANSWER', shown)
        self.assertIn('SECOND ANSWER', shown)
        self.assertNotIn('SECRET', shown)
        self.assertNotIn('start_long_term_update', shown)
        self.assertNotIn('file_read', shown)
        self.assertNotIn('file_patch', shown)
        self.assertNotIn('📄 结果', shown)
        self.assertEqual([m['content'] for m in messages if m['role'] == 'user'], ['task one', 'task two'])
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user', 'assistant'])

    def test_current_direct_final(self):
        _, shown = self.restore([
            (prompt('task'), [text('progress'), tool('start_long_term_update')]),
            (prompt(CURRENT, [result('L0')]), [text('FINAL ANSWER')]),
        ])
        self.assertIn('FINAL ANSWER', shown)
        self.assertNotIn('start_long_term_update', shown)
        self.assertNotIn('📄 结果', shown)

    def test_legacy_stays_hidden_through_retry(self):
        _, shown = self.restore([
            (prompt('task'), [text('OLD FINAL ANSWER'), tool('start_long_term_update')]),
            (prompt(LEGACY, [result('SECRET L0')]), [text('SECRET memory'), tool('file_patch')]),
            (prompt('[ERROR] Incomplete response. Regenerate and tooluse.'), [text('SECRET retry')]),
            (prompt('next task'), [text('NEXT ANSWER')]),
        ])
        self.assertIn('OLD FINAL ANSWER', shown)
        self.assertIn('NEXT ANSWER', shown)
        self.assertNotIn('SECRET', shown)
        self.assertNotIn('start_long_term_update', shown)

    def test_literal_marker_in_user_question_is_not_control(self):
        messages, shown = self.restore([
            (prompt('为什么 ' + MARK + ' 会显示？'), [text('EXPLANATION')]),
        ])
        self.assertEqual(messages[0]['role'], 'user')
        self.assertIn('EXPLANATION', shown)

    def test_normal_tool_and_answer_unchanged(self):
        _, shown = self.restore([
            (prompt('task'), [text('progress'), tool('code_run')]),
            (prompt('', [result('NORMAL OUTPUT')]), [text('NORMAL ANSWER')]),
        ])
        self.assertIn('code_run', shown)
        self.assertIn('NORMAL OUTPUT', shown)
        self.assertIn('NORMAL ANSWER', shown)


if __name__ == '__main__':
    unittest.main()
