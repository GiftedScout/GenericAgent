# -*- coding: utf-8 -*-
"""Regression tests for the 4 fixes (F1/F3/F4) + F2 filter:
F1 responses-mode thinking now streams with a display envelope (matches chat_completions)
F3 _to_responses_input converts claude-style image blocks to input_image
F4 trim_messages_history leaves history byte-stable under cap (prefix-cache protection)
F2 NativeToolClient.chat keeps image blocks (does not drop non-text blocks)
"""
import sys, os, json, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  \u2705 {name}")
    else: FAIL += 1; print(f"  \u274c {name} {detail}")

import py_compile
for f in ('llmcore.py', 'agentmain.py', 'ga.py'):
    py_compile.compile(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), f), doraise=True)
print("\u2705 syntax OK (llmcore.py, agentmain.py, ga.py)")

from llmcore import _parse_openai_sse, _to_responses_input, trim_messages_history

def drain_ret(parser, *a, **k):
    g = parser(*a, **k); out = []
    try:
        while True: out.append(next(g))
    except StopIteration as e:
        return out, e.value
    return out, None

def sse(pairs):
    return [f"data: {p}\n".encode() for p in pairs]

# ---------- F1: responses-mode thinking envelope ----------
print("\n[F1] responses-mode SSE: thinking now streams with envelope")
OPEN, CLOSE = "\n<thinking>\n", "\n</thinking>\n"
lines = sse([
    '{"type":"response.reasoning_text.delta","delta":"Let me reason."}',
    '{"type":"response.reasoning_text.delta","delta":" Then conclude."}',
    '{"type":"response.output_text.delta","delta":"Here is the answer."}',
    '{"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1,"cached_input_tokens":0}}}',
])
out, blocks = drain_ret(_parse_openai_sse, lines, api_mode="responses")
stream = "".join(out)
check("F1 stream has thinking open", OPEN in stream, repr(stream[:120]))
check("F1 stream has thinking close", CLOSE in stream)
check("F1 open before reasoning", stream.index(OPEN) < stream.index("Let me reason."))
check("F1 close before answer", stream.index(CLOSE) < stream.index("Here is the answer."))
check("F1 reasoning intact", "Let me reason. Then conclude." in stream)
check("F1 answer intact", "Here is the answer." in stream)
check("F1 exactly one envelope", stream.count(OPEN) == 1 and stream.count(CLOSE) == 1)
think_blocks = [b for b in (blocks or []) if b.get("type") == "thinking"]
text_blocks = [b for b in (blocks or []) if b.get("type") == "text"]
check("F1 thinking block present (not omit)", think_blocks, repr(blocks))
check("F1 thinking block clean (no tags)", think_blocks and "<thinking>" not in think_blocks[0].get("thinking", ""))
check("F1 text block present", text_blocks and text_blocks[0]["text"] == "Here is the answer.")

# F1b: omit_thinking=True -> still streams visibly, but no thinking block in history
lines_omit = sse([
    '{"type":"response.reasoning_text.delta","delta":"secret thinking"}',
    '{"type":"response.output_text.delta","delta":"answer"}',
    '{"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1,"cached_input_tokens":0}}}',
])
out2, blocks2 = drain_ret(_parse_openai_sse, lines_omit, api_mode="responses", omit_thinking=True)
stream2 = "".join(out2)
check("F1b omit: reasoning still visible in stream", "secret thinking" in stream2)
check("F1b omit: no thinking block in history", not [b for b in (blocks2 or []) if b.get("type") == "thinking"])
check("F1b omit: answer still present", "answer" in stream2)

# ---------- F3: claude-style image block -> input_image ----------
print("\n[F3] _to_responses_input: claude image block -> input_image")
img_b64 = base64.b64encode(b"\x89PNG fake").decode()
msgs = [{"role": "user", "content": [
    {"type": "text", "text": "what is in this image?"},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
]}]
conv = _to_responses_input(msgs)
user = conv[0]
imgs = [p for p in user["content"] if p.get("type") == "input_image"]
check("F3 one input_image produced", len(imgs) == 1, repr(conv))
check("F3 data: URL with base64", imgs and imgs[0]["image_url"].startswith("data:image/png;base64,"))
check("F3 base64 payload intact", imgs and imgs[0]["image_url"].endswith(img_b64))
check("F3 text part preserved", any(p.get("text") == "what is in this image?" for p in user["content"]))

# F3b: url-source image
msgs_url = [{"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}]
conv_url = _to_responses_input(msgs_url)
imgs_url = [p for p in conv_url[0]["content"] if p.get("type") == "input_image"]
check("F3b url-source image -> input_image", imgs_url and imgs_url[0]["image_url"] == "https://x/y.png")

# ---------- F4: trim cache-stable under cap ----------
print("\n[F4] trim_messages_history: byte-stable under cap")
class _Sess:
    context_win = 1000
    trim_keep_rate = 0.6
    trim_keep_prefix = 1
    cut_msg_interval = 7
def cost(ms): return sum(len(json.dumps(m, ensure_ascii=False)) for m in ms)

small = [{"role": "user", "content": [{"type": "text", "text": "hello"}]},
         {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
before = json.dumps(small, ensure_ascii=False)
trim_messages_history(small, _Sess())
after = json.dumps(small, ensure_ascii=False)
check("F4 under-cap: history unchanged (cache prefix stable)", before == after, f"before={len(before)} after={len(after)}")

# F4b: over cap -> shrinks
big = [{"role": "user", "content": [{"type": "text", "text": "x" * 2000}]},
       {"role": "assistant", "content": [{"type": "text", "text": "y" * 2000}]}] * 8
c0 = cost(big)
trim_messages_history(big, _Sess())
c1 = cost(big)
check("F4 over-cap: history shrinks", c1 < c0, f"c0={c0} c1={c1}")

# ---------- F2: NativeToolClient.chat keeps image blocks ----------
print("\n[F2] NativeToolClient.chat filter keeps image blocks")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'llmcore.py'), encoding='utf-8').read()
# locate the filter line and assert it no longer drops non-text blocks
import re
m = re.search(r"filtered_content = \[c for c in combined_content if ([^\]]+)\]", src)
check("F2 filter expression present", m is not None)
if m:
    expr = m.group(1)
    check("F2 filter keeps non-text blocks", 'c.get("type") != "text"' in expr and 'str(c.get("text", ""))' in expr, expr)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
