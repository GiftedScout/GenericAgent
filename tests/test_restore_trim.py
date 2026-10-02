"""Regression for interrupted native requests losing tool results on restore."""
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llmcore
from frontends import continue_cmd


def message(role, blocks):
    return {'role': role, 'content': blocks}


def text(value):
    return {'type': 'text', 'text': value}


def log_record(label, value):
    body = json.dumps(value, ensure_ascii=False) if label == 'Prompt' else repr(value)
    return f'=== {label} === 2026-10-02 11:03:25\n{body}\n\n'


def interrupted_log():
    call = {'type': 'tool_use', 'id': 'call_write', 'name': 'file_write',
            'input': {'content': 'audit'}, '_raw_arguments': '{"content":"audit"}'}
    result = {'type': 'tool_result', 'tool_use_id': 'call_write', 'content': 'success'}
    records = [('Prompt', message('user', [text('start')])),
               ('Response', [call]),
               # This request was interrupted after the successful tool result.
               ('Prompt', message('user', [result, text('working memory')])),
               ('Prompt', message('user', [text('retry')])),
               ('Response', [text('continued')])]
    for n in range(24):
        records.extend([('Prompt', message('user', [text(str(n) + 'x' * 1500)])),
                        ('Response', [text('answer')])])
    return ''.join(log_record(*r) for r in records), call, result


class RestoreTrimTests(unittest.TestCase):
    def test_consecutive_prompts_keep_real_result_and_order(self):
        content, call, result = interrupted_log()
        pairs = continue_cmd._pairs(content)
        history = continue_cmd._parse_native_history(pairs)
        self.assertEqual(len(pairs), 26)
        self.assertEqual(history[1]['content'], [call])
        self.assertEqual(history[2]['content'], [result, text('working memory'), text('retry')])

    def test_restore_then_trim_closes_transaction(self):
        content, _, _ = interrupted_log()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.txt'
            path.write_text(content)
            history = continue_cmd.parse_native_log(path)
            # Exercise all restore entry points, not just the public parser.
            for loader in (continue_cmd.restore, continue_cmd._load_history_into):
                backend = SimpleNamespace(history=[])
                agent = SimpleNamespace(llmclient=SimpleNamespace(backend=backend),
                                        abort=lambda: None)
                _, full = loader(agent, path)
                self.assertTrue(full)
                self.assertEqual(backend.history, history)
        sess = SimpleNamespace(context_win=3000, history_char_limit=9000,
                               trim_keep_prefix=0, trim_keep_rate=0.25, cut_msg_interval=7)
        llmcore.trim_messages_history(history, sess)
        self.assertLessEqual(sum(len(json.dumps(m, ensure_ascii=False)) for m in history), 9000)
        self.assertFalse(any(b.get('id') == 'call_write' for m in history for b in m['content']))

    def test_dangling_prompt_and_text_log_semantics_unchanged(self):
        complete = log_record('Prompt', message('user', [text('hello')])) + log_record('Response', [text('hi')])
        tail = log_record('Prompt', message('user', [text('unfinished')]))
        self.assertEqual(continue_cmd._pairs(complete + tail), continue_cmd._pairs(complete))
        self.assertEqual(continue_cmd._pairs(tail), [])
        self.assertEqual(continue_cmd._pairs('=== Prompt ===\nold\n=== Prompt ===\nnew\n=== Response ===\nreply'), [('new', 'reply')])

    def test_three_native_prompts_preserve_blocks_in_order(self):
        blocks = [[text('first')], [{'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/image'}}], [text('third')]]
        content = ''.join(log_record('Prompt', message('user', b)) for b in blocks) + log_record('Response', [text('ok')])
        history = continue_cmd._parse_native_history(continue_cmd._pairs(content))
        self.assertEqual(history[0]['content'], sum(blocks, []))

    def test_identical_retry_does_not_duplicate_tool_results(self):
        prompt = message('user', [{'type': 'tool_result', 'tool_use_id': 'same', 'content': 'ok'}, text('retry')])
        content = log_record('Prompt', prompt) * 2 + log_record('Response', [text('done')])
        history = continue_cmd._parse_native_history(continue_cmd._pairs(content))
        self.assertEqual(history[0], prompt)

    @unittest.skipUnless(os.environ.get('GA_RESTORE_TEST_LOG'), 'set GA_RESTORE_TEST_LOG for real-log regression')
    def test_actual_log_restore_and_replay_stay_under_budget(self):
        history = continue_cmd.parse_native_log(os.environ['GA_RESTORE_TEST_LOG'])
        self.assertTrue(history)
        for rate in (0.6, 0.25):
            sess = llmcore.NativeOAISession({'apikey': '', 'apibase': 'http://127.0.0.1',
                                           'model': 'gpt-6.1-sol', 'context_win': 70000,
                                           'trim_keep_rate': rate})
            restored = json.loads(json.dumps(history))
            llmcore.trim_messages_history(restored, sess)
            self.assertLessEqual(sum(len(json.dumps(m, ensure_ascii=False)) for m in restored), sess.history_char_limit)
            replay = []
            for m in history:
                replay.append(json.loads(json.dumps(m)))
                if m['role'] == 'user':
                    llmcore.trim_messages_history(replay, sess)
                    self.assertLessEqual(sum(len(json.dumps(x, ensure_ascii=False)) for x in replay), sess.history_char_limit)

    def test_native_http_request_uses_configured_keep_rate(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        seen = []

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if self.path.split('?', 1)[0].endswith('/messages'):
                    response = {'content': [text('ok')], 'stop_reason': 'end_turn'}
                elif self.path.endswith('/responses'):
                    response = {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'ok'}]}]}
                else:
                    response = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            content, _, _ = interrupted_log()
            original = continue_cmd._parse_native_history(continue_cmd._pairs(content))
            for cls, mode in ((llmcore.NativeOAISession, 'chat_completions'),
                              (llmcore.NativeOAISession, 'responses'),
                              (llmcore.NativeClaudeSession, 'messages')):
                sizes = []
                for rate in (0.6, 0.25):
                    sess = cls({'apikey': '', 'apibase': f'http://127.0.0.1:{server.server_port}',
                                'model': 'offline', 'stream': False, 'api_mode': mode,
                                'context_win': 3000, 'trim_keep_rate': rate,
                                'max_retries': 0, 'connect_timeout': 2, 'read_timeout': 2})
                    sess.history = json.loads(json.dumps(original))
                    self.assertEqual(''.join(sess.ask(message('user', [text('continue')]))), 'ok')
                    self.assertIsNone(sess.last_error)
                    sizes.append(len(json.dumps(seen[-1], ensure_ascii=False)))
                    self.assertLess(sizes[-1], 9000)
                with self.subTest(cls=cls.__name__, mode=mode):
                    self.assertLess(sizes[1], sizes[0])
            self.assertEqual(len(seen), 6)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_trim_configuration_for_every_backend(self):
        for cls in (llmcore.BaseSession, llmcore.LLMSession, llmcore.ClaudeSession,
                    llmcore.NativeOAISession, llmcore.NativeClaudeSession):
            for model in ('gpt-6.1-sol', 'claude-test', 'deepseek-test', 'qwen-test'):
                with self.subTest(cls=cls.__name__, model=model):
                    cfg = {'apikey': '', 'apibase': 'http://127.0.0.1', 'model': model,
                           'context_win': 70000, 'trim_keep_rate': 0.25,
                           'trim_keep_prefix': 0, 'cut_msg_interval': 3}
                    sess = cls(cfg)
                    self.assertEqual(getattr(sess, 'trim_keep_rate', None), 0.25)
                    self.assertEqual(sess.context_win, 70000)
                    self.assertEqual(sess.trim_keep_prefix, 0)
                    self.assertEqual(sess.cut_msg_interval, 3)
                    self.assertEqual(getattr(cls({k: v for k, v in cfg.items() if k != 'trim_keep_rate'}), 'trim_keep_rate', None), 0.6)


if __name__ == '__main__':
    unittest.main()
