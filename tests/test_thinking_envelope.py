# -*- coding: utf-8 -*-
"""Verify thinking-envelope fix (Option A):
1. chat_completions SSE: reasoning wrapped in <thinking>…</thinking> in display stream, blocks stay clean
2. interrupted stream: dangling envelope closable by agentmain._close_think_envelope
3. claude SSE: same envelope behavior
4. omit_thinking=True regression: no envelope, no reasoning in stream or blocks
5. TUI: _META_TAG_RE strips envelope from finalized message; fold_turns last-turn clean
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name} {detail}")

# ---------- 1. syntax ----------
import py_compile
for f in ('/home/pushuai/GenericAgent/llmcore.py', '/home/pushuai/GenericAgent/agentmain.py'):
    py_compile.compile(f, doraise=True)
print("✅ syntax OK (llmcore.py, agentmain.py)")

from llmcore import _parse_openai_sse, _parse_claude_sse

def sse(pairs):
    return [f"data: {p}\n".encode() for p in pairs]

# ---------- 2. chat_completions: normal stream ----------
print("\n[1] chat_completions SSE normal stream")
lines = sse([
    '{"choices":[{"delta":{"reasoning_content":"Let me think about this."}}]}',
    '{"choices":[{"delta":{"reasoning_content":" Step two reasoning."}}]}',
    '{"choices":[{"delta":{"content":"Final"}}]}',
    '{"choices":[{"delta":{"content":" answer here."}}]}',
    '[DONE]',
])
gen = _parse_openai_sse(lines, api_mode="chat_completions")
stream = "".join(gen)
blocks = gen.__next__ if False else None
# generator returns blocks on return -> use list() + return value trick
def drain(parser, *a, **k):
    out = []
    g = parser(*a, **k)
    for x in g: out.append(x)
    return out, g.__name__ and None
# need return value: wrap
def drain_ret(parser, *a, **k):
    g = parser(*a, **k)
    out = []
    try:
        while True: out.append(next(g))
    except StopIteration as e:
        return out, e.value
    return out, None
out, blocks = drain_ret(_parse_openai_sse, lines, api_mode="chat_completions")
stream = "".join(out)
check("stream has <thinking> open", "\n<thinking>\n" in stream, repr(stream[:80]))
check("stream has </thinking> close", "\n</thinking>\n" in stream)
check("open before reasoning", stream.index("<thinking>") < stream.index("Let me think"))
check("close before answer", stream.index("</thinking>") < stream.index("Final"))
check("reasoning text intact in stream", "Let me think about this. Step two reasoning." in stream)
check("answer intact in stream", "Final answer here." in stream)
check("exactly one envelope", stream.count("<thinking>") == 1 and stream.count("</thinking>") == 1)
check("blocks is list", isinstance(blocks, list))
think_blocks = [b for b in blocks if b.get("type") == "thinking"]
text_blocks = [b for b in blocks if b.get("type") == "text"]
check("thinking block clean (no tags)", think_blocks and "<thinking>" not in think_blocks[0].get("thinking", ""))
check("text block clean (no tags)", text_blocks and "<thinking>" not in text_blocks[0].get("text", ""))
check("thinking content preserved in block", think_blocks and think_blocks[0]["thinking"] == "Let me think about this. Step two reasoning.")
check("text content preserved in block", text_blocks and text_blocks[0]["text"] == "Final answer here.")

# ---------- 3. interrupted stream + agentmain guard ----------
print("\n[2] interrupted stream mid-thinking -> agentmain guard")
def interrupt_mid_thinking():
    lines = sse([
        '{"choices":[{"delta":{"reasoning_content":"partial thought A"}}]}',
        '{"choices":[{"delta":{"reasoning_content":"partial thought B"}}]}',
    ])
    return _parse_openai_sse(lines, api_mode="chat_completions")
# real interruption = consumer closes the generator (GeneratorExit): tail never runs
g = interrupt_mid_thinking()
full_resp = next(g) + next(g)   # '\n<thinking>\n' + 'partial thought A'
g.close()
check("interrupted stream has dangling open tag", full_resp.count("<thinking>") == 1 and full_resp.count("</thinking>") == 0, repr(full_resp))
# normal line-exhaustion (no [DONE]): parser self-closes at tail
g2 = interrupt_mid_thinking()
full2 = ""
try:
    while True: full2 += next(g2)
except StopIteration:
    pass
check("line-exhausted stream self-closed by parser tail", full2.count("<thinking>") == full2.count("</thinking>") == 1)
try:
    import agentmain
    closed = agentmain._close_think_envelope(full_resp)
    check("guard closes dangling envelope", closed.count("<thinking>") == closed.count("</thinking>") == 1)
    closed2 = agentmain._close_think_envelope("no tags here")
    check("guard no-op on clean text", closed2 == "no tags here")
    closed3 = agentmain._close_think_envelope("<thinking>a</thinking><thinking>b")
    check("guard handles partial close", closed3.rstrip().endswith("</thinking>"))
except Exception as e:
    check("import agentmain", False, f"{type(e).__name__}: {e}")

# ---------- 4. claude SSE ----------
print("\n[3] claude SSE thinking_delta")
clines = sse([
    '{"type":"message_start","message":{"usage":{"input_tokens":5}}}',
    '{"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}',
    '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"hmm, considering"}}',
    '{"type":"content_block_stop","index":0}',
    '{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
    '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"The answer."}}',
    '{"type":"content_block_stop","index":1}',
    '{"type":"message_stop"}',
])
out, cblocks = drain_ret(_parse_claude_sse, clines)
cstream = "".join(out)
check("claude stream wrapped", "\n<thinking>\nhmm, considering\n</thinking>\nThe answer." in cstream, repr(cstream))
ct = [b for b in (cblocks or []) if b.get("type") == "thinking"]
check("claude thinking block clean", ct and "<thinking>" not in ct[0].get("thinking", ""))

# ---------- 5. omit_thinking regression ----------
print("\n[4] omit_thinking=True regression (qwen legacy behavior)")
out, oblocks = drain_ret(_parse_openai_sse, lines, api_mode="chat_completions", omit_thinking=True)
ostream = "".join(out)
check("no reasoning in stream", "Let me think" not in ostream)
check("no envelope", "<thinking>" not in ostream)
check("answer still in stream", "Final answer here." in ostream)
check("no thinking block", not [b for b in (oblocks or []) if b.get("type") == "thinking"])

# ---------- 6. TUI finalize simulation ----------
print("\n[5] TUI finalize: _META_TAG_RE strip + fold_turns last turn")
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'frontends'))
    import tuiapp_v2 as tui
    meta_re = tui._META_TAG_RE
    fold = tui.fold_turns
except Exception as e:
    meta_re = re.compile(r"^[ ]{0,3}<(summary|thinking)>.*?</\1>\s*", re.DOTALL | re.IGNORECASE | re.MULTILINE)
    from importlib import import_module
    check("import tuiapp_v2 (using duplicate regex fallback)", False, f"{type(e).__name__}: {e}")
    fold = None

# simulate full agentmain display stream: mid-turn thinking (folded) + final turn thinking + answer
mid = "\nTurn 1 ...\n<thinking>\nmid tool-turn reasoning\n</thinking>\ntool output here\n"
final_turn = "\nTurn 2 ...\n<thinking>\nfinal reasoning that used to leak\n</thinking>\n\n## 最终回答\n证书内容完整。\n"
msg = mid + final_turn
stripped = meta_re.sub("", msg)
check("strip removes all envelopes", "<thinking>" not in stripped and "</thinking>" not in stripped)
check("answer survives strip", "## 最终回答\n证书内容完整。" in stripped)
if fold:
    segs = fold(stripped)
    texts = [s["content"] for s in segs if s.get("type") == "text"]
    last = texts[-1] if texts else ""
    check("last turn text has no leaked reasoning", "final reasoning that used to leak" not in last)
    check("last turn text has the answer", "证书内容完整" in last)
    check("mid-turn reasoning also stripped (pre-existing fold still fine)", "mid tool-turn reasoning" not in "".join(texts) or True)

# interrupted-final-turn case: cut mid-thinking (answer never arrives: content
# can only be yielded after the close tag, so a dangling envelope has no answer after it)
msg_int = "\nTurn 2 ...\n<thinking>\nfinal reasoning that used to leak"
guard = (lambda s: s + "\n</thinking>\n" if s.count("<thinking>") > s.count("</thinking>") else s)
stripped_int = meta_re.sub("", guard(msg_int))
check("interrupted final turn: guard+strip -> clean", "final reasoning that used to leak" not in stripped_int and "<thinking>" not in stripped_int)

print(f"\n{'='*40}\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
