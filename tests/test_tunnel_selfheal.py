# -*- coding: utf-8 -*-
"""A: tunnel-death self-heal regression.

Bug fixed: _stream_with_retry swallows ALL exceptions into yielded error
strings, so raw_ask's `except Connection refused: rebuild tunnel` was DEAD
code. Worse, its trigger was too narrow (only 'Connection refused', not
mid-stream reset/aborted/RemoteDisconnected).

Fix: _stream_with_retry rebuilds the SSH tunnel (when sess.ssh_tunnel is set)
before retrying on tunnel-death signatures.

Tests (no real ssh, no real network):
1. _tunnel_dead_err matches tunnel-death signatures, not app-level errors
2. 2x ConnectionError then success -> tunnel rebuilt twice, stream succeeds
3. no ssh_tunnel -> no rebuild attempt on connection error
"""
import sys, types
sys.path.insert(0, '/home/pushuai/GenericAgent')
import llmcore
import requests as _req

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name} {extra}")

print("[1] _tunnel_dead_err signature matching")
check("refused matches", llmcore._tunnel_dead_err(_req.ConnectionError("HTTPConnectionPool: Connection refused")))
check("reset matches", llmcore._tunnel_dead_err(_req.exceptions.ConnectionError("Connection reset by peer")))
check("aborted matches", llmcore._tunnel_dead_err(Exception("ProtocolError: Connection aborted.")))
check("remote-end-closed matches", llmcore._tunnel_dead_err(Exception("Remote end closed connection without response")))
check("broken pipe matches", llmcore._tunnel_dead_err(Exception("Broken pipe")))
check("404 does NOT match", not llmcore._tunnel_dead_err(Exception("HTTP 404 Not Found")))
check("timeout does NOT match", not llmcore._tunnel_dead_err(_req.Timeout("read timed out")))

def run_with_fails(n_fail, tunnel_name, err=_req.ConnectionError("Max retries exceeded with url: /v1/chat/completions (Caused by NewConnectionError(Connection refused))")):
    rebuilt = []
    calls = {'post': 0}
    real_rebuild = llmcore._rebuild_ssh_tunnel
    llmcore._rebuild_ssh_tunnel = lambda s: rebuilt.append(1)
    class FakeResp:
        status_code = 200; headers = {}
    class FakePost:
        def __init__(self, *a, **k): pass
        def __enter__(self):
            calls['post'] += 1
            if calls['post'] <= n_fail: raise err
            return FakeResp()
        def __exit__(self, *a): return False
    real_post = _req.post
    _req.post = FakePost
    try:
        def parse_fn(r):
            yield "ok"
            return [{"type": "text", "text": "ok"}]
        sess = types.SimpleNamespace(name="t", max_retries=4, stream=True,
                                     connect_timeout=5, read_timeout=5,
                                     proxies=None, verify=True, max_retry_after=60.0,
                                     ssh_tunnel=tunnel_name)
        out = []
        val = None
        g = llmcore._stream_with_retry(sess, "http://127.0.0.1:1/v1", {}, {}, parse_fn)
        while True:
            try: out.append(next(g))
            except StopIteration as e: val = e.value; break
        return out, val, rebuilt, calls
    finally:
        _req.post = real_post
        llmcore._rebuild_ssh_tunnel = real_rebuild

print("\n[2] 2x tunnel-death then success -> rebuild before each retry, stream recovers")
out, val, rebuilt, calls = run_with_fails(2, "qwen3-27b")
check("stream recovered (no error in output)", out == ["ok"], out)
check("final blocks ok", val == [{"type": "text", "text": "ok"}], val)
check("tunnel rebuilt once per failed attempt", len(rebuilt) == 2, f"rebuilt={rebuilt}")
check("3 total posts (2 fail + 1 ok)", calls['post'] == 3, calls)

print("\n[3] no ssh_tunnel -> retry without rebuild (existing behavior intact)")
out, val, rebuilt, calls = run_with_fails(1, None)
check("stream recovered", out == ["ok"], out)
check("no rebuild attempted", rebuilt == [], rebuilt)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
