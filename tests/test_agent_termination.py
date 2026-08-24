import json
import sys
from types import SimpleNamespace
sys.path.insert(0, ".")
from agent_loop import agent_runner_loop, StepOutcome

class Resp:
    def __init__(self, calls=None, text="final"):
        self.tool_calls = calls or []
        self.content = text
        self.thinking = ""

class Fn:
    def __init__(self, name, args="{}"):
        self.name = name; self.arguments = args
class Call:
    def __init__(self, name, args="{}"):
        self.function = Fn(name, args); self.id = name + "-id"

class Client:
    def __init__(self, responses):
        self.responses = list(responses); self.calls = []; self.last_tools = ""
    def chat(self, messages, tools):
        self.calls.append((messages, tools))
        r = self.responses.pop(0)
        if False:
            yield None
        return r
class Handler:
    def __init__(self, outcomes):
        self.parent = SimpleNamespace(task_dir="")
        self.outcomes = list(outcomes); self.current_turn = 0; self.max_turns = 0
        self._done_hooks = ["SHOULD NEVER RUN"]
        self.callbacks = []
    def dispatch(self, name, args, response, index=0, tool_num=1):
        outcome = self.outcomes.pop(0)
        if False: yield ""
        return outcome
    def turn_end_callback(self, *args):
        self.callbacks.append(args)
        return args[4]

schema = [
    {"type":"function", "function":{"name":"file_read"}},
    {"type":"function", "function":{"name":"file_patch"}},
    {"type":"function", "function":{"name":"code_run"}},
]
# Final answer -> memory marker -> one settlement no-tool finalizes.
client = Client([
    Resp([Call("start_long_term_update")], "final answer"),
    Resp([], "memory settled"),
])
handler = Handler([
    StepOutcome("L0", next_prompt="summarize memory", settlement=True),
    StepOutcome("done", next_prompt=None),
])
events = list(agent_runner_loop(client, "sys", "user", handler, schema, max_turns=20, verbose=False))
assert len(client.calls) == 2, len(client.calls)
assert client.calls[1][1] == [schema[0], schema[1]], client.calls[1][1]
assert events[-1] == "memory settled\n", events[-1]
assert not handler._done_hooks

# A normal terminal no-tool answer must not consume a completion hook.
client2 = Client([Resp([], "answer")])
handler2 = Handler([StepOutcome("done", next_prompt=None)])
list(agent_runner_loop(client2, "sys", "user", handler2, schema, max_turns=20, verbose=False))
assert len(client2.calls) == 1
assert handler2._done_hooks == ["SHOULD NEVER RUN"]
print("TERMINATION PASS", len(client.calls), len(client2.calls))
