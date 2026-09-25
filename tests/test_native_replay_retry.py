"""Offline integration checks: real loopback HTTP, no provider credentials."""
import copy
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llmcore


def drain(gen):
    while True:
        try:
            next(gen)
        except StopIteration as e:
            return e.value


class ReplayRetryTests(unittest.TestCase):
    def test_native_records_immutable_and_trim_closed_groups(self):
        history = []
        for n in range(12):
            history.extend([
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': str(n),
                 'name': 'run', 'input': {'script': 'x' * 1500}, '_raw_arguments': '{ "script": "raw" }'}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(n), 'content': 'y' * 1600}]},
            ])
        original = copy.deepcopy(history)
        llmcore.compress_history_tags(history, keep_recent=0, force=True)
        self.assertEqual(history, original)
        session = SimpleNamespace(context_win=5000, history_char_limit=15000, trim_keep_prefix=1, cut_msg_interval=1)
        llmcore.trim_messages_history(history, session)
        self.assertLess(len(history), len(original))
        self.assertEqual(history, original[-len(history):])
        for call, result in zip(history[::2], history[1::2]):
            self.assertEqual(call['content'][0]['id'], result['content'][0]['tool_use_id'])
        wire = llmcore._msgs_claude2oai(history)
        self.assertEqual(wire[0]['tool_calls'][0]['function']['arguments'], '{ "script": "raw" }')

    def test_http_failure_retry_preserves_history_and_exposes_error(self):
        import agentmain
        for status in (400, 404, 503):
            with self.subTest(status=status):
                requests_seen = []
                class Endpoint(BaseHTTPRequestHandler):
                    def log_message(self, *args): pass
                    def do_POST(self):
                        requests_seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                        if len(requests_seen) == 1:
                            body = json.dumps({'error': {'code': 'test_failure', 'message': 'injected failure'}}).encode()
                            self.send_response(status)
                            self.send_header('Content-Type', 'application/json')
                        else:
                            body = b'data: {"choices":[{"delta":{"content":"recovered"}}]}\n\ndata: [DONE]\n\n'
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    session = llmcore.NativeOAISession({'apikey': 'offline-test', 'model': 'offline',
                        'apibase': 'http://127.0.0.1:%d/v1' % server.server_port, 'max_retries': 0})
                    client = llmcore.NativeToolClient(session)
                    session.history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'original task'}]},
                        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call1', 'name': 'run', 'input': {'x': 1}}]}]
                    client._pending_tool_ids = ['call1']
                    response = drain(client.chat([{'role': 'user', 'content': '', 'tool_results': [{'tool_use_id': 'call1', 'content': 'result ' * 1000}]}]))
                    self.assertEqual(response.error['status'], status)
                    self.assertFalse(response.tool_calls)
                    failed_history = copy.deepcopy(session.history)
                    agent = object.__new__(agentmain.GenericAgent)
                    agent.llmclient = client
                    notice = agent._prepare_retry(SimpleNamespace(put=lambda x: None))
                    self.assertIn(str(status), notice)
                    self.assertLess(len(notice), 2000)
                    self.assertEqual(session.history, failed_history)
                    recovered = drain(client.chat([{'role': 'user', 'content': notice}]))
                    self.assertEqual(recovered.content, 'recovered')
                    self.assertIsNone(session.last_error)
                    self.assertEqual(session.history[:-1], failed_history)
                    before, after = [r['messages'] for r in requests_seen]
                    self.assertEqual(after[:-1], before)
                    self.assertIn(str(status), str(after[-1]['content']))
                    self.assertEqual(sum(m['role'] == 'tool' for m in after), 1)
                finally:
                    server.shutdown(); server.server_close(); worker.join()

    def test_stream_failure_after_summary_discards_partial_calls(self):
        frames = [
            {'type': 'response.output_text.delta', 'delta': 'summary already emitted'},
            {'type': 'response.output_item.added', 'output_index': 0, 'item':
             {'type': 'function_call', 'call_id': 'partial', 'name': 'run', 'arguments': ''}},
            {'type': 'error', 'message': 'tool response cannot translate', 'code': 'translation_failed'},
        ]
        blocks = drain(llmcore._parse_openai_sse(iter(('data: ' + json.dumps(x)).encode() for x in frames), api_mode='responses'))
        self.assertEqual(blocks[0]['_error']['stage'], 'stream')
        self.assertFalse(any(b['type'] == 'tool_use' for b in blocks))
        session = llmcore.NativeOAISession({'apikey': '', 'apibase': 'http://127.0.0.1', 'model': 'offline'})
        def raw(messages):
            yield 'summary'
            return blocks
        session.raw_ask = raw
        response = drain(session.ask({'role': 'user', 'content': [{'type': 'text', 'text': 'task'}]}))
        self.assertTrue(response.error)
        self.assertEqual(len(session.history), 1)
        self.assertFalse(response.tool_calls)

    def test_run_retry_reuses_handler_without_spill(self):
        import queue
        import tempfile
        from unittest.mock import patch
        import agentmain
        with patch.object(agentmain.GenericAgent, 'load_llm_sessions'):
            agent = agentmain.GenericAgent()
        backend = llmcore.NativeOAISession({'apikey': 'offline', 'apibase': 'http://127.0.0.1', 'model': 'offline'})
        backend.history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'x' * 6000}]}]
        backend.last_error = {'message': 'HTTP 400'}
        agent.llmclient = llmcore.NativeToolClient(backend)
        handler = SimpleNamespace(working={'key_info': 'checkpoint', 'passed_sessions': 7},
                                  code_stop_signal=[1], _last_exit='error', history_info=['existing history'])
        agent.handler = handler
        agent.task_anchor = 'original task'
        agent.history = ['existing history']
        output = queue.Queue()
        agent.task_queue.put({'query': '/retry', 'source': 'test', 'output': output})
        agent.task_queue.put('stop')
        seen = []
        def runner(client, system, query, actual_handler, tools, **kwargs):
            seen.append((query, actual_handler))
            yield {'turn': 1}
            yield 'continued'
        with tempfile.TemporaryDirectory() as tmp, patch.object(agentmain, 'script_dir', tmp), patch.object(agentmain, 'get_system_prompt', return_value='system'), patch.object(agentmain, 'agent_runner_loop', runner), patch.object(agentmain, 'GenericAgentHandler', side_effect=AssertionError('handler rebuilt')):
            agent.run()
            self.assertEqual(list(Path(tmp).rglob('user_prompt*')), [])
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][1], handler)
        self.assertIn('HTTP 400', seen[0][0])
        self.assertLess(len(seen[0][0]), 1000)
        self.assertEqual(agent.task_anchor, 'original task')
        self.assertEqual(agent.history, ['existing history'])
        self.assertEqual(handler.working['passed_sessions'], 7)
        self.assertFalse(any('Backend Error' in str(item) or '❌' in str(item) for item in list(output.queue)))

    def test_loop_stops_before_dispatch_on_model_error(self):
        from unittest.mock import patch
        import agent_loop
        response = SimpleNamespace(error={'status': 400}, content='HTTP 400', tool_calls=[])
        def chat(**kwargs):
            yield 'HTTP 400'
            return response
        def forbidden(*args, **kwargs):
            raise AssertionError('failed request dispatched a tool')
        handler = SimpleNamespace(parent=SimpleNamespace(task_dir=None), dispatch=forbidden)
        with patch.object(agent_loop, '_hook'):
            list(agent_loop.agent_runner_loop(SimpleNamespace(chat=chat), 'system', 'task', handler, []))
        self.assertEqual(handler._last_exit['result'], 'MODEL_ERROR')

    def test_error_quote_is_not_transport_error(self):
        session = llmcore.NativeOAISession({'apikey': '', 'apibase': 'http://127.0.0.1', 'model': 'offline'})
        def raw(messages):
            yield 'quoted error'
            return [{'type': 'text', 'text': 'The log says !!!Error: HTTP 400.'}]
        session.raw_ask = raw
        response = drain(session.ask({'role': 'user', 'content': [{'type': 'text', 'text': 'explain'}]}))
        self.assertFalse(getattr(response, 'error', None))


if __name__ == '__main__':
    unittest.main()
