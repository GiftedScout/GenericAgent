import json, tempfile
from pathlib import Path
from agent_loop import agent_runner_loop
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
    # Settlement is ordinary main-loop work: all tools and the normal callback
    # remain available, then the final answer to the original task is displayed.
    assert all(tools == schema() for _, tools in client.calls[10:])
    assert len(handler.callback_calls) == 15, len(handler.callback_calls)
    assert [e.get('settlement') for e in events if isinstance(e, dict) and 'settlement' in e] == []
    display_events = list(iter_display_events(iter(events), 'test', []))
    shown = display_events[-1]['done']
    assert 'FINAL USER ANSWER' in shown and 'maintenance finished' in shown
    print('E2E SINGLE MAIN LOOP + FINAL ANSWER PASS', len(client.calls))

# The regular max_turns budget applies uniformly, including after memory maintenance.
responses = [Resp([Call(str(i), 'file_read', {'path': 'missing', 'start': 1, 'count': 1})])
             for i in range(10)]
responses += [Resp([Call('s', 'start_long_term_update', {})])]
responses += [Resp([Call(str(i), 'file_read', {'path': 'missing', 'start': 1, 'count': 1})])
              for i in range(4)]
client, handler, events = run(Path(tempfile.mkdtemp()), responses, max_turns=12)
assert handler._last_exit['result'] == 'MAX_TURNS_EXCEEDED', handler._last_exit
assert len(client.calls) == 12, len(client.calls)
assert all(tools == schema() for _, tools in client.calls)
assert len(handler.callback_calls) == 12, len(handler.callback_calls)
print('E2E ORDINARY TURN BUDGET APPLIES THROUGHOUT PASS', len(client.calls))
