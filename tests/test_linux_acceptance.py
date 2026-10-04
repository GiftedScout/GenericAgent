"""Runnable Linux acceptance without GUI control or real credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ga import code_run
from frontends import slash_cmds, workspace_cmd


def execute(script, kind, cwd):
    gen = code_run(script, code_type=kind, cwd=str(cwd), myprint=lambda *a, **k: None)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


class LinuxAcceptance(unittest.TestCase):
    def test_real_bash_python_and_failure(self):
        with tempfile.TemporaryDirectory() as td:
            bash = execute("printf 'Ubuntu 中文\\n'; printf data > artifact.txt", 'bash', td)
            self.assertEqual(bash['exit_code'], 0)
            self.assertIn('Ubuntu 中文', bash['stdout'])
            self.assertEqual((Path(td) / 'artifact.txt').read_text(), 'data')
            py = execute("from pathlib import Path\nprint(Path('artifact.txt').read_text())", 'python', td)
            self.assertEqual(py['exit_code'], 0)
            self.assertIn('data', py['stdout'])
            self.assertEqual(execute('exit 17', 'bash', td)['exit_code'], 17)
            self.assertEqual(execute('Write-Host nope', 'powershell', td)['status'], 'error')

    def test_shell_fallback_and_unicode(self):
        for shell in ('/usr/bin/zsh', '/nonexistent/ga-shell'):
            with patch.dict(os.environ, {'SHELL': shell}):
                slash_cmds._USER_SHELL = None
                command, _ = slash_cmds.detect_user_shell()
                out = subprocess.check_output(command + ["printf '中文 ok'"], text=True)
                self.assertEqual(out, '中文 ok')
        slash_cmds._USER_SHELL = None

    def test_symlink_cleanup_cannot_delete_real_target(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / '真实 dir'
            target.mkdir()
            sentinel = target / 'keep.txt'
            sentinel.write_text('keep')
            link = Path(td) / 'workspace'
            self.assertTrue(workspace_cmd.make_dir_link(str(target), str(link)))
            self.assertEqual((link / 'keep.txt').read_text(), 'keep')
            self.assertFalse(workspace_cmd.make_dir_link(str(target), str(link)))
            self.assertFalse(workspace_cmd.remove_dir_link(str(target)))
            self.assertTrue(workspace_cmd.remove_dir_link(str(link)))
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_fresh_system_prompts_and_schema_in_both_languages(self):
        for lang in ('zh', 'en'):
            with tempfile.TemporaryDirectory() as td:
                script = "import agentmain,json; p=agentmain.get_system_prompt(); assert 'computer_use' in p; print(json.dumps(agentmain.TOOLS_SCHEMA))"
                env = dict(os.environ, GA_LANG=lang, GA_MEMORY_DIR=td)
                proc = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                tools = json.loads(proc.stdout.strip().splitlines()[-1])
                code = next(t['function'] for t in tools if t['function']['name'] == 'code_run')
                self.assertEqual(code['parameters']['properties']['type']['enum'], ['python', 'bash'])

    def test_acp_stdio_initialize_is_clean_jsonrpc(self):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': 1}}
        proc = subprocess.run([sys.executable, 'frontends/genericagent_acp_bridge.py'], cwd=ROOT,
                              input=json.dumps(request)+'\n', capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        messages = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        reply = next(m for m in messages if m.get('id') == 1)
        self.assertEqual(reply['result']['protocolVersion'], 1)


if __name__ == '__main__':
    unittest.main()
