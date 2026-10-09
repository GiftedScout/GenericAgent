"""On-demand short subagent tool. Read memory/subagent.md before use.

Not part of the base tool schema; call dispatch(parent, prompt) via inline_eval.
The child has a fresh history, uses the parent's current route/model/effort,
and exits after one task. This is not an orchestration or supervision mode.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent


def dispatch(parent, prompt, timeout=180):
    """Run one fresh conversation and return its output and audit paths.

    Timeout bounds the entire child conversation, not a remote background job.
    Status 'completed' means the conversation ended, NOT that its claims passed
    independent acceptance. No history or credentials are written to task files.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt must be nonempty text')
    timeout = float(timeout)
    if not 0 < timeout <= 600:
        raise ValueError('timeout must be in (0, 600]')
    backend = parent.llmclient.backend
    if getattr(backend, '_sessions', None):
        raise ValueError('Mixin routes are not supported; select a single channel first')
    model = str(getattr(backend, 'model', '') or '')
    route = str(getattr(backend, 'api_base', '') or '').rstrip('/')
    if not model or not route:
        raise ValueError('current backend has no model/route identity')
    job = ROOT / 'temp' / 'subagent_jobs' / uuid.uuid4().hex
    job.mkdir(parents=True)
    spec = dict(prompt=prompt, model=model, route=route,
                backend_type=type(backend).__name__, backend_name=getattr(backend, 'name', ''),
                effort=getattr(backend, 'reasoning_effort', None))
    (job / 'input.json').write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
    args = [sys.executable, str(Path(__file__).resolve()), '--job', str(job), '--no-user-tools']
    timed_out = False
    env = os.environ.copy()
    # Reference the configured key module directory, never copy/read its contents.
    import llmcore
    key_path = getattr(llmcore, '_mykey_path', None)
    if key_path:
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(Path(key_path).parent), env.get('PYTHONPATH', '')]))
    with (job / 'stdout.log').open('w', encoding='utf-8') as out, (job / 'stderr.log').open('w', encoding='utf-8') as err:
        proc = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        (job / 'pid.txt').write_text(str(proc.pid), encoding='utf-8')
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Also clean up on a caller interrupt/exception, never leave a child.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    result_path = job / 'result.json'
    result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {}
    result.update(status='timeout' if timed_out else ('completed' if proc.returncode == 0 and result.get('round_end') else 'error'),
                  pid=proc.pid, exit_code=proc.returncode, job_dir=str(job),
                  stderr_path=str(job / 'stderr.log'))
    return result


def _worker(job):
    from agentmain import GenericAgent
    agent = GenericAgent()
    spec = json.loads((job / 'input.json').read_text(encoding='utf-8'))
    # Route selection is exact and fails closed; an index may shift after reload.
    for index, client in enumerate(agent.llmclients):
        backend = getattr(client, 'backend', None)
        if (type(backend).__name__ == spec['backend_type'] and
                getattr(backend, 'name', '') == spec['backend_name'] and
                str(getattr(backend, 'api_base', '')).rstrip('/') == spec['route']):
            agent.next_llm(index)
            backend.model = spec['model']
            backend.reasoning_effort = spec['effort']
            break
    else:
        raise ValueError('parent route is no longer configured; refusing model fallback')
    agent.peer_hint = False
    stopped = threading.Event()

    def stop(signum, frame):
        stopped.set()
        agent.abort()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    dq = agent.put_task(spec['prompt'], source='func')
    final = None
    try:
        while not stopped.is_set():
            try:
                item = dq.get(timeout=.2)
            except queue.Empty:
                if not thread.is_alive():
                    raise RuntimeError('agent worker exited without a final result; see stderr.log')
                continue
            if 'done' in item:
                final = item['done']
                break
    finally:
        if stopped.is_set():
            agent.abort()
        agent.task_queue.put('exit')
        thread.join(timeout=5)
        client = getattr(agent, '_ssh_client', None)
        if client is not None:
            client.close()
    if final is None or thread.is_alive():
        return 2
    output = job / 'output.txt'
    output.write_text(final + '\n\n[ROUND END]\n', encoding='utf-8')
    outcome = getattr(agent.handler, '_last_exit', {}) or {}
    normal_end = outcome.get('result') == 'CURRENT_TASK_DONE'
    result = dict(round_end=normal_end, output=final, output_path=str(output),
                  log_path=agent.log_path, model=spec['model'], route=spec['route'],
                  effort=spec['effort'], outcome=outcome.get('result'),
                  error=outcome.get('error'))
    (job / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if normal_end else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=Path, required=True)
    args, _ = parser.parse_known_args()
    sys.exit(_worker(args.job))
