"""Execute the packaging syntax gate exactly as the desktop CI workflow does."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DesktopCiContractTests(unittest.TestCase):
    def test_packaging_syntax_gate_uses_existing_linux_helpers(self):
        workflow = (ROOT / '.github/workflows/desktop-ci.yml').read_text()
        block = re.search(
            r'      - name: Verify packaging helper syntax\n        run: \|\n'
            r'((?:          [^\n]*\n)+)', workflow,
        )
        self.assertIsNotNone(block, 'packaging syntax gate must remain enabled')
        script = '\n'.join(line[10:] for line in block[1].splitlines())
        for helper in (
            'packaging/scripts/linux/install_linux.sh',
            'packaging/scripts/linux/uninstall.sh',
            'release_qualification/linux/run_linux_release_qualification.sh',
            'packaging/scripts/merge_desktop_settings.py',
        ):
            self.assertIn('frontends/desktop/' + helper, script)
        with tempfile.TemporaryDirectory() as cache:
            proc = subprocess.run(
                ['bash', '-e', '-c', script], cwd=ROOT, text=True,
                capture_output=True, env=dict(os.environ, PYTHONPYCACHEPREFIX=cache),
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == '__main__':
    unittest.main()
