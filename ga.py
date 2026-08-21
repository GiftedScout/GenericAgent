import sys, os, re, json, time, threading, importlib, webbrowser, requests
from datetime import datetime, timedelta
from pathlib import Path
import tempfile, traceback, subprocess, itertools, collections, difflib, shutil
if sys.stdout is None: sys.stdout = open(os.devnull, "w")
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agent_loop import BaseHandler, StepOutcome, json_default
script_dir = os.path.dirname(os.path.abspath(__file__))

def resolve_memory_dir(repo_dir=None):
    """Return the canonical personal-memory directory for this checkout.

    Linked Git worktrees share the main worktree's ignored personal memory by
    default. GA_MEMORY_DIR remains an explicit escape hatch for isolated runs.
    """
    override = os.environ.get('GA_MEMORY_DIR')
    if override:
        return str(Path(override).expanduser().resolve())

    root = Path(repo_dir or script_dir).resolve()
    dotgit = root / '.git'
    if dotgit.is_file():
        try:
            marker = dotgit.read_text(encoding='utf-8', errors='replace').strip()
            if marker.startswith('gitdir:'):
                gitdir = Path(marker.split(':', 1)[1].strip()).expanduser()
                if not gitdir.is_absolute():
                    gitdir = dotgit.parent / gitdir
                commondir_file = gitdir.resolve() / 'commondir'
                if commondir_file.is_file():
                    commondir = Path(commondir_file.read_text(encoding='utf-8').strip())
                    if not commondir.is_absolute():
                        commondir = commondir_file.parent / commondir
                    commondir = commondir.resolve()
                    if commondir.name == '.git':
                        return str(commondir.parent / 'memory')
        except (OSError, ValueError):
            pass
    return str(root / 'memory')

memory_dir = resolve_memory_dir()

def safe_print(*args, **kwargs):
    try: print(*args, **kwargs)
    except: pass

def code_run(code, code_type="python", timeout=60, cwd=None, code_cwd=None, stop_signal=None, maxlen=10000, myprint=safe_print):
    """代码执行器
    python: 运行复杂的 .py 脚本（文件模式）
    powershell/bash: 运行单行指令（命令模式）
    优先使用python，仅在必要系统操作时使用powershell"""
    preview = (code[:60].replace('\n', ' ') + '...') if len(code) > 60 else code.strip()
    yield f"[Action] Running {code_type} in {os.path.basename(cwd)}: {preview}\n"
    cwd = cwd or os.path.join(script_dir, 'temp'); tmp_path = None
    if code_type in ["python", "py"]:
        tmp_file = tempfile.NamedTemporaryFile(suffix=".ai.py", delete=False, mode='w', encoding='utf-8', dir=code_cwd)
        cr_header = os.path.join(script_dir, 'assets', 'code_run_header.py')
        if os.path.exists(cr_header):
            with open(cr_header, encoding='utf-8') as f:
                tmp_file.write(f.read())
        tmp_file.write(code)
        tmp_path = tmp_file.name
        tmp_file.close()
        cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]   
    elif code_type in ["powershell", "bash", "sh", "shell", "ps1", "pwsh"]:
        if os.name == 'nt':
            _ps = "pwsh" if shutil.which("pwsh") else "powershell"
            utf8_prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            cmd = [_ps, "-NoProfile", "-NonInteractive", "-Command", utf8_prefix + code]
        else: cmd = ["bash", "-c", code]
    else:
        return {"status": "error", "msg": f"不支持的类型: {code_type}"}
    myprint("code run output:")
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0 # SW_HIDE
    full_stdout = []

    def stream_reader(proc, logs):
        try:
            for line_bytes in iter(proc.stdout.readline, b''):
                try: line = line_bytes.decode('utf-8')
                except UnicodeDecodeError: line = line_bytes.decode('gbk', errors='ignore')
                logs.append(line)
                myprint(line, end="")
        except: pass

    try:
        child_env = os.environ.copy()
        child_env['GA_MEMORY_DIR'] = memory_dir
        process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, cwd=cwd, startupinfo=startupinfo, env=child_env,
            creationflags=0x08000000 if os.name == 'nt' else 0
        )
        start_t = time.time()
        t = threading.Thread(target=stream_reader, args=(process, full_stdout), daemon=True)
        t.start()

        while t.is_alive():
            istimeout = time.time() - start_t > timeout
            if istimeout or stop_signal:
                process.kill()
                myprint("[Debug] Process killed due to timeout or stop signal.")
                if istimeout: full_stdout.append("\n[Timeout Error] 超时强制终止")
                else: full_stdout.append("\n[Stopped] 用户强制终止")
                break
            time.sleep(1)

        t.join(timeout=1)
        exit_code = process.poll()

        stdout_str = "".join(full_stdout)
        status = "success" if exit_code == 0 else "error"
        status_icon = "✅" if exit_code == 0 else "❌"
        if exit_code is None: status_icon = "⏳" 
        output_snippet = smart_format(stdout_str, max_str_len=600, omit_str='\n\n[omitted long output]\n\n')
        output_snippet = re.sub(r'`{4,}', lambda m: m.group(0)[:3] + '\u200b' + m.group(0)[3:], output_snippet)
        yield f"[Status] {status_icon} Exit Code: {exit_code}\n[Stdout]\n{output_snippet}\n"
        if process.stdout: threading.Thread(target=process.stdout.close, daemon=True).start()
        return {
            "status": status,
            "stdout": smart_format(stdout_str, max_str_len=maxlen, omit_str='\n\n[omitted long output]\n\n'),
            "exit_code": exit_code
        }
    except Exception as e:
        if 'process' in locals(): process.kill()
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type == "python" and tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)


def ask_user(question, candidates=None):
    """question: 向用户提出的问题。candidates: 可选的候选项列表"""
    return {"status": "INTERRUPT", "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []}}

import simphtml
driver = None

def _auto_firefox_bridge_enabled():
    return os.environ.get('GA_AUTO_FIREFOX_BRIDGE', '1').lower() not in ('0', 'false', 'no', 'off')

def _browser_unavailable_msg():
    return ("没有可用的浏览器标签页，已尝试自动启动 Firefox Bridge 但未连接成功；"
            "可手动执行 scripts/start_firefox_bridge.sh 查看日志，或设置 GA_AUTO_FIREFOX_BRIDGE=0 禁用自动启动。")

def _run_firefox_bridge():
    """运行 Firefox Bridge 启动脚本（start_firefox_bridge.sh），优先复用现有 bridge；必要时后台/headless 启动。"""
    print("[WebScan] ⚠️ 浏览器未连接，正在运行 Firefox Bridge 恢复脚本（优先复用/后台启动）...")
    script_path = os.path.join(script_dir, 'scripts/start_firefox_bridge.sh')
    ret = subprocess.run(['bash', script_path], capture_output=True, text=True, timeout=120)
    out = (ret.stdout or '')[:500] + ('...' if len(ret.stdout or '') > 500 else '')
    err = (ret.stderr or '')[:200] + ('...' if len(ret.stderr or '') > 200 else '')
    if out.strip(): print(f"[FirefoxBridge] {out}")
    if err.strip(): print(f"[FirefoxBridge] stderr: {err}")
    if ret.returncode != 0:
        print(f"[WebScan] ❌ 恢复脚本返回错误码 {ret.returncode}")
        return False
    # 等待连接建立
    for i in range(10):
        time.sleep(1)
        if len(driver.get_all_sessions()) > 0:
            print(f"[WebScan] ✅ 浏览器已连接！现在可以用了")
            return True
    print("[WebScan] ❌ 脚本执行成功但浏览器仍未连接")
    return False

def first_init_driver():
    global driver
    from TMWebDriver import TMWebDriver
    driver = TMWebDriver()
    for i in range(7):
        time.sleep(2)
        sess = driver.get_all_sessions()
        if len(sess) > 0:
            break
        if i == 4:
            # 无已有后台标签时，静默启动独立 Firefox Bridge；不要调用
            # webbrowser.open，否则会唤起用户当前的可见浏览器窗口。
            if _auto_firefox_bridge_enabled():
                if _run_firefox_bridge():
                    break
                return
            return

_WEB_SEARCH_PROVIDERS = ('tavily', 'brave', 'exa')
_WEB_SEARCH_ENV_KEYS = {
    'tavily': 'TAVILY_API_KEY', 'brave': 'BRAVE_SEARCH_API_KEY', 'exa': 'EXA_API_KEY',
}


def _web_search_configs():
    """Discover configured providers by each dictionary's ``provider`` field.

    Variable names are intentionally ignored: any top-level mykey.py dictionary
    using a supported provider is a search configuration. API keys are returned
    separately so settings dictionaries do not carry credentials through search
    execution.
    """
    try:
        from llmcore import _load_mykeys
        configured = _load_mykeys()
    except Exception:
        configured = {}
    configured = configured if isinstance(configured, dict) else {}
    configs = {}
    for value in configured.values():
        if not isinstance(value, dict):
            continue
        candidate = dict(value)
        provider = str(candidate.get('provider') or '').strip().lower()
        if provider not in _WEB_SEARCH_PROVIDERS:
            continue
        api_key = candidate.pop('api_key', None) or os.environ.get(_WEB_SEARCH_ENV_KEYS[provider])
        # Keep a deterministic first definition, unless a later one supplies a
        # missing key for the same provider.
        if provider not in configs or (not configs[provider][0] and api_key):
            configs[provider] = (api_key, candidate)

    # Retain environment-only usage and GA_WEB_SEARCH_PROVIDER as an explicit
    # default override, without making configuration variable names significant.
    env_provider = str(os.environ.get('GA_WEB_SEARCH_PROVIDER') or '').strip().lower()
    if env_provider in _WEB_SEARCH_PROVIDERS:
        old_key, old_config = configs.get(env_provider, (None, {'provider': env_provider}))
        configs[env_provider] = (os.environ.get(_WEB_SEARCH_ENV_KEYS[env_provider]) or old_key, old_config)
    return configs


def _web_search_config(requested_provider=None):
    """Select one configured provider without exposing its key in output."""
    configs = _web_search_configs()
    requested_provider = str(requested_provider or '').strip().lower()
    if requested_provider:
        api_key, config = configs.get(requested_provider, (None, {}))
        return requested_provider, api_key, config

    env_provider = str(os.environ.get('GA_WEB_SEARCH_PROVIDER') or '').strip().lower()
    preferences = ((env_provider,) if env_provider in _WEB_SEARCH_PROVIDERS else ()) + _WEB_SEARCH_PROVIDERS
    for provider in preferences:
        if provider in configs:
            api_key, config = configs[provider]
            return provider, api_key, config
    return '', None, {}


def _web_search_setup_msg():
    return ("Web search is not configured. Add any top-level dictionary with "
            "`provider` ('tavily', 'brave', or 'exa') and `api_key` to mykey.py, "
            "or set GA_WEB_SEARCH_PROVIDER plus its provider API-key environment variable.")


def _search_error(response):
    try:
        detail = response.json().get('error', response.text)
        if isinstance(detail, dict): detail = detail.get('message') or detail.get('detail') or json.dumps(detail)
    except Exception:
        detail = response.text
    detail = smart_format(str(detail).replace('\n', ' '), max_str_len=500)
    return {"status": "error", "msg": f"Search API returned HTTP {response.status_code}: {detail}"}


def _web_search_request(provider, api_key, config, query, max_results, search_depth, topic, time_range):
    """Make one provider request and return ``(result, retryable_outage)``."""
    timeout = max(1, min(_arg(config, 'timeout', 20, int), 120))
    try:
        if provider == 'tavily':
            payload = {
                'api_key': api_key, 'query': query, 'max_results': max_results,
                'search_depth': search_depth if search_depth in ('basic', 'advanced') else 'basic',
                'topic': topic if topic in ('general', 'news', 'finance') else 'general',
                'include_answer': True,
            }
            if time_range in ('day', 'week', 'month', 'year'): payload['time_range'] = time_range
            response = requests.post(config.get('base_url', 'https://api.tavily.com/search'), json=payload, timeout=timeout)
            if not response.ok: return _search_error(response), False
            data = response.json()
            results = [{k: item[k] for k in ('title', 'url', 'content', 'score', 'published_date') if item.get(k) is not None}
                       for item in data.get('results', [])]
            answer = data.get('answer')
        elif provider == 'brave':
            params = {'q': query, 'count': max_results}
            freshness = {'day': 'pd', 'week': 'pw', 'month': 'pm', 'year': 'py'}
            if time_range in freshness: params['freshness'] = freshness[time_range]
            response = requests.get(config.get('base_url', 'https://api.search.brave.com/res/v1/web/search'),
                                    params=params, headers={'Accept': 'application/json', 'X-Subscription-Token': api_key}, timeout=timeout)
            if not response.ok: return _search_error(response), False
            data = response.json()
            results = [dict((key, value) for key, value in (
                ('title', item.get('title')), ('url', item.get('url')),
                ('content', item.get('description')), ('published_date', item.get('age')),
            ) if value is not None) for item in data.get('web', {}).get('results', [])]
            answer = None
        else:  # exa
            payload = {'query': query, 'type': 'auto', 'num_results': max_results,
                       'contents': {'text': {'max_characters': 3000}}}
            days = {'day': 1, 'week': 7, 'month': 31, 'year': 365}
            if time_range in days:
                payload['startPublishedDate'] = (datetime.now().astimezone() - timedelta(days=days[time_range])).isoformat()
            response = requests.post(config.get('base_url', 'https://api.exa.ai/search'), json=payload,
                                     headers={'x-api-key': api_key, 'Content-Type': 'application/json'}, timeout=timeout)
            if not response.ok: return _search_error(response), False
            data = response.json()
            results = [dict((key, value) for key, value in (
                ('title', item.get('title')), ('url', item.get('url')), ('content', item.get('text')),
                ('score', item.get('score')), ('published_date', item.get('publishedDate')),
            ) if value is not None) for item in data.get('results', [])]
            answer = None
        output = {'status': 'success', 'provider': provider, 'query': query, 'results': results}
        if answer: output['answer'] = answer
        return output, False
    except (requests.Timeout, requests.ConnectionError) as e:
        return {'status': 'error', 'msg': f'{provider} search connection failed: {smart_format(str(e), max_str_len=500)}'}, True
    except requests.RequestException as e:
        return {'status': 'error', 'msg': f'{provider} search request failed: {smart_format(str(e), max_str_len=500)}'}, False
    except (TypeError, ValueError, KeyError) as e:
        return {'status': 'error', 'msg': f'{provider} search returned an invalid response: {smart_format(str(e), max_str_len=500)}'}, False


def web_scan(query, max_results=5, search_depth='basic', topic='general', time_range=None, provider=None):
    """Search via Tavily by default, or explicitly select a configured provider.

    Set ``provider='exa'`` only for specialised or unusually obscure scientific
    research.  In automatic mode Exa is used only after Tavily times out or its
    connection fails; API errors and empty results never consume Exa quota.
    Browser interaction lives in ``web_execute_js(scan=True)``.
    """
    query = str(query or '').strip()
    if not query:
        return {'status': 'error', 'msg': 'query is required'}
    max_results = max(1, min(_arg({'value': max_results}, 'value', 5, int), 20))
    requested_provider = str(provider or '').strip().lower()
    if requested_provider and requested_provider not in _WEB_SEARCH_PROVIDERS:
        return {'status': 'error', 'msg': f'Unsupported search provider: {requested_provider}'}
    selected_provider, api_key, config = _web_search_config(requested_provider)
    if selected_provider not in _WEB_SEARCH_PROVIDERS:
        return {'status': 'error', 'msg': _web_search_setup_msg()}
    if not api_key:
        return {'status': 'error', 'msg': f'{selected_provider} search API key is missing. ' + _web_search_setup_msg()}

    result, retryable_outage = _web_search_request(
        selected_provider, api_key, config, query, max_results, search_depth, topic, time_range)
    if not requested_provider and selected_provider == 'tavily' and retryable_outage:
        fallback_provider, fallback_key, fallback_config = _web_search_config('exa')
        if fallback_provider == 'exa' and fallback_key:
            result, _ = _web_search_request(
                fallback_provider, fallback_key, fallback_config, query, max_results, search_depth, topic, time_range)
            result['fallback_from'] = 'tavily'
    return result


def web_browser_scan(tabs_only=False, switch_tab_id=None, text_only=False, maxlen=35000):
    """Get simplified browser HTML and tab metadata for ``web_execute_js(scan=True)``."""
    global driver
    try:
        if driver is None:
            first_init_driver()
            if driver is None or len(driver.get_all_sessions()) == 0:
                return {"status": "error", "msg": _browser_unavailable_msg()}
        else:
            # driver 已存在但无会话：默认自动启动独立 Firefox Bridge 窗口恢复连接。
            if len(driver.get_all_sessions()) == 0:
                if _auto_firefox_bridge_enabled():
                    if not _run_firefox_bridge():
                        return {"status": "error", "msg": _browser_unavailable_msg()}
                else:
                    return {"status": "error", "msg": _browser_unavailable_msg()}
        tabs = []
        for sess in driver.get_all_sessions(): 
            sess.pop('connected_at', None)
            sess.pop('type', None)
            sess['url'] = sess.get('url', '')[:50] + ("..." if len(sess.get('url', '')) > 50 else "")
            tabs.append(sess)
        if switch_tab_id: driver.default_session_id = switch_tab_id
        result = {
            "status": "success",
            "metadata": {
                "tabs_count": len(tabs), "tabs": tabs,
                "active_tab": driver.default_session_id
            }
        }
        if not tabs_only: 
            importlib.reload(simphtml); result["content"] = simphtml.get_html(driver, cutlist=True, maxchars=maxlen, text_only=text_only)
            if text_only: result['content'] = smart_format(result['content'], max_str_len=maxlen//3, omit_str='\n\n[omitted long content]\n\n')
        return result
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}
    
def format_error(e):
    exc_type, exc_value, exc_traceback = sys.exc_info()
    tb = traceback.extract_tb(exc_traceback)
    if tb:
        f = tb[-1]
        fname = os.path.basename(f.filename)
        return f"{exc_type.__name__}: {str(e)} @ {fname}:{f.lineno}, {f.name} -> `{f.line}`"
    return f"{exc_type.__name__}: {str(e)}"

def log_memory_access(path):
    if 'memory' not in path: return
    stats_file = os.path.join(memory_dir, 'file_access_stats.json')
    try:
        with open(stats_file, 'r', encoding='utf-8') as f: stats = json.load(f)
    except: stats = {}
    fname = os.path.basename(path)
    stats[fname] = {'count': stats.get(fname, {}).get('count', 0) + 1, 'last': datetime.now().strftime('%Y-%m-%d')}
    with open(stats_file, 'w', encoding='utf-8') as f: json.dump(stats, f, indent=2, ensure_ascii=False)

def web_execute_js(script, switch_tab_id=None, no_monitor=False):
    """执行 JS 脚本来控制浏览器，并捕获结果和页面变化"""
    global driver
    try:
        if driver is None:
            first_init_driver()
            if driver is None or len(driver.get_all_sessions()) == 0:
                return {"status": "error", "msg": _browser_unavailable_msg()}
        else:
            if len(driver.get_all_sessions()) == 0:
                if _auto_firefox_bridge_enabled():
                    if not _run_firefox_bridge():
                        return {"status": "error", "msg": _browser_unavailable_msg()}
                else:
                    return {"status": "error", "msg": _browser_unavailable_msg()}
        if switch_tab_id: driver.default_session_id = switch_tab_id
        result = simphtml.execute_js_rich(script, driver, no_monitor=no_monitor)
        return result
    except Exception as e: return {"status": "error", "msg": format_error(e)}

def expand_file_refs(text, base_dir=None):
    """展开文本中的 {{file:路径:起始行:结束行}} 引用为实际文件内容。
    可与普通文本混排。展开失败抛 ValueError。
    base_dir: 相对路径的基准目录，默认为进程 cwd"""
    pattern = r'\{\{file:(.+?):(\d+):(\d+)\}\}'
    def replacer(match):
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        path = os.path.abspath(os.path.join(base_dir or '.', path))
        if not os.path.isfile(path): raise ValueError(f"引用文件不存在: {path}")
        with open(path, 'r', encoding='utf-8') as f: lines = f.readlines()
        if start < 1 or end > len(lines) or start > end: raise ValueError(f"行号越界: {path} 共{len(lines)}行, 请求{start}-{end}")
        return ''.join(lines[start-1:end])
    return re.sub(pattern, replacer, text)
    
def _file_newline(path):
    endings = set(re.findall(rb'\r\n|[\r\n]', Path(path).read_bytes())) if os.path.exists(path) else set()
    return next(iter(endings)).decode() if len(endings) == 1 else None

def _arg(args, name, default, type=None):
    v = args.get(name, default)
    if type is int:
        try: return int(v)
        except (TypeError, ValueError): return default
    if type is bool:
        if isinstance(v, str): return v.strip().lower() in ('1','true','yes','y','on')
        return default if v is None else bool(v)
    return v

def file_patch(path: str, old_content: str, new_content: str):
    """在文件中寻找唯一的 old_content 块并替换为 new_content"""
    path = str(Path(path).resolve())
    try:
        if not os.path.exists(path): return {"status": "error", "msg": "file not found"}
        with open(path, 'r', encoding='utf-8') as f: full_text = f.read()
        if not old_content: return {"status": "error", "msg": "old_content is blank"}
        count = full_text.count(old_content)
        if count == 0: return {"status": "error", "msg": "old_content is not found. Suggestion: use file_read to check current file content, make more small patches. Don't huge overwrite (even with code)"}
        if count > 1: return {"status": "error", "msg": f"find {count} matches, unable to determine unique position. Provide a longer, more specific old_content to ensure uniqueness. Suggestion: include context lines to enhance features, or modify in smaller segments."}
        updated_text = full_text.replace(old_content, new_content)
        with open(path, 'w', encoding='utf-8', newline=_file_newline(path)) as f: f.write(updated_text)
        return {"status": "success", "msg": "file patched successfully"}
    except Exception as e: return {"status": "error", "msg": str(e)}

_read_dirs = set()
def _scan_files(base, depth=2):
    try:
        for e in os.scandir(base):
            if e.is_file(): yield (e.name, e.path)
            elif depth > 0 and e.is_dir(follow_symlinks=False): yield from _scan_files(e.path, depth - 1)
    except (PermissionError, OSError): pass
def file_read(path, start=1, keyword=None, count=200, show_linenos=True):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            stream = ((i, l.rstrip('\r\n')) for i, l in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)
            if keyword:
                before = collections.deque(maxlen=count//3)
                for i, l in stream:
                    if keyword.lower() in l.lower():
                        res = list(before) + [(i, l)] + list(itertools.islice(stream, count - len(before) - 1))
                        break
                    before.append((i, l))
                else: return f"Keyword '{keyword}' not found after line {start}. Falling back to content from line {start}:\n\n" \
                               + file_read(path, start, None, count, show_linenos)
            else: res = list(itertools.islice(stream, count))
            realcnt = len(res); L_MAX = min(max(100, 256000//max(realcnt,1)), 8000); TAG = " ... [TRUNCATED]"
            remaining = sum(1 for _ in itertools.islice(stream, 5000))
            total_lines = (res[0][0] - 1 if res else start - 1) + realcnt + remaining
            tl_str = f"{total_lines}+" if remaining >= 5000 else str(total_lines)
            partial = total_lines > realcnt
            total_tag = f"[FILE] {tl_str} lines" + (f" | PARTIAL showing {realcnt}; assess need for more" if partial else "") + "\n"
            res = [(i, l if len(l) <= L_MAX else l[:L_MAX] + TAG) for i, l in res]
            result = "\n".join(f"{i}|{l}" if show_linenos else l for i, l in res)
            if show_linenos: result = total_tag + result
            elif partial: result += f"\n\n[FILE PARTIAL: showing {realcnt}/{tl_str} lines; assess need for more]"
            _read_dirs.add(os.path.dirname(os.path.abspath(path)))
            return result
    except FileNotFoundError:
        msg = f"Error: File not found: {path}"
        try:
            tgt = os.path.basename(path); parent = os.path.dirname(os.path.abspath(path)); scan = os.path.dirname(parent)
            roots = [parent, scan] + [d for d in _read_dirs if not d.startswith(scan)]
            cands = list(dict.fromkeys(itertools.islice((c for base in roots for c in _scan_files(base)), 2000)))
            top = sorted([(difflib.SequenceMatcher(None, tgt.lower(), c[0].lower()).ratio(), c) for c in cands[:2000]], key=lambda x: -x[0])[:5]
            top = [(s, c) for s, c in top if s > 0.3]
            if top: msg += "\n\nDid you mean:\n" + "\n".join(f"  {c[1]}  ({s:.0%})" for s, c in top)
        except Exception: pass
        return msg
    except Exception as e: return f"Error: {str(e)}"

def smart_format(data, max_str_len=100, omit_str=' ... '):
    if not isinstance(data, str): data = str(data)
    if len(data) < max_str_len + len(omit_str)*2: return data
    return f"{data[:max_str_len//2]}{omit_str}{data[-max_str_len//2:]}"

def consume_file(dr, file):
    if dr and os.path.exists(os.path.join(dr, file)): 
        with open(os.path.join(dr, file), encoding='utf-8', errors='replace') as f: content = f.read()
        os.remove(os.path.join(dr, file))
        return content

class GenericAgentHandler(BaseHandler):
    '''Generic Agent 工具库，包含多种工具的实现。工具函数自动加上了 do_ 前缀。实际工具名没有前缀。'''
    def __init__(self, parent, last_history=None, cwd='./temp', original_task=''):
        self.parent = parent
        self.working = {}
        self.cwd = cwd;  self.current_turn = 0
        self.history_info = last_history if last_history else []
        # A task can outlive the rolling summary/history window.  Keep a bounded,
        # immutable copy for tool-result continuations rather than reconstructing it
        # from recent agent summaries.
        self.original_task = smart_format((original_task or '').strip(), max_str_len=1195)[:1200]
        self.code_stop_signal = []
        self._done_hooks = []
        self.print = safe_print

    def _get_tool_maxlen(self, l, args, growth_rate=1.0):
        multiplier = 1 + (self.parent.get_ctx_multiplier() - 1) * growth_rate
        return int(l * multiplier / args.get('_tool_num', 1))
    def _get_abs_path(self, path):
        if not path: return ""
        return os.path.abspath(os.path.join(self.cwd, path))   

    def _extract_code_block(self, response, code_type):
        code_type = {'python':'python|py', 'powershell':'powershell|ps1|pwsh', 'bash':'bash|sh|shell'}.get(code_type, re.escape(code_type))
        matches = re.findall(rf"```(?:{code_type})\n(.*?)\n```", response.content, re.DOTALL)
        return matches[-1].strip() if matches else None

    def do_ocr(self, args, response):
        """用视觉模型从本地图片提取文字；优先当前模型，失败自动轮换其他已配置视觉模型。"""
        path = args.get("image_path") or args.get("path")
        if not path:
            return StepOutcome("[Error] image_path is required", next_prompt="\n")
        try:
            from media_api import ocr
            backend = getattr(getattr(self, "llmclient", None), "backend", None)
            current = getattr(backend, "_mykey_name", None) if backend is not None else None
            result = ocr(self._get_abs_path(path), prompt=args.get("prompt") or "",
                         timeout=_arg(args, "timeout", 120, int),
                         model=args.get("model") or None, current=current)
            return StepOutcome(result, next_prompt="\n")
        except Exception as e:
            return StepOutcome(f"[Error] OCR failed: {type(e).__name__}: {e}", next_prompt="\n")

    def do_generate_image(self, args, response):
        """调用 image2 的 OpenAI-compatible images/generations 接口。"""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return StepOutcome("[Error] prompt is required", next_prompt="\n")
        try:
            from media_api import generate_image
            result = generate_image(prompt, size=args.get("size", "1K"),
                                    quality=args.get("quality", "auto"),
                                    timeout=_arg(args, "timeout", 180, int),
                                    output_dir=self.cwd)
            return StepOutcome(result, next_prompt="\n")
        except Exception as e:
            return StepOutcome(f"[Error] image generation failed: {type(e).__name__}: {e}", next_prompt="\n")

    def do_code_run(self, args, response):
        '''执行代码片段，有长度限制，不允许代码中放大量数据，如有需要应当通过文件读取进行。'''
        code_type = args.get("type", "python")
        code = args.get("code") or args.get("script")
        if not code:
            code = self._extract_code_block(response, code_type)
            if not code: return StepOutcome("[Error] Code missing. Must use reply code block or 'script' arg.", next_prompt="\n")
        timeout = _arg(args, "timeout", 60, int)
        raw_path = os.path.join(self.cwd, args.get("cwd", './'))
        cwd = os.path.normpath(os.path.abspath(raw_path))
        code_cwd = os.path.normpath(self.cwd)
        maxlen = self._get_tool_maxlen(10000, args)
        if timeout > 600: result = '[ERROR] Timeout must be <= 600 seconds; code not executed. Run time-consuming code in the background instead of waiting for it to finish in the foreground, verify it started successfully, and monitor it until completion or failure.'
        elif code_type == 'python' and _arg(args, "inline_eval", False, bool):
            ns = {'handler':self, 'parent':self.parent, 'history':json.dumps(self.parent.llmclient.backend.history)}
            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                try:
                    try: result = repr(eval(code, ns))
                    except SyntaxError: exec(code, ns); result = ns.get('_r', 'OK')
                except Exception as e: result = f'Error: {e}'
            finally: os.chdir(old_cwd)
        else: result = yield from code_run(code, code_type, timeout, cwd, code_cwd=code_cwd, stop_signal=self.code_stop_signal, maxlen=maxlen, myprint=self.print)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_ask_user(self, args, response):
        question = args.get("question", "请提供输入：")
        candidates = args.get("candidates", [])
        result = ask_user(question, candidates)
        yield f"Waiting for your answer ...\n"
        return StepOutcome(result, next_prompt="", should_exit=True)
    
    def do_web_scan(self, args, response):
        '''Search the web through the configured external search API.'''
        query = args.get("query", "")
        max_results = _arg(args, "max_results", 5, int)
        search_depth = args.get("search_depth", "basic")
        topic = args.get("topic", "general")
        time_range = args.get("time_range")
        provider = args.get("provider")
        result = web_scan(query=query, max_results=max_results, search_depth=search_depth,
                          topic=topic, time_range=time_range, provider=provider)
        show = smart_format(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), max_str_len=300)
        self.print("Web Search Result:", show)
        yield f"Web search result:\n{show}\n"
        maxlen = self._get_tool_maxlen(8000, args)
        return StepOutcome(smart_format(json.dumps(result, ensure_ascii=False, default=json_default), max_str_len=maxlen), next_prompt="\n")
    
    def do_web_execute_js(self, args, response):
        '''Control the browser with JavaScript, or inspect its active page with scan=true.'''
        scan = _arg(args, "scan", False, bool)
        save_to_file = args.get("save_to_file", "")
        switch_tab_id = args.get("switch_tab_id") or args.get("tab_id")
        if scan:
            maxlen = self._get_tool_maxlen(35000, args, growth_rate=0.5)
            result = web_browser_scan(tabs_only=_arg(args, "tabs_only", False, bool),
                                      switch_tab_id=switch_tab_id,
                                      text_only=_arg(args, "text_only", False, bool), maxlen=maxlen)
            content = result.pop("content", None)
            if content is not None:
                result["scan_content"] = content
            result_key = "scan_content"
        else:
            script = args.get("script", "") or self._extract_code_block(response, "javascript")
            if not script: return StepOutcome("[Error] Script missing. Use `scan=true`, a ```javascript block, or the 'script' arg.", next_prompt="\n")
            abs_path = self._get_abs_path(script.strip())
            if os.path.isfile(abs_path):
                with open(abs_path, 'r', encoding='utf-8') as f: script = f.read()
            no_monitor = _arg(args, "no_monitor", False, bool)
            result = web_execute_js(script, switch_tab_id=switch_tab_id, no_monitor=no_monitor)
            result_key = "js_return"
        if save_to_file and result_key in result:
            content = str(result[result_key] or '')
            abs_path = self._get_abs_path(save_to_file)
            result[result_key] = smart_format(content, max_str_len=170)
            try:
                with open(abs_path, 'w', encoding='utf-8') as f: f.write(content)
                result[result_key] += f"\n\n[Saved complete content to {abs_path}]"
            except Exception:
                result[result_key] += f"\n\n[Could not save complete content to {abs_path}]"
        show = smart_format(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), max_str_len=300)
        self.print("Web Browser Result:", show)
        yield f"Web browser result:\n{show}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        result = json.dumps(result, ensure_ascii=False, default=json_default)
        maxlen = self._get_tool_maxlen(8000, args)
        return StepOutcome(smart_format(result, max_str_len=maxlen), next_prompt=next_prompt)
    
    def do_file_patch(self, args, response):
        path = self._get_abs_path(args.get("path", ""))
        yield f"[Action] Patching file: {path}\n"
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        try: new_content = expand_file_refs(new_content, base_dir=self.cwd)
        except ValueError as e:
            yield f"[Status] ❌ 引用展开失败: {e}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        result = file_patch(path, old_content, new_content)
        yield f"\n{str(result)}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_file_write(self, args, response):
        '''用于对整个文件的大量处理，精细修改要用file_patch。
        需要将要写入的内容放在<file_content>标签内，或者放在代码块中'''
        path = self._get_abs_path(args.get("path", ""))
        mode = args.get("mode", "overwrite")  # overwrite/append/prepend
        action_str = {"prepend": "Prepending to", "append": "Appending to"}.get(mode, "Overwriting")
        yield f"[Action] {action_str} file: {os.path.basename(path)}\n"

        def extract_robust_content(text):
            tags = re.findall(r"<file_content[^>]*>(.*?)</file_content>", text, re.DOTALL)
            if tags: return tags[-1].strip()
            blocks = re.findall(r"```[^\n]*\n([\s\S]*?)```", text)
            if blocks: return blocks[-1].strip()
            return None
        
        content = args.get('content') or extract_robust_content(response.content)
        if not content:
            yield f"[Status] ❌ 失败: 未在回复中找到<file_content>代码块内容\n"
            return StepOutcome({"status": "error", "msg": "No content found. Blank is not supported. Put content inside <file_content>...</file_content> tags in your reply body before call file_write."}, next_prompt="\n")
        try:
            new_content = expand_file_refs(content, base_dir=self.cwd)
            if mode == "prepend":
                old = open(path, 'r', encoding="utf-8").read() if os.path.exists(path) else ""
                open(path, 'w', encoding="utf-8", newline=_file_newline(path)).write(new_content + old)
            else:
                with open(path, 'a' if mode == "append" else 'w', encoding="utf-8", newline=_file_newline(path)) as f: f.write(new_content)
            yield f"[Status] ✅ {mode.capitalize()} 成功 ({len(new_content)} bytes)\n"
            next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
            if len(new_content) > 5000: next_prompt = "\n[SYSTEM TIPS] WRITE TOO LONG! MUST RECHECK HALLUCINATIONS! SMALL WRITES OR PATCHES NEXT TIME!"
            return StepOutcome({"status": "success", 'writed_bytes': len(new_content)}, next_prompt=next_prompt)
        except Exception as e:
            yield f"[Status] ❌ 写入异常: {str(e)}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        
    def do_file_read(self, args, response):
        '''读取文件内容。从第start行开始读取。如有keyword则返回第一个keyword(忽略大小写)周边内容'''
        path = self._get_abs_path(args.get("path", ""))
        yield f"\n[Action] Reading file: {path}\n"
        start = _arg(args, "start", 1, int)
        count = _arg(args, "count", 200, int)
        keyword = args.get("keyword")
        show_linenos = _arg(args, "show_linenos", True, bool)
        result = file_read(path, start=start, keyword=keyword,
                           count=count, show_linenos=show_linenos)
        if show_linenos and not result.startswith("Error:"): result = '由于设置了show_linenos，以下返回信息为：(行号|)内容 。\n' + result 
        if ' ... [TRUNCATED]' in result: result += '\n\n（某些行被截断，如需完整内容可改用 code_run 读取）'
        maxlen = self._get_tool_maxlen(15000, args)
        result = smart_format(result, max_str_len=maxlen, omit_str='\n\n[omitted long content]\n\n')
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        log_memory_access(path)
        if 'memory' in path or 'sop' in path: 
            next_prompt += "\n[SYSTEM TIPS] 正在读取记忆或SOP文件，若决定按sop执行请提取sop中的关键点（特别是靠后的）update working memory."
        return StepOutcome(result, next_prompt=next_prompt)
    
    def export_history(self, fn):
        with open(fn, 'w', encoding='utf-8') as f: json.dump(self.parent.llmclient.backend.history, f, ensure_ascii=False)
    def enter_project_mode(self, name): self.parent._ga_project_mode_name = name
    def _in_plan_mode(self): return self.working.get('in_plan_mode')
    def _exit_plan_mode(self): self.working.pop('in_plan_mode', None)
    def enter_plan_mode(self, plan_path): 
        self.working['in_plan_mode'] = plan_path; self.max_turns = 100
        self.print(f"[Info] Entered plan mode with plan file: {plan_path}")
        return plan_path
    def _check_plan_completion(self):
        if not os.path.isfile(p:=self._in_plan_mode() or ''): return None
        try: return len(re.findall(r'\[ \]', open(p, encoding='utf-8', errors='replace').read()))
        except: return None
    
    def do_update_working_checkpoint(self, args, response):
        '''为整个任务设定后续需要临时记忆的重点。'''
        key_info = args.get("key_info", "")
        if "key_info" in args: self.working['key_info'] = key_info
        self.working['passed_sessions'] = 0
        yield f"[Info] Updated key_info.\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        if self.current_turn <= 1: next_prompt += "\n[TIPS] Working checkpoint updated. Do not call update_working_checkpoint again unless new, non-obvious facts appear. Skip for short tasks.\n"
        #next_prompt += '\n[SYSTEM TIPS] 此函数一般在任务开始或中间时调用，如果任务已成功完成应该是start_long_term_update用于结算长期记忆。\n'
        return StepOutcome({"result": "working key_info updated"}, next_prompt=next_prompt)

    def _retry_or_exit(self, prompt):
        self._empty_ct = getattr(self, '_empty_ct', 0) + 1
        if self._empty_ct >= 3: return StepOutcome({}, should_exit=True)
        return StepOutcome({}, next_prompt=prompt)

    def do_no_tool(self, args, response):
        '''这是一个特殊工具，由引擎自主调用，不要包含在TOOLS_SCHEMA里。
        当模型在一轮中未显式调用任何工具时，由引擎自动触发。
        二次确认仅在回复几乎只包含<thinking>/<summary>和一段大代码块时触发。'''
        content = getattr(response, 'content', '') or ""
        thinking = getattr(response, 'thinking', '') or ""
        if not response or (not content.strip() and not thinking.strip()):
            yield "[Warn] LLM returned an empty response. Retrying...\n"
            return self._retry_or_exit("[ERROR] Blank response, regenerate and tooluse")
        if '[!!! 流异常中断' in content[-100:] or '!!!Error:' in content[50:][-100:] or (content.endswith('</summary>') and len(content) < 100):
            return self._retry_or_exit("[ERROR] Incomplete response. Regenerate and tooluse.")
        if 'max_tokens !!!]' in content[-100:]:
            return self._retry_or_exit("[ERROR] max_tokens limit reached. Use multi small steps to do it.")
        
        if self._in_plan_mode() and any(kw in content for kw in ['任务完成', '全部完成', '已完成所有', '🏁']):
            if 'VERDICT' not in content and '[VERIFY]' not in content and '验证subagent' not in content:
                yield "[Warn] Plan模式完成声明拦截。\n"
                return StepOutcome({}, next_prompt="⛔ [验证拦截] 检测到你在plan模式下声称完成，但尚无验证证据。请先执行与任务风险匹配的独立验证并附上[VERIFY]结果，再声称完成。")
            
        # 2. 检测"包含较大代码块但未调用工具"的情况
        # 关键特征：恰好1个大代码块 + 代码块直接结尾（后面只有空白）
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{50,}?```"
        blocks = re.findall(code_block_pattern, content)
        if len(blocks) == 1:
            m = re.search(code_block_pattern, content)
            after_block = content[m.end():]
            if not after_block.strip():
                residual = content.replace(m.group(0), "")
                residual = re.sub(r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE)
                residual = re.sub(r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE)
                clean_residual = re.sub(r"\s+", "", residual)
                if len(clean_residual) <= 30:
                    yield "[Info] Detected large code block without tool call and no extra natural language. Requesting clarification.\n"
                    next_prompt = (
                        "[System] 检测到你在上一轮回复中主要内容是较大代码块，且本轮未调用任何工具。\n"
                        "如果这些代码需要执行、写入文件或进一步分析，请重新组织回复并显式调用相应工具"
                        "（例如：code_run、file_write、file_patch 等）；\n"
                        "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                        "并明确是否还需要额外的实际操作。"
                    )
                    return StepOutcome({}, next_prompt=next_prompt)
                
        if self._in_plan_mode():
            remaining = self._check_plan_completion()
            if remaining == 0:
                self._exit_plan_mode(); yield "[Info] Plan完成：plan.md中0个[ ]残留，退出plan模式。\n"
        
        #yield "[Info] Final response to user.\n"
        return StepOutcome(response, next_prompt=None)
    
    def do_start_long_term_update(self, args, response):
        '''Agent觉得当前任务完成后有重要信息需要记忆时调用此工具。'''
        prompt = '''### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。
本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。
**如果没有经验证的，未来能用上的信息，忽略本次调用！**
**只能提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1
- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP（只记你被坑得多次重试的核心要点）
**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节、只是做了但没有验证的信息
**操作**：严格遵循提供的L0的记忆更新SOP。先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，保证对记忆库最小局部修改。\n
''' + get_global_memory()
        yield "[Info] Start distilling good memory for long-term storage.\n"
        path = './memory/memory_management_sop.md'
        if os.path.exists(path): result = 'This is L0:\n' + file_read(path, show_linenos=False)
        else: result = "Memory Management SOP not found. Do not update memory."
        if self.current_turn < 10: result, prompt = 'start_long_term_update is only used after completing a long turn task!', '\n'
        return StepOutcome(result, next_prompt=prompt)

    def _fold_earlier(self, lines):
        FALLBACK = '直接回答了用户问题'
        parts, cnt, last = [], 0, ''
        def flush():
            if cnt:
                if FALLBACK in last: parts.append(f'[Agent]（{cnt} turns）')
                else: parts.append(f'{last}（{cnt} turns）')
        for line in lines:
            if line.startswith('[USER]'):
                flush(); parts.append(line); cnt = 0; last = ''
            else: cnt += 1; last = line
        flush()
        return "\n".join(parts[-70:])

    def _get_anchor_prompt(self, skip=False):
        if skip: return "\n"
        h = self.history_info; W = 30
        earlier = f'<earlier_context>\n{self._fold_earlier(h[:-W])}\n</earlier_context>\n' if len(h) > W and self.current_turn % 4 == 1 else ""
        joined_history = "\n".join(h[-W:])
        history = f'<history>\n{joined_history}\n</history>' if self.current_turn % 2 == 1 else ""
        prompt = f"\n### [WORKING MEMORY]\n{earlier}{history}"
        if self.original_task: prompt += f"\n<task_anchor>{self.original_task}</task_anchor>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"
        if self.working.get('key_info'): prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
        if getattr(self.parent, 'verbose', False): self.print(prompt)
        return prompt
    
    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        _c = re.sub(r'```.*?```|<thinking>.*?</thinking>', '', response.content, flags=re.DOTALL)
        rsumm = re.search(r"<summary>(.*?)</summary>", _c, re.DOTALL)
        raw = (rsumm.group(1) if rsumm else _c).strip()
        if raw:
            summary = smart_format(raw.replace('\n', ''), max_str_len=80)
            self.history_info.append('[Agent] ' + summary)
        if not rsumm and tool_calls and tool_calls[0]['tool_name'] != 'no_tool':
            next_prompt += "\n\n\n[TIPS] 必须在回复文本中包含<summary>！\n\n"
        _plan = self._in_plan_mode()

        if turn % 175 == 0 and (not _plan):
            next_prompt += f"\n\n[DANGER] Turn {turn}. Must call ask_user to summarize progress and get direction. No more blind retries."
        elif turn % 13 == 0:
            next_prompt += f"\n\n[DANGER] Turn {turn}. Call update_working_checkpoint to save key context. Stop ineffective retries; if no progress, switch strategy: 1) Probe physical boundaries 2) **Re-read relevant SOPs**"
        elif turn % 31 == 0:
            next_prompt += f"\n\n[DANGER] Turn {turn}. Write checkpoints/key findings/tried approaches to a **file** for future reference (not only working_checkpoint!). Avoid losing critical info."
        elif turn % 10 == 0: next_prompt += get_global_memory()

        if _plan and turn >= 10 and turn % 5 == 0:
            next_prompt = f"[Plan Hint] 正在计划模式。必须 file_read({_plan}) 确认当前步骤，回复开头引用：📌 当前步骤：...\n\n" + next_prompt
        if _plan and turn >= 190: next_prompt += f"\n\n[DANGER] Plan模式已运行 {turn} 轮，已达上限。必须 ask_user 汇报进度并确认是否继续。"

        injkeyinfo = self.parent.extrakeyinfo or consume_file(self.parent.task_dir, '_keyinfo')
        injprompt = self.parent.intervene or consume_file(self.parent.task_dir, '_intervene')
        if injkeyinfo: self.working['key_info'] = self.working.get('key_info', '') + f"\n[MASTER] {injkeyinfo}"
        if injprompt: next_prompt += f"\n\n[MASTER] {injprompt}\n"
        self.parent.intervene = self.parent.extrakeyinfo = None
        for hook in list(getattr(self.parent, '_turn_end_hooks', {}).values()): hook(locals())  # current readonly
        return next_prompt

def get_global_memory():
    prompt = "\n"
    try:
        suffix = '_en' if os.environ.get('GA_LANG', '') == 'en' else ''
        with open(os.path.join(memory_dir, 'global_mem_insight.txt'), 'r', encoding='utf-8', errors='replace') as f: insight = f.read()
        with open(os.path.join(script_dir, f'assets/insight_fixed_structure{suffix}.txt'), 'r', encoding='utf-8') as f: structure = f.read()
        prompt += f'cwd = {os.path.join(script_dir, "temp")} (./)\n'
        prompt += f"\n[Memory] ({memory_dir})\n"
        structure = structure.replace('../memory', memory_dir)
        prompt += structure + f'\n{os.path.join(memory_dir, "global_mem_insight.txt")}:\n'
        prompt += insight.replace('../memory', memory_dir) + "\n"
    except FileNotFoundError: pass
    return prompt
