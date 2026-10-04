# -*- coding: utf-8 -*-
"""Pure helpers for editing providers in mykey.py.

Stdlib-only by design, so any frontend can write a key without dragging in a
web stack: the terminal TUI (`/addkey`) uses it directly, and it is the natural
home for the desktop bridge's editing code too.

Layout handled: the single `native_config = {...}` dict (llmcore expands it into
the legacy flat names at load).  Legacy flat files still work — entries are then
appended as top-level variables, exactly as before.
"""

import json
import os
import re
import shutil
import time

NATIVE_CONFIG_VAR = "native_config"

# Interface-grouped layout (mykey.py since 2026-09): group variable →
# default api_mode.  llmcore._expand_nested_groups flattens it at load.
NESTED_GROUPS = (
    ("native_oai_config", "responses"),
    ("native_claude_config", "claude"),
    ("native_chat_config", "chat_completions"),
    ("native_image_config", "images/generations"),
)

# Written into new entries; the rest of BaseSession's knobs stay optional.
PROTOCOLS = ("oai", "claude")
# Shown by the TUI form so users know what they may type.  Full list lives in
# llmcore.BaseSession.__init__.
FIELD_HINTS = (
    ("name", "显示名/mixin 引用名（缺省=model）"),
    ("apikey", "密钥（必填）"),
    ("apibase", "接口地址（必填）"),
    ("model", "模型名（必填）"),
    ("protocol", "oai | claude"),
    ("api_mode", "responses | chat_completions | images/generations（缺省按 protocol 推）"),
    ("type", "厂商（TUI 分组，缺省按 protocol 推）"),
    ("router", "路由/渠道（TUI 分组，缺省=apibase 主机名）"),
)

KNOWN_FIELDS = (
    "name", "apikey", "apibase", "model", "protocol", "api_mode",
    "reasoning_effort", "thinking_type", "thinking_budget_tokens", "omit_thinking",
    "reasoning_format", "temperature", "max_tokens", "context_win",
    "history_char_limit", "trim_keep_rate", "trim_keep_prefix",
    "connect_timeout", "timeout", "read_timeout", "max_retries", "max_retry_after",
    "stream", "proxy", "verify", "user_agent", "ssh_tunnel", "api_key_header",
    "fake_cc_system_prompt", "extra_sys_prompt", "extra_sys_prompt_file",
    "service_tier", "type", "router",
)
_BOOL_FIELDS = ("omit_thinking", "stream", "verify", "fake_cc_system_prompt")
_INT_FIELDS = ("thinking_budget_tokens", "max_tokens", "context_win",
               "history_char_limit", "trim_keep_prefix", "connect_timeout",
               "timeout", "read_timeout", "max_retries")


def mykey_path(root: str = "") -> str:
    """Path of the mykey.py that llmcore would load."""
    import llmcore
    p = getattr(llmcore, "_mykey_path", None)
    if p:
        return p
    return os.path.join(root or os.path.dirname(os.path.abspath(__file__)), "mykey.py")


def backup(path: str) -> str:
    """Copy `path` next to itself as `.bak-<ts>` (holds live keys: gitignored)."""
    dst = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dst)
    return dst


# ── parsing ────────────────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"\s+(?=(?:" + "|".join(KNOWN_FIELDS) + r")\s*[:=])")


def parse_entry(text: str) -> dict:
    """`key: value` / `key=value` lines (or one line of space-separated pairs)
    into a cfg dict.

    Splitting happens only in front of a *known* field name, so values that
    contain a colon (URLs, `claude-x:beta`) survive intact.  Unknown keys are
    rejected rather than dropped — llmcore silently ignores fields it doesn't
    know, so a typo would otherwise look like it worked."""
    out, unknown = {}, []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("-*").strip()
        if not line or line.startswith("#"):
            continue
        for part in _SPLIT_RE.split(line):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z_0-9]*)\s*[:=]\s*(.*)$", part)
            if not m:
                raise ValueError(f"无法解析: {part!r}（应为 key: value）")
            k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
            if k not in KNOWN_FIELDS:
                unknown.append(k)
                continue
            if k in _BOOL_FIELDS:
                out[k] = v.lower() not in ("false", "0", "no", "off")
            elif k in _INT_FIELDS and re.fullmatch(r"\d+", v or ""):
                out[k] = int(v)
            else:
                out[k] = v
    if unknown:
        raise ValueError("未知字段: " + ", ".join(sorted(set(unknown))))
    missing = [k for k in ("apikey", "apibase", "model") if not out.get(k)]
    if missing:
        raise ValueError("缺少必填字段: " + ", ".join(missing))
    proto = str(out.get("protocol") or "").strip().lower()
    if proto and proto not in PROTOCOLS:
        raise ValueError("protocol 只能是 oai 或 claude")
    out["protocol"] = proto or ("claude" if "claude" in str(out["model"]).lower() else "oai")
    return out


# ── text surgery ───────────────────────────────────────────────────────────

def format_dict(d: dict, indent: int = 4) -> str:
    pad = " " * indent
    body = []
    for k, v in d.items():
        if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            body.append(f"{pad}'{k}': {v!r},")
        else:
            body.append(f"{pad}'{k}': {json.dumps(str(v), ensure_ascii=False)},")
    return "{\n" + "\n".join(body) + "\n" + " " * (indent - 4) + "}"


def has_native_config(text: str) -> bool:
    return bool(re.search(rf"^{re.escape(NATIVE_CONFIG_VAR)}\s*=\s*\{{", text, re.M))


def find_block_span(text: str, var_name: str):
    m = re.search(rf"^{re.escape(var_name)}\s*=\s*\{{", text, re.M)
    if not m:
        return None
    start, i, depth = m.start(), m.end() - 1, 0
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                while end < len(text) and text[end] in " \t":
                    end += 1
                if end < len(text) and text[end] in "\r\n":
                    end += 1
                return start, end
        i += 1
    return None


def find_entry_span(text: str, key: str, dict_var: str = NATIVE_CONFIG_VAR):
    """Span of one `'key': {...},` entry inside a top-level dict literal.

    Includes the trailing comma and newline so deleting an entry cannot leave a
    dangling comma behind (which would break the file)."""
    span = find_block_span(text, dict_var)
    if not span:
        return None
    s, e = span
    m = re.search(rf"^[ \t]*{re.escape(repr(key))}[ \t]*:", text[s:e], re.M)
    if not m:
        return None
    start = s + m.start()
    try:
        j = text.index("{", s + m.end())
    except ValueError:
        return None
    depth, i = 0, j
    while i < e:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                while end < len(text) and text[end] in " \t":
                    end += 1
                if end < len(text) and text[end] == ",":
                    end += 1
                while end < len(text) and text[end] in "\r\n":
                    end += 1
                return start, end
        i += 1
    return None


def insert_entry(text: str, key: str, cfg: dict, dict_var: str = NATIVE_CONFIG_VAR) -> str:
    span = find_block_span(text, dict_var)
    if not span:
        raise ValueError(f"config block not found: {dict_var}")
    s, e = span
    close = text[s:e].rstrip().rfind("}")
    if close < 0:
        raise ValueError(f"malformed dict block: {dict_var}")
    at = s + close
    head = text[:at].rstrip("\n")
    if not head.endswith("{"):
        head += "\n"
    entry = f"    {repr(key)}: {format_dict(cfg, 8)},\n"
    return head + entry + text[at:]


def next_entry_key(keys, cfg: dict) -> str:
    stem = "claude" if str(cfg.get("protocol")) == "claude" else "oai"
    taken = {str(k) for k in keys}
    n = 1
    while f"{stem}{n}" in taken:
        n += 1
    return f"{stem}{n}"


def upsert_entry(text: str, key: str, cfg: dict, old_var: str = "") -> str:
    """Add/replace one provider, always inside native_config."""
    entry = dict(cfg)
    entry["protocol"] = str(entry.get("protocol") or "oai")
    if old_var:
        parts = native_var_parts(old_var)
        if parts and parts[1] != key:
            if (span := find_entry_span(text, parts[1])):
                text = text[:span[0]] + text[span[1]:]
    if (span := find_entry_span(text, key)):
        new_entry = f"    {repr(key)}: {format_dict(entry, 8)},\n"
        return text[:span[0]] + new_entry + text[span[1]:]
    return insert_entry(text, key, entry)


def native_var_parts(var: str):
    """('oai' | 'claude', entry_key) for an expanded native_config key."""
    for proto, prefix in (("claude", "native_claude_config_"),
                          ("oai", "native_oai_config_")):
        if var.startswith(prefix) and len(var) > len(prefix):
            return proto, var[len(prefix):]
    return None


# ── interface-grouped layout ───────────────────────────────────────────────

def group_for(cfg: dict) -> str:
    """Which nested group variable an entry belongs to."""
    proto = str(cfg.get("protocol") or "oai").lower()
    if proto == "claude":
        return "native_claude_config"
    mode = str(cfg.get("api_mode") or "responses").lower().replace("-", "_")
    if mode == "images/generations":
        return "native_image_config"
    if mode in ("responses", "response"):
        return "native_oai_config"
    return "native_chat_config"


def has_nested_groups(text: str) -> bool:
    for var, _mode in NESTED_GROUPS:
        if re.search(rf"^{re.escape(var)}\s*=\s*\{{", text, re.M):
            return True
    return False


def _entry_text(key: str, cfg: dict, indent: int = 12) -> str:
    body = format_dict(cfg, indent + 4)
    return f"{' ' * indent}{repr(key)}: {body},"


def _group_span(text: str, group: str):
    """Char span (start .. closing brace) of the REAL `group = {...}` dict.

    AST-based on purpose: mykey.py's header docstring contains a sample
    snippet starting with `native_oai_config = {`, so a text/regex search
    would anchor inside the docstring and corrupt it.
    """
    import ast as _ast
    for node in _ast.parse(text).body:
        if (isinstance(node, _ast.Assign)
                and len(node.targets) == 1
                and getattr(node.targets[0], "id", "") == group
                and isinstance(node.value, _ast.Dict)):
            d = node.value
            first_line = text.split("\n")[node.lineno - 1]
            ob = first_line.index("{")
            start = _line_offset(text, node.lineno) + ob
            last_line = text.split("\n")[d.end_lineno - 1]
            cb = last_line.rindex("}")
            end = _line_offset(text, d.end_lineno) + cb
            return start, end
    return None


def _line_offset(text: str, lineno: int) -> int:
    """Offset of the start of 1-based `lineno` within `text`."""
    return sum(len(l) + 1 for l in text.split("\n")[:lineno - 1])


def _block_range(block: str, open_idx: int) -> int:
    """Index of the brace/bracket matching the one at `open_idx`."""
    opener, closer = (("{", "}") if block[open_idx] == "{" else ("[", "]"))
    depth, i = 0, open_idx
    while i < len(block):
        if block[i] == opener:
            depth += 1
        elif block[i] == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced block")


def insert_nested_entry(text: str, cfg: dict, group: str) -> str:
    """Append an entry to `group = {vendor: {router: [entry, ...]}}`.

    Entries are keyless list items; new vendor/router layers are created as
    needed, existing ones are reused."""
    vendor = str(cfg.get("type") or "未标注")
    router = str(cfg.get("router") or "未标注")
    # entry = a dict literal (no key) indented for the list position
    entry = "            " + format_dict(cfg, 20).rstrip() + ",\n"

    span = _group_span(text, group)
    if span is None:
        return (text.rstrip() + f"\n{group} = {{\n"
                f"    {repr(vendor)}: {{\n        {repr(router)}: [\n"
                f"{entry}        ],\n    }},\n}}\n")
    gs, gc = span
    body = text[gs:gc]
    mv = re.search(rf"^[ \t]*{re.escape(repr(vendor))}[ \t]*:\s*\{{", body, re.M)
    if mv:
        vi = _block_range(body, body.index("{", mv.end() - 1))
        vbody = body[mv.end():vi]
        mr = re.search(rf"^[ \t]*{re.escape(repr(router))}[ \t]*:\s*[\{{\[]", vbody, re.M)
        if mr:
            rj = vbody.index(vbody[mr.end() - 1], mr.end() - 1)
            ri = _block_range(vbody, rj)
            new_vbody = vbody[:ri] + entry + vbody[ri:]
            new_body = body[:mv.end()] + new_vbody + body[vi:]
        else:
            new_vbody = vbody.rstrip() + f"\n        {repr(router)}: [\n{entry}        ],\n"
            new_body = body[:mv.end()] + new_vbody + body[vi:]
    else:
        new_body = body.rstrip() + (f"\n    {repr(vendor)}: {{\n"
                                    f"        {repr(router)}: [\n{entry}        ],\n"
                                    f"    }},\n")
    return text[:gs] + new_body + text[gc:]


def default_type_router(cfg: dict) -> None:
    """Fill `type`/`router` defaults so the TUI picker can group the entry."""
    if not cfg.get("type"):
        model = str(cfg.get("model") or "").lower()
        cfg["type"] = ("Claude" if "claude" in model else
                       "本地模型" if str(cfg.get("apibase", "")).startswith(("http://127.", "http://localhost"))
                       else "其他模型")
    if not cfg.get("router"):
        host = str(cfg.get("apibase") or "").split("//")[-1].split("/", 1)[0]
        cfg["router"] = host or "未标注路由"


# ── high-level write ───────────────────────────────────────────────────────

def add_provider(cfg: dict, path: str = "", root: str = "") -> tuple[str, str]:
    """Write `cfg` as a new provider.  Returns (entry_key, path).

    Legacy flat files (no native_config) get a top-level variable appended, so
    the command works on old-style mykey.py too."""
    path = path or mykey_path(root)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not has_native_config(text):
        proto = str(cfg.get("protocol") or "oai")
        var = f"native_{'claude' if proto == 'claude' else 'oai'}_config_{cfg['model'].split('/')[0][:16]}"
        var = re.sub(r"[^0-9A-Za-z_]", "_", var)
        if re.search(rf"^{re.escape(var)}\s*=", text, re.M):
            n = 2
            while re.search(rf"^{re.escape(var)}_{n}\s*=", text, re.M):
                n += 1
            var = f"{var}_{n}"
        text = text.rstrip() + f"\n{var} = {format_dict(cfg)}\n"
        entry_key = var
    elif has_nested_groups(text):
        entry = dict(cfg)
        default_type_router(entry)          # fills type/router for group placement
        # type/router are structural (vendor/router keys), not entry fields
        entry.pop("protocol", None)
        entry.pop("type", None)
        entry.pop("router", None)
        text = insert_nested_entry(text, entry, group_for(cfg))
        entry_key = f"{group_for(cfg)}/{entry.get('name', entry.get('model'))}"
    else:
        import llmcore
        try:
            keys = [k for k in llmcore.reload_mykeys()[0]]
        except Exception:
            keys = [m.group(1) for m in re.finditer(r"^\s*'([^']+)'\s*:", text, re.M)]
        entry_key = next_entry_key(keys, cfg)
        text = upsert_entry(text, entry_key, cfg)
    backup(path)
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    import ast
    ast.parse(text)                      # never install a file that won't import
    os.replace(tmp, path)
    return entry_key, path
