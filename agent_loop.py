import json, re, os
from dataclasses import dataclass
from typing import Any, Optional
try: from plugins.hooks import trigger as _hook
except ImportError: _hook = lambda *a, **k: None
@dataclass
class StepOutcome:
    data: Any
    next_prompt: Optional[str] = None
    should_exit: bool = False
    # Start a bounded, memory-only settlement sub-loop.  This is deliberately
    # separate from should_exit: the tool must still let the model finish the
    # approved file_read/file_patch/file_write memory update before stopping.
    settlement: bool = False
def try_call_generator(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if hasattr(ret, '__iter__') and not isinstance(ret, (str, bytes, dict, list)): ret = yield from ret
    return ret

class BaseHandler:
    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason): return next_prompt
    def dispatch(self, tool_name, args, response, index=0, tool_num=1):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args['_index'] = index; args['_tool_num'] = tool_num
            _hook('tool_before', locals())
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            _hook('tool_after', locals())
            return ret
        elif tool_name == 'bad_json': return StepOutcome(None, next_prompt=args.get('msg', 'bad_json'), should_exit=False)
        else:
            yield f"未知工具: {tool_name}\n"
            return StepOutcome(None, next_prompt=f"未知工具 {tool_name}", should_exit=False)

def json_default(o): return list(o) if isinstance(o, set) else str(o)
def exhaust(g):
    try: 
        while True: next(g)
    except StopIteration as e: return e.value

def get_pretty_json(data):
    if isinstance(data, dict) and "script" in data:
        data = data.copy(); data["script"] = data["script"].replace("; ", ";\n  ")
    return json.dumps(data, indent=2, ensure_ascii=False).replace('\\n', '\n')

def _settlement_tools(tools_schema):
    """Return only tools allowed after start_long_term_update."""
    allowed = {"file_read", "file_patch", "file_write"}
    if not isinstance(tools_schema, (list, tuple)):
        return []
    result = []
    for spec in tools_schema:
        if not isinstance(spec, dict):
            continue
        fn = spec.get("function") if isinstance(spec.get("function"), dict) else spec
        if fn.get("name") in allowed:
            result.append(spec)
    return result


def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema, 
                      max_turns=40, verbose=True, initial_user_content=None, yield_info=False):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_content if initial_user_content is not None else user_input}
    ]
    turn = 0;  handler.max_turns = max_turns
    settlement_mode = False
    settlement_turns = 0
    settlement_schema = _settlement_tools(tools_schema)
    _hook('agent_before', locals())
    while turn < handler.max_turns:
        if settlement_mode and settlement_turns >= 8:
            exit_reason = {'result': 'MEMORY_SETTLEMENT_LIMIT'}
            break
        turn += 1
        turnstr = f'LLM Running (Turn {turn}) ...'
        if handler.parent.task_dir:
            turnstr = f'Turn {turn} ...'
        if verbose:
            turnstr = f'**{turnstr}**'
        if yield_info: yield {'turn': turn}
        yield f"\n{turnstr}\n\n"
        if turn%10 == 0: client.last_tools = ''  # 每10轮重置一次工具描述
        _hook('turn_before', locals())
        _hook('llm_before', locals())
        active_tools = settlement_schema if settlement_mode else tools_schema
        response_gen = client.chat(messages=messages, tools=active_tools)
        if verbose:
            response = yield from response_gen
            yield '\n\n'
        else:
            response = exhaust(response_gen)
            cleaned = _clean_content(response.content)
            if cleaned: yield cleaned + '\n'
        _hook('llm_after', locals())

        if not response.tool_calls: tool_calls = [{'tool_name': 'no_tool', 'args': {}}]
        else: tool_calls = [{'tool_name': tc.function.name, 'args': json.loads(tc.function.arguments), 'id': tc.id}
                          for tc in response.tool_calls]
       
        tool_results = []; next_prompts = set(); exit_reason = {}
        for ii, tc in enumerate(tool_calls):
            tool_name, args, tid = tc['tool_name'], tc['args'], tc.get('id', '')
            if tool_name == 'no_tool': pass
            else: 
                if verbose: yield f"🛠️ Tool: `{tool_name}`  📥 args:\n````text\n{get_pretty_json(args)}\n````\n"
                else: yield f"🛠️ {tool_name}({_compact_tool_args(tool_name, args)})\n"
            handler.current_turn = turn
            gen = handler.dispatch(tool_name, args, response, index=ii, tool_num=len(tool_calls))
            try:
                result_parts = []
                while True:
                    # 工具结果只在 agent_loop 内收集；向调用方发无正文进度事件，
                    # 让 stop/abort 仍有机会被外层检查，但不把 stdout 刷进终端。
                    part = next(gen)
                    if part is not None:
                        result_parts.append(str(part))
                    if verbose:
                        yield {"tool_progress": True, "turn": turn}
            except StopIteration as e:
                outcome = e.value

            if verbose:
                full_result = "".join(result_parts)
                full_block = f"`````\n{full_result}`````\n"
                preview = f"📄 结果 {full_result.count(chr(10)) + 1} 行\n"
                # full 只供 agentmain 的 done/history 通道，preview 才进入流式通道。
                yield {"tool_result": {"full": full_block, "preview": preview},
                       "turn": turn}
            
            if outcome.should_exit:
                exit_reason = {'result': 'EXITED', 'data': outcome.data}; break
            if outcome.settlement:
                settlement_mode = True
                handler._done_hooks.clear()
                # 通知外层（agentmain）：进入后台记忆维护。注意结算轮的正文在拿到
                # outcome 之前已经流出（yield from response_gen 先于 dispatch），
                # agentmain 据此把显示缓冲回退到本轮起点并冻结显示通道——
                # 结算/记忆维护的文本不再进入 TUI，避免"记忆吞掉答案"。
                yield {"settlement": True, "turn": turn}
            if not outcome.next_prompt:
                exit_reason = {'result': 'CURRENT_TASK_DONE', 'data': outcome.data}; break
            if outcome.next_prompt.startswith('未知工具'): client.last_tools = ''
            if outcome.data is not None and tool_name != 'no_tool': 
                datastr = json.dumps(outcome.data, ensure_ascii=False, default=json_default) if type(outcome.data) in [dict, list] else str(outcome.data) 
                tool_results.append({'tool_use_id': tid, 'content': datastr})
            next_prompts.add(outcome.next_prompt)
        if exit_reason:
            # CURRENT_TASK_DONE / EXITED are terminal states. Never let an
            # external completion hook resurrect a task that has already
            # produced its final response (especially after memory finalization).
            break
        if not next_prompts:
            if len(handler._done_hooks) == 0:
                break
            next_prompts.add(handler._done_hooks.pop(0))
        next_prompt = handler.turn_end_callback(response, tool_calls, tool_results, turn, '\n'.join(next_prompts), exit_reason)
        _hook('turn_after', locals())
        messages = [{"role": "user", "content": next_prompt, "tool_results": tool_results}]   # just new message, history is kept in *Session
    if exit_reason: handler.turn_end_callback(response, tool_calls, tool_results, turn, '', exit_reason)
    _hook('agent_after', locals())
    return exit_reason or {'result': 'MAX_TURNS_EXCEEDED'}

def _clean_content(text):
    if not text: return ''
    def _shrink_code(m):
        lines = m.group(0).split('\n')
        lang = lines[0].replace('```','').strip()
        body = [l for l in lines[1:-1] if l.strip()]
        if len(body) <= 6: return m.group(0)
        preview = '\n'.join(body[:5])
        return f'```{lang}\n{preview}\n  ... ({len(body)} lines)\n```'
    text = re.sub(r'```[\s\S]*?```', _shrink_code, text)
    for p in [r'<file_content>[\s\S]*?</file_content>', r'<tool_(?:use|call)>[\s\S]*?</tool_(?:use|call)>', r'(\r?\n){3,}']:
        text = re.sub(p, '\n\n' if '\\n' in p else '', text)
    return text.strip()

def _compact_tool_args(name, args):
    a = {k: v for k, v in args.items() if k != '_index'}
    for k in ('path',): 
        if k in a: a[k] = os.path.basename(a[k])
    if name == 'update_working_checkpoint': s = a.get('key_info', ''); return (s[:60]+'...') if len(s)>60 else s
    if name == 'ask_user':
        q = str(a.get('question', ''))
        cs = a.get('candidates') or []
        if cs: q += '\ncandidates:\n' + '\n'.join(f'- {c}' for c in cs)
        return q
    s = json.dumps(a, ensure_ascii=False); return (s[:120]+'...') if len(s)>120 else s
