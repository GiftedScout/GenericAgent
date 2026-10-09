import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import subagent_tool as tool


class DispatchTests(unittest.TestCase):
    def parent(self):
        backend = SimpleNamespace(model='runtime-model', api_base='https://route.test/v1/',
                                  name='channel', reasoning_effort='high')
        return SimpleNamespace(llmclient=SimpleNamespace(backend=backend))

    def launch(self, root, program, **kwargs):
        real = tool.subprocess.Popen
        def substitute(args, **opts):
            return real([sys.executable, '-c', program, args[3]], **opts)
        with patch.object(tool, 'ROOT', root), patch.object(tool.subprocess, 'Popen', side_effect=substitute):
            return tool.dispatch(self.parent(), 'small task', **kwargs)

    def test_real_child_output_and_route_spec(self):
        program = """import json,sys
from pathlib import Path
j=Path(sys.argv[1]);s=json.loads((j/'input.json').read_text())
assert s['model']=='runtime-model' and s['route']=='https://route.test/v1'
assert s['effort']=='high' and 'history' not in s and 'api_key' not in s
(j/'result.json').write_text(json.dumps({'round_end':True,'output':'result'}))
"""
        with tempfile.TemporaryDirectory() as tmp:
            r = self.launch(Path(tmp), program)
            self.assertEqual(r['status'], 'completed')
            self.assertEqual(r['output'], 'result')
            self.assertFalse((Path('/proc') / str(r['pid'])).exists())

    def test_timeout_reaps_real_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.launch(Path(tmp), 'import time; time.sleep(30)', timeout=.15)
            self.assertEqual(r['status'], 'timeout')
            self.assertFalse((Path('/proc') / str(r['pid'])).exists())

    def test_child_failure_is_not_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.launch(Path(tmp), 'raise RuntimeError("child failed")')
            self.assertEqual(r['status'], 'error')
            self.assertIn('child failed', Path(r['stderr_path']).read_text())

    def test_validation(self):
        with self.assertRaises(ValueError): tool.dispatch(self.parent(), '')
        with self.assertRaises(ValueError): tool.dispatch(self.parent(), 'task', timeout=0)
        p = self.parent(); p.llmclient.backend._sessions = [object()]
        with self.assertRaisesRegex(ValueError, 'Mixin'): tool.dispatch(p, 'task')


if __name__ == '__main__':
    unittest.main()
