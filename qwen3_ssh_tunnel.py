"""On-demand SSH tunnel lifecycle for the qwen3.8-27B local endpoint."""
import atexit
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_HOST = "ga-qwen3-27b"
_PORT = 18080
_IDLE_SECONDS = 30 * 60
_ROOT = Path(__file__).resolve().parent
_ASKPASS = _ROOT / "scripts" / "qwen3_ssh_askpass.py"
_lock = threading.RLock()
_proc = None
_idle_timer = None


def _healthy(timeout=1.5):
    try:
        with socket.create_connection(("127.0.0.1", _PORT), timeout=timeout) as conn:
            conn.sendall(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return b" 200 " in conn.recv(128)
    except OSError:
        return False


def ensure_qwen3_tunnel():
    """Return after a healthy endpoint exists; never takes ownership of another tunnel."""
    global _proc, _idle_timer
    with _lock:
        if _idle_timer:
            _idle_timer.cancel(); _idle_timer = None
        if _healthy(): return
        if _proc and _proc.poll() is None:
            _proc.terminate()
            try: _proc.wait(timeout=5)
            except subprocess.TimeoutExpired: _proc.kill(); _proc.wait()
        env = os.environ.copy()
        env.update(SSH_ASKPASS=str(_ASKPASS), SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY", ":0"))
        _proc = subprocess.Popen(
            ["ssh", "-N", "-T", "-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1",
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
             "-o", "ServerAliveCountMax=3", _HOST],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if _healthy(): return
            if _proc.poll() is not None:
                error = (_proc.stderr.read() or "SSH tunnel exited").strip()
                _proc = None
                raise RuntimeError(f"qwen3.8-27B SSH tunnel failed: {error}")
            time.sleep(0.2)
        _proc.terminate(); _proc.wait(timeout=5); _proc = None
        raise RuntimeError("qwen3.8-27B SSH tunnel did not become healthy within 20 seconds")


def release_qwen3_tunnel():
    """Keep a GA-created tunnel for the configured idle interval, then close it."""
    global _idle_timer
    with _lock:
        if _proc is None or _proc.poll() is not None: return
        if _idle_timer: _idle_timer.cancel()
        _idle_timer = threading.Timer(_IDLE_SECONDS, close_qwen3_tunnel)
        _idle_timer.daemon = True
        _idle_timer.start()


def close_qwen3_tunnel():
    global _proc, _idle_timer
    with _lock:
        if _idle_timer: _idle_timer.cancel(); _idle_timer = None
        if _proc and _proc.poll() is None:
            _proc.terminate()
            try: _proc.wait(timeout=5)
            except subprocess.TimeoutExpired: _proc.kill(); _proc.wait()
        _proc = None


atexit.register(close_qwen3_tunnel)
