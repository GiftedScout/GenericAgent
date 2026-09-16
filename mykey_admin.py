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
