import json, tempfile
from pathlib import Path
from agent_loop import agent_runner_loop, SETTLEMENT_CONTINUATION, MEMORY_SETTLEMENT_MAX_TURNS
from ga import GenericAgentHandler
from agentmain import iter_display_events

class Parent:
    task_dir = ''
    verbose = False
    extrakeyinfo = None
    intervene = None
    def get_ctx_multiplier(self): return 1

class Fn:
    def __init__(self, name, args):
        self.name = name
        self.arguments = json.dumps(args)
class Call:
    def __init__(self, ident, name, args):
        self.id = ident
        self.function = Fn(name, args)
class Resp:
    def __init__(self, calls=(), content=''):
        self.tool_calls = list(calls)
        self.content = content
        self.thinking = ''
class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.last_tools = ''
    def chat(self, messages, tools):
        self.calls.append((messages, tools))
        if not self.responses:
            raise AssertionError('fake client exhausted')
        result = self.responses.pop(0)
        if False:
            yield None
        return result
class Handler(GenericAgentHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.callback_calls = []
    def turn_end_callback(self, *args):
        self.callback_calls.append(args)
        return super().turn_end_callback(*args)

def make_handler(root):
    return Handler(Parent(), cwd=str(root), original_task='ORIGINAL USER TASK MUST NOT RETURN')

def schema():
    return [
        {'type': 'function', 'function': {'name': 'start_long_term_update', 'parameters': {}}},
        {'type': 'function', 'function': {'name': 'file_read', 'parameters': {}}},
        {'type': 'function', 'function': {'name': 'file_patch', 'parameters': {}}},
        {'type': 'function', 'function': {'name': 'file_write', 'parameters': {}}},
    ]

def run(root, responses, max_turns=40):
    client = FakeClient(responses)
    handler = make_handler(root)
    events = list(agent_runner_loop(client, 'system', 'user', handler, schema(),
                                    max_turns=max_turns, verbose=False, yield_info=True))
    return client, handler, events

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    target = root / 'memory.txt'
    target.write_text('before\n', encoding='utf-8')
    # Ten real tool turns make the handler accept start_long_term_update at turn 11.
    responses = [Resp([Call(str(i), 'file_read', {'path': 'memory.txt', 'start': 1, 'count': 1})])
                 for i in range(10)]
    responses += [
        Resp([Call('s', 'start_long_term_update', {})], 'FINAL USER ANSWER'),
        Resp([Call('r', 'file_read', {'path': 'memory.txt', 'start': 1, 'count': 10})]),
        Resp([Call('p', 'file_patch', {'path': 'memory.txt', 'old_content': 'before', 'new_content': 'after'})]),
        Resp([Call('v', 'file_read', {'path': 'memory.txt', 'start': 1, 'count': 10})]),
        Resp([], 'maintenance finished'),
    ]
    client, handler, events = run(root, responses)
    assert target.read_text(encoding='utf-8') == 'after\n'
    assert handler._last_exit['result'] == 'CURRENT_TASK_DONE', handler._last_exit
    assert len(client.calls) == 15, len(client.calls)
    # call 10 is the entry turn; call 11 gets the real L0 prompt, later calls
    # receive only the isolated continuation.
    entry_prompt = client.calls[11][0][0]['content']
    assert '[后台记忆维护]' in entry_prompt
    post = client.calls[12:]
    assert all(m[0]['content'] == SETTLEMENT_CONTINUATION for m, _ in post), [m[0]['content'] for m, _ in post]
    assert all('ORIGINAL USER TASK MUST NOT RETURN' not in m[0]['content'] for m, _ in post)
    assert all(not any(x['function']['name'] == 'start_long_term_update' for x in tools) for _, tools in post)
    assert len(handler.callback_calls) == 10, len(handler.callback_calls)
    assert all(call[3] <= 10 for call in handler.callback_calls)
    # The actual display consumer freezes at the settlement marker.
    display_events = list(iter_display_events(iter(events), 'test', []))
    shown = display_events[-1]['done']
    assert 'FINAL USER ANSWER' in shown and 'maintenance finished' not in shown
    assert any(isinstance(e, dict) and e.get('settlement') for e in events)
    print('E2E FILE READ/PATCH/VERIFY + DISPLAY FREEZE PASS', len(client.calls))

# A maintenance loop may exceed three turns, but is bounded at the explicit guard.
responses = [Resp([Call(str(i), 'file_read', {'path': 'missing', 'start': 1, 'count': 1})])
             for i in range(10)]
responses += [Resp([Call('s', 'start_long_term_update', {})])]
responses += [Resp([Call(str(i), 'file_read', {'path': 'missing', 'start': 1, 'count': 1})])
              for i in range(MEMORY_SETTLEMENT_MAX_TURNS)]
client, handler, events = run(Path(tempfile.mkdtemp()), responses)
assert handler._last_exit['result'] == 'MEMORY_SETTLEMENT_LIMIT', handler._last_exit
assert len(client.calls) == 11 + MEMORY_SETTLEMENT_MAX_TURNS
assert '[后台记忆维护]' in client.calls[11][0][0]['content']
assert all(m[0]['content'] == SETTLEMENT_CONTINUATION for m, _ in client.calls[12:])
assert len(handler.callback_calls) == 10, len(handler.callback_calls)
assert all(call[3] <= 10 for call in handler.callback_calls)
print('E2E 16-TURN GUARD PASS', len(client.calls))


# Settlement entered on the last ordinary turn must still get its own budget.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    target = root / 'memory.txt'
    target.write_text('before\n', encoding='utf-8')
    responses = [Resp([Call(str(i), 'file_read', {'path': 'memory.txt', 'start': 1, 'count': 1})]) for i in range(10)]
    responses += [Resp([Call('s', 'start_long_term_update', {})])]
    responses += [Resp([Call('r', 'file_read', {'path': 'memory.txt', 'start': 1, 'count': 1})]), Resp([], 'done')]
    client, handler, events = run(root, responses, max_turns=11)
    assert handler._last_exit['result'] == 'CURRENT_TASK_DONE', handler._last_exit
    assert len(client.calls) == 13, len(client.calls)
    assert '[后台记忆维护]' in client.calls[11][0][0]['content']
    assert client.calls[12][0][0]['content'] == SETTLEMENT_CONTINUATION
    print('E2E LAST-ORDINARY-TURN SETTLEMENT PASS', len(client.calls))
