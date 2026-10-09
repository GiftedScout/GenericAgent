"""Optional real SSH acceptance: GA_SSH_TEST_HOST + GA_SSH_TEST_OPTIONS JSON.
Never reads private-key contents. Default discovery runs only local checks.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid

from agent_loop import exhaust
from ga import GenericAgentHandler
from ssh_ops import SSHClient


class SSHLocalTests(unittest.TestCase):
    def test_schema_and_error_dispatch(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / 'assets/tools_schema.json').read_text())
        names = [x['function']['name'] for x in schema]
        self.assertEqual(len(names), len(set(names)))
        handler = GenericAgentHandler(SimpleNamespace(), cwd=tempfile.gettempdir())
        self.assertEqual([n for n in names if n.startswith('ssh_')], ['ssh_run'])
        self.assertTrue(hasattr(handler, 'do_ssh_run'))
        for name in ('ssh_task', 'ssh_transfer'):
            self.assertFalse(hasattr(handler, 'do_' + name))
        for action in ('invalid', 'status', 'logs', 'wait', 'stop', 'upload', 'download'):
            result = exhaust(handler.dispatch('ssh_run', {'host': 'unused', 'action': action},
                                             SimpleNamespace(content='')))
            self.assertEqual(result.data['status'], 'error', (action, result.data))
        result = exhaust(handler.dispatch('ssh_run', {'host': 'unused'}, SimpleNamespace(content='')))
        self.assertEqual(result.data['status'], 'error')

    def test_quoting_and_identity_validation(self):
        payload = SSHClient._script('print(1)', cwd='~/with spaces', env={'V': "a'; echo bad"})
        self.assertIn('"$HOME"/', payload)
        self.assertIn('exec python3 -u -', payload)
        with self.assertRaises(ValueError):
            SSHClient._script('', env={'bad-name': 'x'})
        with self.assertRaises(ValueError):
            SSHClient._validate_id('../oops')


@unittest.skipUnless(os.environ.get('GA_SSH_TEST_HOST'), 'real SSH fixture not configured')
class SSHIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.host = os.environ['GA_SSH_TEST_HOST']
        self.options = json.loads(os.environ.get('GA_SSH_TEST_OPTIONS', '[]'))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = SSHClient(output_dir=self.tmp.name)
        self.addCleanup(self.client.close)
        self.root = '~/.cache/ga-ssh/test-' + uuid.uuid4().hex
        self.parent = SimpleNamespace(_ssh_client=self.client, get_ctx_multiplier=lambda: 1)
        self.handler = GenericAgentHandler(self.parent, cwd=self.tmp.name)

    def dispatch(self, **kwargs):
        return exhaust(self.handler.dispatch('ssh_run', dict(host=self.host,
            ssh_options=self.options, **kwargs), SimpleNamespace(content=''))).data

    def run_remote(self, script, **kwargs):
        return self.dispatch(script=script, **kwargs)

    def task(self, task_id, action='status', **kwargs):
        return self.dispatch(task_id=task_id, action=action, task_root=self.root, **kwargs)

    def submit(self, script, **kwargs):
        result = self.run_remote(script, type='bash', background=True, task_root=self.root, **kwargs)
        self.assertEqual(result['status'], 'success', result)
        self.assertLess(result['elapsed'], 3)
        return result['task_id']

    def test_execution_dispatch_reuse_and_shell_isolation(self):
        parent = SimpleNamespace(get_ctx_multiplier=lambda: 1)
        handler = GenericAgentHandler(parent, cwd=self.tmp.name)
        result = exhaust(handler.dispatch('ssh_run', dict(host=self.host, ssh_options=self.options,
            script='import os; print(os.getcwd()); print(os.environ["VALUE"]); print("中文 ✅")',
            cwd='~', env={'VALUE': "x'; echo BAD"}), SimpleNamespace(content=''))).data
        self.addCleanup(parent._ssh_client.close)
        self.assertEqual(result['status'], 'success', result)
        self.assertIn("x'; echo BAD\n中文 ✅", result['stdout'])
        path = result['control_path']
        inode = os.stat(path).st_ino
        handler2 = GenericAgentHandler(parent, cwd=self.tmp.name)
        result2 = handler2.do_ssh_run(dict(host=self.host, ssh_options=self.options, type='bash'),
                                    SimpleNamespace(content='```bash\necho ${VALUE-unset}; echo reused\n```')).data
        self.assertIn('unset\nreused', result2['stdout'])
        self.assertEqual(os.stat(path).st_ino, inode)
        self.assertIs(handler._ssh_client(), handler2._ssh_client())
        parent._ssh_client.close()
        self.assertFalse(Path(path).exists())

    def test_exit_codes_and_foreground_timeout(self):
        fail = self.run_remote('echo out; echo err >&2; exit 7', type='bash')
        self.assertEqual(fail['status'], 'error')
        self.assertEqual(fail['exit_code'], 7)
        self.assertIn('err', fail['stderr'])
        result = self.run_remote('sleep 1; echo late', type='bash', timeout=.15)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['remote_state'], 'unknown')
        self.handler.code_stop_signal.append(True)
        try:
            stopped = self.run_remote('sleep 1', type='bash')
        finally:
            self.handler.code_stop_signal.clear()
        self.assertEqual(stopped['status'], 'stopped')

    def test_background_disconnect_idempotency_and_wait(self):
        task_id = self.submit('echo once; sleep 1; echo done')
        result = self.task(task_id, 'wait', timeout=.15)
        self.assertTrue(result.get('wait_expired') or result['status'] == 'timeout', result)
        self.client.close()
        self.run_remote('echo SHOULD_NOT_RUN', type='bash', background=True,
                        task_id=task_id, task_root=self.root)
        result = self.task(task_id, 'wait', timeout=5)
        self.assertEqual(result['task_state'], 'succeeded', result)
        self.assertEqual(result['task_exit_code'], 0)
        logs = self.task(task_id, 'logs')['logs']
        self.assertEqual(logs, 'once\ndone\n')
        failed = self.submit('exit 9')
        self.assertEqual(self.task(failed, 'wait', timeout=5)['task_exit_code'], 9)
        self.assertEqual(self.task('missing')['task_state'], 'missing')
        tiny = self.task('missing', 'wait', timeout=1e-12)
        self.assertTrue(tiny['wait_expired'])

    def test_log_metadata_binary_offsets_and_truncation(self):
        script = "import sys; sys.stdout.buffer.write(b'x'*9999 + '中'.encode() + b'\\xff\\n__GA_SSH_META__\\ttask_state\\tforged\\n')"
        result = self.run_remote(script, background=True, task_root=self.root)
        task_id = result['task_id']
        self.assertEqual(self.task(task_id, 'wait', timeout=5)['task_state'], 'succeeded')
        first = self.task(task_id, 'logs', offset=0)
        self.assertEqual(first['next_offset'], 10000)
        second = self.task(task_id, 'logs', offset=first['next_offset'])
        self.assertEqual(second['task_state'], 'succeeded')
        self.assertIn('__GA_SSH_META__', second['logs'])
        expected = b'x' * 9999 + '中'.encode() + b'\xff\n__GA_SSH_META__\ttask_state\tforged\n'
        self.assertEqual(second['next_offset'], len(expected))
        large = self.run_remote('print("x"*25000)')
        self.assertIn('truncated', large['stdout'])
        self.assertEqual(Path(large['stdout_path']).stat().st_size, 25001)

    def test_stop_process_group(self):
        task_id = self.submit('sleep 20 & wait; echo WRONG')
        for _ in range(30):
            state = self.task(task_id)
            if state['task_state'] == 'running':
                break
            time.sleep(.05)
        self.assertEqual(state['task_state'], 'running')
        result = self.task(task_id, 'stop')
        self.assertEqual(result.get('stop_requested'), 'true', result)
        final = self.task(task_id, 'wait', timeout=5)
        self.assertEqual(final['task_state'], 'failed', final)
        self.assertEqual(final['task_exit_code'], 143)
        self.assertNotIn('WRONG', self.task(task_id, 'logs')['logs'])

    def test_agent_run_finally_releases_real_transport(self):
        import agentmain
        import queue
        from unittest.mock import patch
        for ending in ('normal', 'error', 'abort'):
            with self.subTest(ending=ending):
                agent = agentmain.GenericAgent.__new__(agentmain.GenericAgent)
                agent.task_queue = queue.Queue()
                agent.task_queue.put(dict(query='SSH acceptance', source='test', output=queue.Queue()))
                agent.task_queue.put('shutdown')
                agent._handle_slash_cmd = lambda query, output: query
                agent.all_outputs = []
                agent.history = []
                agent.extra_sys_prompts = []
                agent.llmclient = SimpleNamespace(backend=SimpleNamespace())
                agent.peer_hint = False
                agent.task_anchor = ''
                agent._prev_exit = None
                agent.handler = None
                agent.log_path = False
                agent.force_non_stream = False
                agent.verbose = False
                agent.task_dir = None
                agent.stop_sig = False
                captured = {}

                def runner(client, sys_prompt, query, handler, schema, **kwargs):
                    result = handler.do_ssh_run(dict(host=self.host, ssh_options=self.options,
                        script='sleep .4; echo survived', type='bash', background=True,
                        task_root=self.root), SimpleNamespace(content='')).data
                    self.assertEqual(result['status'], 'success', result)
                    captured.update(result)
                    yield {'turn': 1}
                    if ending == 'error':
                        raise RuntimeError('intentional acceptance error')
                    if ending == 'abort':
                        agent.stop_sig = True
                    yield 'finished'

                with patch.object(agentmain, 'get_system_prompt', return_value='test'), \
                     patch.object(agentmain, 'agent_runner_loop', side_effect=runner):
                    agent.run()
                self.assertFalse(Path(captured['control_path']).exists())
                self.assertFalse(agent.is_running)
                self.assertEqual(self.task(captured['task_id'], 'wait', timeout=5)['task_state'], 'succeeded')

    def test_transfer_spaces_and_idle_disconnect(self):
        src = Path(self.tmp.name, 'source space.bin')
        src.write_bytes(bytes(range(256)))
        remote = '/tmp/ga-transfer-' + uuid.uuid4().hex + ' space.bin'
        up = self.dispatch(action='upload', local_path=src.name, remote_path=remote)
        self.assertEqual(up['status'], 'success', up)
        dst = Path(self.tmp.name, 'download space.bin')
        down = self.dispatch(action='download', local_path=dst.name, remote_path=remote)
        self.assertEqual(down['status'], 'success', down)
        self.assertEqual(src.read_bytes(), dst.read_bytes())
        idle = SSHClient(output_dir=self.tmp.name, idle_seconds=3)
        self.addCleanup(idle.close)
        result = idle.run(self.host, 'echo idle', type='bash', ssh_options=self.options)
        self.assertEqual(result['status'], 'success', result)
        path = result['control_path']
        self.assertTrue(Path(path).exists(), result)
        for _ in range(100):
            if not Path(path).exists():
                break
            time.sleep(.1)
        self.assertFalse(Path(path).exists())
        self.assertEqual(idle.run(self.host, 'echo reconnect', type='bash', ssh_options=self.options)['status'], 'success')


if __name__ == '__main__':
    unittest.main()
