"""Generic on-demand SSH tunnel lifecycle for remote local-model endpoints.

Each registered tunnel name maps to an ssh-config Host entry that already
carries the LocalForward rule; the tunnel process itself is spawned with
``ssh -N`` against that Host. GA-owned tunnels stay up for a configurable
idle interval after the last call, and never take ownership of a healthy
endpoint that something else (e.g. a manual ssh) already provides.
"""
import atexit
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_ASKPASS = _ROOT / "scripts" / "qwen3_ssh_askpass.py"
_IDLE_SECONDS = 30 * 60
_STARTUP_TIMEOUT = 20

# tunnel name -> ssh-config Host + locally forwarded port
TUNNELS = {
    "qwen3-27b": ("ga-qwen3-27b", 18080),
    # local 28081 (28080 is taken by VS Code locally); remote listens on 28080
    "ornith1.5-35B-A3B": ("ga-ornith1.5", 28081),
}

_lock = threading.RLock()
_procs = {}
_idle_timers = {}


def _healthy(port, timeout=1.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
            conn.sendall(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return b" 200 " in conn.recv(128)
    except OSError:
        return False


def _require(name):
    if name not in TUNNELS:
        raise ValueError(f"Unknown ssh tunnel {name!r}; registered: {sorted(TUNNELS)}")
    return TUNNELS[name]


def _terminate(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def ensure_tunnel(name, idle_seconds=_IDLE_SECONDS):
    """Return once the endpoint for ``name`` is healthy locally.

    Never takes ownership of an already-healthy port; otherwise (re)starts
    the ssh -N process and waits up to 20s for health.
    """
    host, port = _require(name)
    with _lock:
        timer = _idle_timers.pop(name, None)
        if timer:
            timer.cancel()
        if _healthy(port):
            return
        _terminate(_procs.get(name))
        env = os.environ.copy()
        env.update(SSH_ASKPASS=str(_ASKPASS), SSH_ASKPASS_REQUIRE="force",
                   DISPLAY=env.get("DISPLAY", ":0"))
        proc = subprocess.Popen(
            ["ssh", "-N", "-T", "-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1",
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
             "-o", "ServerAliveCountMax=3", host],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
        _procs[name] = proc
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if _healthy(port):
                return
            if proc.poll() is not None:
                error = (proc.stderr.read() or "SSH tunnel exited").strip()
                _procs.pop(name, None)
                raise RuntimeError(f"{name} SSH tunnel failed: {error}")
            time.sleep(0.2)
        _terminate(proc)
        _procs.pop(name, None)
        raise RuntimeError(f"{name} SSH tunnel did not become healthy within {_STARTUP_TIMEOUT} seconds")


def release_tunnel(name, idle_seconds=_IDLE_SECONDS):
    """Keep a GA-owned tunnel for the idle interval, then close it."""
    host, port = _require(name)
    with _lock:
        proc = _procs.get(name)
        if proc is None or proc.poll() is not None:
            return
        old = _idle_timers.pop(name, None)
        if old:
            old.cancel()
        timer = threading.Timer(idle_seconds, close_tunnel, args=(name,))
        timer.daemon = True
        _idle_timers[name] = timer
        timer.start()


def close_tunnel(name=None):
    with _lock:
        names = [name] if name else list(_procs)
        for n in names:
            timer = _idle_timers.pop(n, None)
            if timer:
                timer.cancel()
            _terminate(_procs.pop(n, None))


def _atexit_close():
    for name in list(_procs):
        close_tunnel(name)


atexit.register(_atexit_close)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <tunnel-name>  [{', '.join(TUNNELS)}]")
    ensure_tunnel(sys.argv[1])
    print(f"{sys.argv[1]}: healthy on {_require(sys.argv[1])[1]}")
