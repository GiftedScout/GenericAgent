# -*- coding: utf-8 -*-
"""Regression: anchored inline-CoT splitter (_visible_content).

Root cause this guards: when a llama.cpp server runs with reasoning_format=none,
the raw thinking tags ride inside content deltas.  The old splitter flipped
state on ANY literal tag occurrence, so a tag the model merely *quoted* (in
backticks inside its thinking, or inline in the answer) would:
  - leak the thinking tail into the visible stream (10711-char flood), or
  - swallow the tail of the answer into "thinking" (report amputation at 19:00).

Fix: a tag only counts as a boundary when it sits on a clean edge — stream
start, or right after a newline.  A tag anywhere else is literal text.

NOTE: the tag literals are built by concatenation below on purpose.  Writing a
complete tag as a source literal gets its line split by the render layer, which
is exactly the failure class under test.
"""
import json
import sys

sys.path.insert(0, '/home/pushuai/GenericAgent')
import llmcore

# tag literals, assembled at runtime (never written contiguously in source)
O  = "<" + "think" + ">"        # short open
C  = "</" + "think" + ">"       # short close
O2 = "<" + "thinking" + ">"     # long open
C2 = "</" + "thinking" + ">"    # long close

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS {name}")
    else: FAIL += 1; print(f"  FAIL {name}  {detail}")


def sse(content):
    return 'data: ' + json.dumps(
        {"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)


def run(deltas, omit=True):
    lines = [sse(d) for d in deltas] + ['data: [DONE]']
    gen = llmcore._parse_openai_sse(iter(lines), api_mode="chat_completions",
                                    omit_thinking=omit)
    yielded = []
    try:
        while True:
            yielded.append(next(gen))
    except StopIteration as e:
        blocks = e.value
    return "".join(yielded), blocks


def visible(blocks):
    return "".join(b["text"] for b in blocks if b.get("type") == "text")


print("[1] genuine stream splits (open at stream start, close on its own line)")
y, b = run([O + "\nstep one.", " then step two.\n" + C + "\n\nAnswer: 42"])
v = visible(b)
check("answer kept", "Answer: 42" in v, v)
check("thinking stripped", "step one" not in v and "step two" not in v, v)
check("no literal tags in answer", O not in v and C not in v, v)

print("[2] open tag quoted in backticks in the ANSWER must not amputate (19:00 bug)")
y, b = run([O + "\nprivate reasoning\n" + C,
            "\nAnswer: see `" + O + "` inline, more follows."])
v = visible(b)
check("tail of answer kept", "more follows" in v, v)
check("quoted open tag stays literal", O in v, v)
check("private reasoning stripped", "private reasoning" not in v, v)

print("[3] close tag quoted in backticks in THINKING must not leak (10711 bug)")
y, b = run([O + "\nmodel quotes `" + C + "` while reasoning. still inside.\n" + C + "\nFinal: done"])
v = visible(b)
check("final answer kept", "Final: done" in v, v)
check("thinking body stripped", "still inside" not in v, v)
check("quoted close not leaked as answer", "model quotes" not in v, v)

print("[4] lone close tag at stream start (upstream noise) is dropped")
y, b = run([C + "\nReal answer here"])
v = visible(b)
check("answer kept", "Real answer here" in v, v)
check("noise close dropped", C not in v, v)

print("[5] omit_thinking=False passes raw stream through (regression guard)")
raw = O + "\nthinking body\n" + C + "\nAnswer: x"
y, b = run([raw], omit=False)
v = visible(b)
check("raw stream intact", v == raw, repr(v))

print("[6] genuine tag split across SSE frames still splits")
y, b = run([O + "\nthinking text", "\n" + C[:4], C[4:] + "\nAnswer: ok"])
v = visible(b)
check("answer kept", "Answer: ok" in v, v)
check("thinking stripped", "thinking text" not in v, v)

print("[7] empty thinking block (open immediately followed by close)")
y, b = run([O + C + "\nAnswer: y"])
v = visible(b)
check("answer kept", "Answer: y" in v, v)
check("no tag leaked", O not in v and C not in v, v)

print("[8] frame-safe escape: quoted OPEN tag split across SSE frames")
# Servers emit a few chars per frame, so a tag the model merely quotes inside
# its CoT is split mid-tag ("... `<think" + "ing>` ...").  Escaping each frame
# on its own lets the halves re-assemble in the display stream, and the TUI's
# non-greedy envelope regex then closes the CoT block early (CoT tail floods the
# terminal).  The escaper must hold the partial prefix back until the tag is
# complete, then escape it whole.
y, b = run([O2 + "\nquote `" + O2[:6], O2[6:] + "`\n" + C2 + "\n\nA"])
v_ = "".join(y)
check("split quoted open tag escaped whole", "＜thinking＞" in v_, repr(v_))
check("no half open tag left", O2[:6] not in v_.replace(O2, ""), repr(v_))
check("answer kept", v_.rstrip().endswith("A"), repr(v_[-30:]))

print("[9] frame-safe escape: quoted CLOSE tag split across SSE frames")
# Same hazard for a quoted close tag: if the halves re-assemble un-escaped the
# TUI pair regex closes the envelope at the quote, dumping the CoT tail.
y, b = run([O2 + "\nthe close tag is `" + C2[:5], C2[5:] + "`\n" + C2 + "\n\nAnswer: z"])
v_ = "".join(y)
after = v_[v_.rfind(C2) + len(C2):]
check("answer after envelope", "Answer: z" in after, repr(after))
check("CoT not dumped after envelope", "close tag is" not in after, repr(after))
check("split quoted close tag escaped", "＜/thinking＞" in v_, repr(v_))

print("[10] escaper flushes a dangling partial tag verbatim (deliberate)")
# A stream that dies mid-tag leaves a prefix in the escaper's tail.  We flush it
# verbatim on purpose: escaping a lone "<" would corrupt legitimate text such as
# "a < b".  The guard is that no *complete* tag is fabricated and the envelope
# still pairs, so a truncated CoT can never leak as content.
y, b = run([O2 + "\nbody\n" + C2[:5]])
v_ = "".join(y)
check("no fabricated complete close tag", v_.count(C2) == 1, repr(v_))
check("no fabricated complete open tag", v_.count(O2) == 1, repr(v_))

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
