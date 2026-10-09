"""Thin OpenSSH execution, multiplexing and detached-job helpers.

No remote daemon, command allowlist, or private-key parsing. Each client owns
its control sockets; SSH config/agent remain the source of authentication.
"""
import atexit
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid


_TASK_ROOT = "~/.cache/ga-ssh/tasks"
_META = "__GA_SSH_META__"


def _q(value):
    return shlex.quote(str(value))


def _remote_path(value):
    # Expand only the user's home shorthand, never evaluate arbitrary input.
    value = str(value)
    if value == "~":
        return '"$HOME"'
    if value.startswith("~/"):
        return '"$HOME"/' + _q(value[2:])
    return _q(value)


def _seconds(value):
    if value is None or float(value) == 0:
        return None
    if float(value) < 0:
        raise ValueError("timeout must be nonnegative (0/null means unlimited)")
    return float(value)


class SSHClient:
    """One agent's connections. Commands share transport, NOT shell state."""

    def __init__(self, output_dir=None, idle_seconds=1800):
        self.control_dir = Path(tempfile.mkdtemp(prefix="ga-ssh-"))
        self.output_dir = Path(output_dir or self.control_dir / "outputs")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.idle_seconds = int(idle_seconds)
        if self.idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        self._connections = {}
        self._lock = threading.RLock()
        atexit.register(self.close)

    def _connection(self, host, ssh_options=None):
        if not isinstance(host, str) or not host or host.startswith("-") or "\x00" in host:
            raise ValueError("host must be an SSH alias or destination, not an option")
        options = list(ssh_options or [])
        if not all(isinstance(x, str) for x in options):
            raise ValueError("ssh_options must be a list of command-line strings")
        key = (host, tuple(options))
        with self._lock:
            self.control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if key not in self._connections:
                digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:20]
                self._connections[key] = str(self.control_dir / digest)
            path = self._connections[key]
        # Our socket/lifecycle settings take precedence over user SSH config.
        argv = ["ssh", "-S", path, "-o", "ControlMaster=auto",
                "-o", f"ControlPersist={self.idle_seconds}",
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3"] + options
        return argv, path

    def _execute(self, host, command, payload=None, timeout=60, ssh_options=None,
                 stop_signal=None, max_output=10000):
        argv, path = self._connection(host, ssh_options)
        token = uuid.uuid4().hex
        out_path = self.output_dir / (token + ".stdout")
        err_path = self.output_dir / (token + ".stderr")
        started = time.monotonic()
        reason = None
        timeout = _seconds(timeout)
        # Files avoid PIPE inheritance deadlocks and unbounded output RAM.
        with tempfile.TemporaryFile() as inp, out_path.open("wb") as out, err_path.open("wb") as err:
            inp.write((payload or "").encode("utf-8")); inp.seek(0)
            proc = subprocess.Popen(argv + ["-T", host, command], stdin=inp,
                                    stdout=out, stderr=err)
            while proc.poll() is None:
                if stop_signal and (stop_signal() if callable(stop_signal) else bool(stop_signal)):
                    reason = "stopped"
                elif timeout is not None and time.monotonic() - started >= timeout:
                    reason = "timeout"
                if reason:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.wait()
                    break
                time.sleep(0.05)
        def preview(file):
            with file.open("rb") as f:
                data = f.read(max_output + 1)
            text = data[:max_output].decode("utf-8", errors="replace")
            return text + ("\n[truncated; full output in log file]" if len(data) > max_output else "")
        result = {"status": reason or ("success" if proc.returncode == 0 else "error"),
                  "stdout": preview(out_path), "stderr": preview(err_path),
                  "exit_code": proc.returncode, "timed_out": reason == "timeout",
                  "stdout_path": str(out_path), "stderr_path": str(err_path),
                  "control_path": path, "elapsed": round(time.monotonic() - started, 3)}
        if reason or proc.returncode == 255:
            result["remote_state"] = "unknown"
            result["note"] = "SSH ended; remote termination/completion is NOT confirmed. Do not blindly retry."
        return result

    @staticmethod
    def _script(script, type="python", cwd=None, env=None, interpreter=None):
        if type not in ("bash", "python"):
            raise ValueError("type must be bash or python")
        lines = ["#!/bin/sh"]
        if cwd:
            lines.append("cd -- " + _remote_path(cwd) + " || exit $?")
        for key, value in (env or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("invalid environment variable name")
            lines.append("export " + key + "=" + _q(value))
        marker = "GA_SCRIPT_" + uuid.uuid4().hex
        executable = interpreter or ("python3" if type == "python" else "bash")
        lines.extend(["exec " + _q(executable) + (" -u -" if type == "python" else " -s") + " <<'" + marker + "'",
                      script, marker])
        return "\n".join(lines) + "\n"

    def run(self, host, script, type="python", cwd=None, timeout=60, background=False,
            interpreter=None, env=None, ssh_options=None, stop_signal=None,
            task_id=None, task_root=_TASK_ROOT, max_output=10000):
        wrapper = self._script(script, type, cwd, env, interpreter)
        if not background:
            return self._execute(host, "sh -s", wrapper, timeout, ssh_options,
                                 stop_signal, max_output)
        task_id = task_id or uuid.uuid4().hex
        self._validate_id(task_id)
        encoded = base64.b64encode(wrapper.encode()).decode()
        # One mkdir claims an ID atomically. Never resubmit an existing job.
        launch = f'''set -eu
root={_remote_path(task_root)}
mkdir -p -- "$root"
d="$root"/{_q(task_id)}
if mkdir -m 700 -- "$d" 2>/dev/null; then
  printf '%s' {_q(encoded)} | base64 -d > "$d/script.sh"
  cat > "$d/runner.sh" <<'GA_RUNNER'
#!/bin/sh
d=$1
# /proc start time disambiguates reused Linux PIDs. Other OSes fail closed.
start=$(awk '{{print $22}}' /proc/$$/stat 2>/dev/null || true)
printf '%s %s\\n' "$$" "$start" > "$d/identity.tmp"
mv "$d/identity.tmp" "$d/identity"
finish() {{
  rc=$?
  trap - EXIT
  printf '%s\\n' "$rc" > "$d/exit_code.tmp"
  mv "$d/exit_code.tmp" "$d/exit_code"
  exit "$rc"
}}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
sh "$d/script.sh"
exit $?
GA_RUNNER
  nohup setsid sh "$d/runner.sh" "$d" > "$d/output.log" 2>&1 < /dev/null &
fi
'''
        launch += self._status_script(task_id, task_root)
        result = self._execute(host, "sh -s", launch, timeout, ssh_options,
                               stop_signal, max_output)
        result.update(task_id=task_id, task_root=task_root)
        result.update(self._metadata(result["stdout"]))
        if result["status"] in ("timeout", "stopped") or result["exit_code"] == 255:
            result["note"] = "Submission uncertain; query this task_id before any retry."
        return result

    @staticmethod
    def _validate_id(task_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id or ""):
            raise ValueError("invalid task_id")

    @staticmethod
    def _metadata(text):
        result = {}
        for line in text.splitlines():
            if line.startswith(_META + "\t"):
                _, key, value = line.split("\t", 2)
                if key in ("pid", "task_exit_code"):
                    value = int(value) if value else None
                elif key in ("task_dir", "log_path"):
                    value = base64.b64decode(value).decode()
                result[key] = value
        return result

    @staticmethod
    def _status_script(task_id, task_root):
        return f'''d={_remote_path(task_root)}/{_q(task_id)}
state=missing; pid=; saved=; code=
if [ -d "$d" ]; then
  state=starting
  if [ -f "$d/identity" ]; then
    read -r pid saved < "$d/identity"
    state=lost
    current=$(awk '{{print $22}}' "/proc/$pid/stat" 2>/dev/null || true)
    procstate=$(awk '{{print $3}}' "/proc/$pid/stat" 2>/dev/null || true)
    if [ -n "$saved" ] && [ "$current" = "$saved" ] && [ "$procstate" != Z ]; then state=running; fi
  fi
  if [ -f "$d/exit_code" ]; then
    code=$(cat "$d/exit_code")
    if [ "$code" = 0 ]; then state=succeeded; else state=failed; fi
  fi
fi
printf '{_META}\\ttask_state\\t%s\\n' "$state"
printf '{_META}\\tpid\\t%s\\n' "$pid"
printf '{_META}\\ttask_exit_code\\t%s\\n' "$code"
printf '{_META}\\ttask_dir\\t%s\\n' "$(printf '%s' "$d" | base64 | tr -d '\\n')"
printf '{_META}\\tlog_path\\t%s\\n' "$(printf '%s' "$d/output.log" | base64 | tr -d '\\n')"
'''

    def task(self, host, task_id, action="status", timeout=20, tail=100, offset=None,
             ssh_options=None, stop_signal=None, task_root=_TASK_ROOT):
        self._validate_id(task_id)
        if action not in ("status", "logs", "wait", "stop"):
            raise ValueError("action must be status/logs/wait/stop")
        if action == "wait":
            limit = _seconds(timeout)
            started = time.monotonic()
            result = {"status": "success", "task_id": task_id, "task_state": "unknown"}
            while True:
                left = None if limit is None else max(0, limit - (time.monotonic() - started))
                if left is not None and left <= 0:
                    result["wait_expired"] = True
                    return result
                result = self.task(host, task_id, "status", min(15, left) if left else 15,
                                   ssh_options=ssh_options, stop_signal=stop_signal, task_root=task_root)
                if result["status"] != "success" or result.get("task_state") not in ("running", "starting"):
                    return result
                time.sleep(min(0.25, left) if left else 0.25)
        command = self._status_script(task_id, task_root)
        if action == "logs":
            command += "test -f \"$d/output.log\" || { echo 'Task log missing' >&2; exit 1; }\n"
            command += "printf '__GA_SSH_LOGS__\\n'\n"
            if offset is None:
                command += f'tail -n {max(0, int(tail))} -- "$d/output.log" | head -c 10000 | base64\n'
            else:
                # Base64 preserves byte offsets, binary data and metadata-like log lines.
                command += f'dd if="$d/output.log" bs=1 skip={max(0, int(offset))} count=10000 2>/dev/null | base64\n'
        elif action == "stop":
            # Signal the setsid process GROUP, never arbitrary or reused PIDs.
            command += f'''if [ "$state" = running ]; then
  pgid=$(ps -o pgid= -p "$pid" | tr -d ' ')
  if [ "$pgid" = "$pid" ]; then
    kill -TERM -- "-$pid" 2>/dev/null || /bin/kill -TERM -- "-$pid"
    printf '{_META}\\tstop_requested\\ttrue\\n'
  else
    echo 'Task identity/group could not be verified' >&2; exit 1
  fi
fi
'''
        result = self._execute(host, "sh -s", command, timeout, ssh_options, stop_signal,
                               16000 if action == "logs" else 10000)
        header, _, encoded_logs = result["stdout"].partition("__GA_SSH_LOGS__\n")
        metadata = self._metadata(header)
        result.update(metadata)
        result.update(task_id=task_id, task_root=task_root)
        if action == "logs":
            raw = base64.b64decode(encoded_logs)
            result["logs"] = raw.decode("utf-8", errors="replace")
            result["log_bytes"] = len(raw)
            if offset is not None:
                result["next_offset"] = max(0, int(offset)) + len(raw)
        return result

    def transfer(self, host, direction, local_path, remote_path, timeout=60,
                 ssh_options=None, stop_signal=None):
        if direction not in ("upload", "download"):
            raise ValueError("direction must be upload/download")
        argv, path = self._connection(host, ssh_options)
        # Use SFTP-mode scp (modern OpenSSH); no remote-shell quoting required.
        scp = ["scp", "-o", "ControlPath=" + path, "-o", "ControlMaster=auto",
               "-o", f"ControlPersist={self.idle_seconds}", "-o", "ConnectTimeout=15"] + list(ssh_options or [])
        remote = host + ":" + remote_path
        local = str(Path(local_path).expanduser().resolve())
        args = [local, remote] if direction == "upload" else [remote, local]
        # scp's -P (not ssh's -p) is needed if a port is explicitly provided;
        # prefer ssh_options=['-o','Port=...'] or an SSH config alias.
        deadline = _seconds(timeout)
        proc = subprocess.Popen(scp + ["--"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        started = time.monotonic()
        while True:
            try:
                out, err = proc.communicate(timeout=0.1)
                return {"status": "success" if proc.returncode == 0 else "error",
                        "exit_code": proc.returncode, "stdout": out.decode(errors="replace"),
                        "stderr": err.decode(errors="replace"), "control_path": path}
            except subprocess.TimeoutExpired:
                stopped = stop_signal and (stop_signal() if callable(stop_signal) else bool(stop_signal))
                if stopped or (deadline is not None and time.monotonic() - started >= deadline):
                    proc.kill(); out, err = proc.communicate()
                    return {"status": "stopped" if stopped else "timeout", "exit_code": proc.returncode,
                            "stderr": err.decode(errors="replace"),
                            "note": "Transfer may be partial; verify destination before retry."}

    def close(self):
        """Close only this client's sockets. Detached jobs are unaffected."""
        with self._lock:
            connections = list(self._connections.items())
            self._connections.clear()
        for (host, options), path in connections:
            try:
                subprocess.run(["ssh", "-S", path, "-O", "exit"] + list(options) + [host],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass  # ControlPersist still provides bounded idle cleanup.
        # Keep output files available after task completion; remove only sockets.
        for entry in self.control_dir.glob("*"):
            if entry.is_socket():
                try: entry.unlink()
                except OSError: pass
        try: self.control_dir.rmdir()
        except OSError: pass
