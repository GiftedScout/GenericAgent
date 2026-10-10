"""SSH must share code_run's borderless card in live and replayed TUI v2."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontends'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tuiapp_v2 as tui
from test_tui_fold_window import FoldProbe
from frontends import continue_cmd as replay
from agent_loop import get_pretty_json
from plugins import hooks
from rich.style import Style


def block(args, data):
    return ('🛠️ Tool: `ssh_run`  📥 args:\n' + '`' * 4 + 'text\n' +
            get_pretty_json(args) + '\n' + '`' * 4 + '\n' + '`' * 5 + '\n' +
            json.dumps(data, ensure_ascii=False) + '\n' + '`' * 5)


class SSHFoldProbe(FoldProbe):
    theme = 'ga-default'
    _render_md = tui.GenericAgentTUI._render_md
    on_click = tui.GenericAgentTUI.on_click

    def _messages_width(self):
        return max(10, self.query_one('#messages').content_size.width)

    def _at_root(self):
        return str(Path(__file__).resolve().parents[1] / 'temp')


def no_background(rendered):
    for span in rendered.spans:
        style = Style.parse(span.style) if isinstance(span.style, str) else span.style
        assert style.bgcolor is None, str(style)


class SSHCardTests(unittest.TestCase):
    def setUp(self):
        self.caps = dict(tui._WRITE_CAP)
        self.registry = {k: list(v) for k, v in hooks._registry.items()}
        self.installed = tui.GenericAgentTUI._write_snapshot_hook_installed
        tui._WRITE_CAP.clear()
        tui.GenericAgentTUI._write_snapshot_hook_installed = False
        tui.GenericAgentTUI._install_write_snapshot_hook(SimpleNamespace())
        self.renderer = SimpleNamespace(_at_root=lambda: '.', theme='ga-default')

    def tearDown(self):
        tui._WRITE_CAP.clear()
        tui._WRITE_CAP.update(self.caps)
        hooks._registry.clear()
        hooks._registry.update(self.registry)
        tui.GenericAgentTUI._write_snapshot_hook_installed = self.installed

    def capture(self, args, data):
        ctx = {'tool_name': 'ssh_run', 'args': dict(args, _index=0, _tool_num=1),
               'ret': SimpleNamespace(data=data)}
        hooks.trigger('tool_before', ctx)
        hooks.trigger('tool_after', ctx)
        cap = tui._WRITE_CAP[hash(get_pretty_json(args))]
        self.assertEqual(cap['data'], data)
        return cap

    def render(self, args, data, width=80):
        self.capture(args, data)
        rendered = tui.GenericAgentTUI._render_md(self.renderer, block(args, data), width)
        no_background(rendered.text)
        self.assertNotIn('🛠️ Tool:', rendered.source)
        self.assertNotIn('password_file', rendered.source)
        return rendered

    def test_live_hooks_and_borderless_output(self):
        args = {'host': 'a800', 'password_file': '~/sshpassword', 'script': 'print(5050)'}
        r = self.render(args, {'status': 'success', 'stdout': '5050\n', 'exit_code': 0})
        for value in ('ssh_run(python)', 'host: a800', '│ print(5050)', '└ 5050'):
            self.assertIn(value, r.source)
        _, expected = tui._render_code_card(args, {'status': 'success', 'stdout': '5050\n', 'exit_code': 0}, 79, 'ssh_run')
        self.assertEqual(r.source.strip(), expected.strip())

    def test_error_timeout_and_stderr_are_not_hidden(self):
        for data in ({'status': 'error', 'exit_code': 255, 'stderr': 'Permission denied'},
                     {'status': 'timeout', 'timed_out': True, 'stderr': 'network unavailable', 'note': 'remote_state unknown'}):
            with self.subTest(data=data):
                r = self.render({'host': '服务器', 'script': 'print(1)'}, data, 42)
                self.assertIn('✗', r.source)
                self.assertIn(data['stderr'], r.source)
                if data.get('timed_out'):
                    self.assertIn('超时', r.source)

    def test_task_and_transfer_cards(self):
        for action in ('status', 'logs', 'wait', 'stop', 'upload', 'download'):
            args = {'host': 'a800', 'action': action, 'task_id': 'job1',
                    'local_path': '本地.txt', 'remote_path': '/tmp/远程.txt'}
            data = {'status': 'success', 'stdout': 'log 中文\n', 'exit_code': 0,
                    'task_id': 'job1', 'task_state': 'succeeded', 'task_exit_code': 0}
            r = self.render(args, data)
            self.assertIn('ssh_run(' + action + ')', r.source)
            self.assertIn('task_state: succeeded', r.source)
            if action in ('upload', 'download'):
                self.assertIn('→', r.source)
        failed = self.render({'host': 'a800', 'action': 'status'},
                             {'status': 'success', 'exit_code': 0, 'task_state': 'failed', 'task_exit_code': 9})
        self.assertIn('✗', failed.source)
        self.assertIn('task_exit_code: 9', failed.source)

    def test_repeated_args_keep_each_occurrence_result(self):
        args = {'host': 'a800', 'script': 'print(1)'}
        failure = {'status': 'error', 'exit_code': 7, 'stderr': 'FIRST_FAILURE'}
        success = {'status': 'success', 'exit_code': 0, 'stdout': 'SECOND_SUCCESS'}
        self.capture(args, success)
        r = tui.GenericAgentTUI._render_md(self.renderer, block(args, failure) + '\n\n' + block(args, success), 80)
        self.assertEqual(r.source.count('FIRST_FAILURE'), 1)
        self.assertEqual(r.source.count('SECOND_SUCCESS'), 1)
        self.assertEqual(r.source.count('Exit 7'), 1)
        no_background(r.text)

    def test_continue_restores_ssh_card(self):
        args = {'host': 'a800', 'script': 'print(5050)'}
        data = {'status': 'success', 'stdout': '5050\n', 'exit_code': 0}
        pairs = [(json.dumps({'role': 'user', 'content': [{'type': 'text', 'text': 'test ssh'}]}),
                  [{'type': 'tool_use', 'id': 'ssh1', 'name': 'ssh_run', 'input': args}]),
                 (json.dumps({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'ssh1', 'content': json.dumps(data)}]}),
                  [{'type': 'text', 'text': 'done'}])]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'log.txt'
            path.write_text(''.join('=== Prompt ===\n' + p + '\n=== Response ===\n' + repr(r) + '\n' for p, r in pairs))
            caps = replay.iter_write_captures(path)
            messages = replay.extract_ui_messages(path)
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]['data'], data)
        tui._WRITE_CAP[hash(get_pretty_json(args))] = caps[0]
        text = '\n'.join(m['content'] for m in messages if m['role'] == 'assistant')
        r = tui.GenericAgentTUI._render_md(self.renderer, text, 80)
        self.assertIn('ssh_run(python)', r.source)
        self.assertIn('└ 5050', r.source)
        no_background(r.text)

    def test_actual_textual_fold_expand_and_resize(self):
        args = {'host': 'a800', 'script': 'print("你好")'}
        data = {'status': 'success', 'stdout': '你好\n', 'exit_code': 0}
        self.capture(args, data)

        async def scenario():
            app = SSHFoldProbe()
            message = tui.ChatMessage('assistant', '**LLM Running (Turn 1) ...**\n' + block(args, data) +
                                      '\n**LLM Running (Turn 2) ...**\nFinal answer')
            app.current = SimpleNamespace(messages=[message])
            async with app.run_test(size=(100, 35)) as pilot:
                container = app.query_one('#messages')
                app._mount_message(container, message)
                await pilot.pause()
                self.assertFalse(any(s[0] == 'fold-body' for s in app._assistant_segments(message, 100)))
                self.assertTrue(await pilot.click(tui.FoldHeader))
                await pilot.pause()
                self.assertIn(0, message._toggled_folds)
                bodies = [s[1] for s in app._assistant_segments(message, 100) if s[0] == 'fold-body']
                self.assertEqual(len(bodies), 1)
                self.assertIn('ssh_run(python)', bodies[0].source)
                self.assertIn('└ 你好', bodies[0].source)
                no_background(bodies[0].text)
                from textual.selection import Selection
                widget = next(w for w in message._segment_widgets
                              if getattr(w, '_ga_render', None) is not None
                              and 'ssh_run(python)' in w._ga_render.source)
                copied, _ = widget.get_selection(Selection(None, None))
                self.assertEqual(copied, widget._ga_render.source)
                self.assertNotIn('🛠️ Tool:', copied)
                self.assertIn('└ 你好', copied)
                await pilot.resize_terminal(48, 35)
                await pilot.pause()
                for width in (48, 100):
                    for kind, body, _ in app._assistant_segments(message, width):
                        if kind == 'fold-body':
                            no_background(body.text)
                            self.assertIn('你好', body.source)
                out = Path(__file__).resolve().parents[1] / 'temp' / 'ssh_tui_acceptance'
                out.mkdir(exist_ok=True)
                app.save_screenshot('expanded.svg', str(out))
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
