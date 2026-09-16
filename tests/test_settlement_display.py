# -*- coding: utf-8 -*-
"""Regression: settlement (background memory-maintenance) text must not reach
the display channel.

Drives the REAL agentmain.iter_display_events (the exact code path run() uses),
with chunk streams reproducing agent_runner_loop's true ordering:

  turn N:   {'turn':N} -> header -> ANSWER text -> tool markers
            -> {'settlement': True, 'turn': N}      (tool that triggered it)
  turn N+1: {'turn':N+1} -> header -> MEMORY text -> tool markers -> ...

The marker lands at the END of the answer turn, so the answer is already
displayed and no memory text has been displayed yet. The implementation
freezes the display at the marker: everything after goes to full/history only.

Asserts:
  1. done text contains the answer, NOT the memory text
  2. no 'next' piece after the freeze contains memory text
  3. history (turn_resps) keeps the FULL text incl. memory (audit intact)
  4. no settlement -> behavior unchanged (everything shown)
  5. marker with no prior visible answer -> minimal shown, no crash
"""
import sys, os, json
sys.path.insert(0, '/home/pushuai/GenericAgent')
import agentmain
import agent_loop

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name}  {detail}")

def collect(chunks):
    turn_resps = []
    events = list(agentmain.iter_display_events(iter(chunks), "src", turn_resps))
    return events, turn_resps

def collect_stop(chunks, stop_check):
    turn_resps = []
    events = list(agentmain.iter_display_events(iter(chunks), "src", turn_resps,
                                                stop_check=stop_check))
    return events, turn_resps

ANSWER = "这是给用户的最终回答：任务已完成，结果在 temp/report.md。"
MEMORY = "现在把经验写入 L1/L2 记忆：用户偏好、模型分工……"
HDR1 = "\nLLM Running (Turn 1) ...\n\n"
HDR2 = "\nLLM Running (Turn 2) ...\n\n"
TOOL = "🛠️ Tool: `start_long_term_update`  📥 args:\n````text\n{}\n````\n"
TOOLR = {'tool_result': {'full': '```text\nok\n```', 'preview': '📄 结果 3 行\n'}}

print("[1] answer kept, memory hidden (real ordering: marker at end of answer turn)")
events, history = collect([
    {'turn': 1}, HDR1, ANSWER + "\n", TOOL, TOOLR,
    {'settlement': True, 'turn': 1},
    {'turn': 2}, HDR2, MEMORY + "\n",
    "🛠️ Tool: `file_patch`  📥 args:\n````text\n{}\n````\n",
    {'tool_result': {'full': '```text\nwrote L2\n```', 'preview': '📄 结果 3 行\n'}},
])
check("last event is done", 'done' in events[-1])
check("done contains the answer", ANSWER in events[-1]['done'], repr(events[-1]['done']))
check("done does NOT contain memory text", MEMORY not in events[-1]['done'] and "L1/L2" not in events[-1]['done'], repr(events[-1]['done']))
shown_stream = "".join(e.get('next', '') for e in events if 'done' not in e)
check("no memory text in streamed pieces", MEMORY not in shown_stream, shown_stream[-120:])
check("history keeps full text (answer+memory)", MEMORY in "".join(history) and ANSWER in "".join(history))

print("[2] no settlement -> unchanged (everything shown)")
events2, hist2 = collect([
    {'turn': 1}, "\nTurn 1 ...\n\n", "第一段回答。\n",
    {'turn': 2}, "\nTurn 2 ...\n\n", "第二段回答。\n",
])
check("no settlement marker -> full done", "第一段" in events2[-1]['done'] and "第二段" in events2[-1]['done'], repr(events2[-1]['done'][:80]))
check("history has both turns", len(hist2) == 2 and "第一段" in hist2[0] and "第二段" in hist2[1])
check("all next pieces flow (no freeze)", len([e for e in events2 if 'next' in e]) >= 1)

print("[3] marker with no prior answer -> minimal shown, no crash")
events3, _ = collect([{'turn': 1}, HDR1, TOOL, TOOLR, {'settlement': True, 'turn': 1},
                      {'turn': 2}, HDR2, MEMORY + "\n"])
check("done has no memory text", MEMORY not in events3[-1]['done'] and "L1/L2" not in events3[-1]['done'], repr(events3[-1]['done']))
check("no crash, done present", 'done' in events3[-1])

print("[4] stop_check still honored")
calls = {'n': 0}
def stop_after_first():
    calls['n'] += 1
    return calls['n'] > 3
ev4 = list(agentmain.iter_display_events(iter([{'turn': 1}, HDR1, "a\n", "b\n", "c\n", "d\n"]), "s", [], stop_check=stop_after_first))
check("stopped early (not all pieces)", len([e for e in ev4 if 'next' in e]) < 4)

print("[5] settlement notice reaches display AND final text (live side)")
notice = agent_loop.SETTLEMENT_NOTICE
events5, history5 = collect([
    {'turn': 1}, HDR1, ANSWER + "\n", TOOL, TOOLR,
    {'settlement': True, 'turn': 1},
    {'turn': 2}, HDR2, MEMORY + "\n", "🛠️ Tool: `file_patch`  📥 args:\n````text\n{}\n````\n",
])
streamed5 = "".join(e.get('next', '') for e in events5 if 'done' not in e)
done5 = next((e['done'] for e in events5 if 'done' in e), '')
check("notice streamed to display", notice.strip() in streamed5)
check("notice in final done text", notice.strip() in done5)
check("memory text still hidden from display", MEMORY not in streamed5)
check("memory text still absent from done text", MEMORY not in done5)

print("[6] /continue replay drops settlement turn and mirrors the same notice")
sys.path.insert(0, os.path.join(agentmain.script_dir, 'frontends'))
try:
    import continue_cmd
    replayed = continue_cmd.extract_ui_messages(_LOG_FIXTURE) if False else None
except Exception as exc:                                  # pragma: no cover
    continue_cmd = None
if continue_cmd is not None:
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8') as fh:
        fh.write("=== Prompt === 2026-01-01 00:00:00\n"
                 + json.dumps({"role": "user", "content": [{"type": "text", "text": "do the thing"}]}, ensure_ascii=False) + "\n"
                 + "=== Response === 2026-01-01 00:00:01 model=m\n"
                 + repr([{'type': 'text', 'text': 'the answer'}]) + "\n"
                 + "=== Prompt === 2026-01-01 00:00:02\n"
                 + json.dumps({"role": "user", "content": [{"type": "text", "text": "### [后台记忆维护] internal"}]}, ensure_ascii=False) + "\n"
                 + "=== Response === 2026-01-01 00:00:03 model=m\n"
                 + repr([{'type': 'text', 'text': 'memory chatter'}]) + "\n")
        fixture = fh.name
    restored = continue_cmd.extract_ui_messages(fixture)
    os.unlink(fixture)
    tail = restored[-1]['content'] if restored else ''
    check("restored bubble keeps the answer", 'the answer' in tail)
    check("restored bubble carries the same notice", notice.strip() in tail)
    check("restored bubble hides memory chatter", 'memory chatter' not in tail)

    # Framework auto-retries must not be replayed as user bubbles.
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8') as fh:
        fh.write("=== Prompt === 2026-01-01 00:00:00\n"
                 + json.dumps({"role": "user", "content": [
                     {"type": "text", "text": "[ERROR] Incomplete response. Regenerate and tooluse.\ncwd = /tmp"}]}, ensure_ascii=False) + "\n"
                 + "=== Response === 2026-01-01 00:00:01 model=m\n"
                 + repr([{'type': 'text', 'text': 'retrying'}]) + "\n")
        fixture = fh.name
    retry_msgs = continue_cmd.extract_ui_messages(fixture)
    os.unlink(fixture)
    check("auto-retry prompt is not a user bubble",
          not any(m['role'] == 'user' and '[ERROR]' in (m.get('content') or '')
                  for m in retry_msgs))

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
