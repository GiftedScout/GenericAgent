"""Wayland safety boundary: never inject an uncalibrated click or mistake X11 for native windows."""
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WaylandPointerSafetyTests(unittest.TestCase):
    def test_uncalibrated_pointer_and_native_window_are_rejected(self):
        script = """
import sys
sys.path.insert(0, %r)
import ljqCtrl as c
assert c._wayland and (c.swidth, c.sheight) == (0, 0)
try:
    c.SetCursorPos((100, 100))
except ValueError as exc:
    assert 'Portal' in str(exc)
else:
    raise AssertionError('pointer moved before geometry was established')
try:
    c.GrabWindow('native window')
except NotImplementedError as exc:
    assert 'Wayland' in str(exc)
else:
    raise AssertionError('native Wayland screenshot was confused with X11')
""" % str(ROOT / "memory")
        env = dict(os.environ, XDG_SESSION_TYPE="wayland")
        proc = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True,
            text=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
