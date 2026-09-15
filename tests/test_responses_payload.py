# -*- coding: utf-8 -*-
"""Regression: responses-mode payload must (a) pass reasoning_effort through
verbatim (ultra included), (b) pass user-configured temperature (was dropped,
so mykey temperature silently no-op'd for every responses endpoint).

Verification method: monkeypatch llmcore.requests.post to capture the payload
and return a canned SSE stream; drive a real NativeOAISession.raw_ask. No
network, no credentials needed.
"""
import io
import sys
import types

sys.path.insert(0, '/home/pushuai/GenericAgent')
import llmcore

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name}  {detail}")


def fake_sse():
    lines = [
        'data: {"type":"response.output_text.delta","delta":"hi"}',
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1}}}',
        'data: [DONE]', '',
    ]
    class R:
        status_code = 200
        def iter_lines(self):
            for l in lines: yield l
        def json(self): return {}
    return R()


captured = {}
def fake_post(url, json=None, headers=None, stream=False, timeout=None, **kw):
    captured['url'] = url
    captured['payload'] = json
    return fake_sse()


llmcore.requests.post = fake_post

cfg = {
    'apikey': 'sk-test', 'apibase': 'https://api.example.com/v1',
    'model': 'test-model', 'api_mode': 'responses',
    'reasoning_effort': 'ultra', 'temperature': 0.5, 'max_tokens': 1234,
    'system': 'S', 'stream': True, 'max_retries': 0,
    '_mykey_name': 'test_cfg',
}
sess = llmcore.NativeOAISession(cfg)
out = list(sess.raw_ask([{'role': 'user', 'content': 'x'}]))
p = captured['payload']

print("[1] ultra passthrough (whitelist must not drop it)")
check("session kept ultra", sess.reasoning_effort == 'ultra', sess.reasoning_effort)
check("payload reasoning.effort == ultra", p.get('reasoning', {}).get('effort') == 'ultra', p.get('reasoning'))

print("[2] temperature alignment (was dropped for responses mode)")
check("payload has temperature 0.5", p.get('temperature') == 0.5, p.get('temperature'))

print("[3] default temperature(1) omitted (keeps payload identical for most configs)")
cfg2 = dict(cfg); cfg2['temperature'] = 1
sess2 = llmcore.NativeOAISession(cfg2)
captured.clear()
list(sess2.raw_ask([{'role': 'user', 'content': 'x'}]))
check("temperature absent when ==1", 'temperature' not in captured['payload'], captured['payload'].get('temperature'))

print("[4] max_output_tokens still mapped")
check("max_output_tokens 1234", p.get('max_output_tokens') == 1234, p.get('max_output_tokens'))

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
