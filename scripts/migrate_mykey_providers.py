# -*- coding: utf-8 -*-
"""Migrate mykey.py from flat top-level provider variables (+ LLM_CATALOG) to a
single `native_config = {...}` dict.

What it does, per top-level statement:
  * the first provider assignment becomes the whole `native_config` dict; the
    remaining provider assignments are dropped
  * each entry keeps its original body text verbatim (inline comments included),
    re-indented one level, plus inlined 'protocol' and 'type'/'router' (the
    latter taken from the old LLM_CATALOG, which is then dropped)
  * comment blocks / docstrings / web_search_config* / everything else is
    emitted byte-for-byte

Entry keys are the old variable suffixes, so the legacy names the framework
consumes are reconstructed exactly.  The one suffix used by two protocols
(`4router`) keeps its plain key for the oai entry and gets a protocol prefix on
the claude one, because keys share one namespace.

Usage:
    python3 scripts/migrate_mykey_providers.py [path]            # dry run, prints a diff
    python3 scripts/migrate_mykey_providers.py [path] --apply    # backs up, then rewrites
"""

import ast
import os
import re
import shutil
import sys
import time

PROVIDER_RE = re.compile(r'^native_(oai|claude)_config_(.+)$')
DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mykey.py')


def build(src):
    """Return (new_source, report). Pure — never touches the filesystem."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    providers = []          # (var, proto, suffix, node)
    catalog = {}
    drop = set()
    for node in tree.body:
        name = getattr(getattr(node, 'targets', [None])[0], 'id', None) if isinstance(node, ast.Assign) else None
        m = PROVIDER_RE.match(name or '')
        if m and isinstance(node.value, ast.Dict):
            providers.append((name, m.group(1), m.group(2), node))
        elif name == 'LLM_CATALOG' and isinstance(node.value, ast.Dict):
            catalog = ast.literal_eval(node.value)
            drop.add(id(node))

    if not providers:
        return src, {'providers': 0, 'changed': False, 'entries': []}

    # A suffix used by both protocols needs a distinct key for one of them.
    seen = {}
    for var, proto, suffix, _n in providers:
        seen.setdefault(suffix, []).append(proto)
    clashes = {s for s, ps in seen.items() if len(ps) > 1}

    entries = []            # (key, proto, body_lines)
    for var, proto, suffix, node in providers:
        key = f'{proto}_{suffix}' if (suffix in clashes and proto != 'oai') else suffix
        body = lines[node.lineno:node.end_lineno - 1]      # inner lines of {...}
        body = [('    ' + ln if ln.strip() else ln) for ln in body]
        meta = catalog.get(var) or {}
        extra = [f"        'protocol': '{proto}',\n"]
        if meta.get('type'):
            extra.append("        'type': " + repr(str(meta['type'])) + ",\n")
        if meta.get('router'):
            extra.append("        'router': " + repr(str(meta['router'])) + ",\n")
        entries.append([("    " + repr(key) + ": {\n").replace("'", "'", 1), proto, body + extra])

    out, emitted, dropped_vars = [], False, []
    for node in tree.body:
        if id(node) in drop:
            continue
        name = getattr(getattr(node, 'targets', [None])[0], 'id', None) if isinstance(node, ast.Assign) else None
        if PROVIDER_RE.match(name or '') and isinstance(node.value, ast.Dict):
            if emitted:
                dropped_vars.append(name)
                continue
            emitted = True
            out.append('native_config = {\n')
            for head, _proto, body in entries:
                out.append(head)
                out.extend(body)
                out.append('    },\n')
            out.append('}\n')
            continue
        out.append(''.join(lines[node.lineno - 1:node.end_lineno]))

    if not emitted:                                    # providers only in a docstring
        return src, {'providers': 0, 'changed': False, 'entries': []}
    new_src = ''.join(out)
    return new_src, {'providers': len(providers),
                     'changed': new_src != src,
                     'entries': [(e[0].strip().rstrip('{').strip().rstrip(':'), e[1]) for e in entries],
                     'dropped': dropped_vars,
                     'catalog_dropped': bool(drop)}


def identities(mod_dict):
    """Legacy variable list + model@apibase identities, for equivalence checks."""
    import re as _re
    out = []
    for k, v in mod_dict.items():
        if _re.match(r'^native_(oai|claude)_config_', k) and isinstance(v, dict):
            out.append((k, str(v.get('model')), str(v.get('apibase'))))
    return out


def main():
    path = next((a for a in sys.argv[1:] if not a.startswith('-')), DEFAULT)
    path = os.path.abspath(path)
    apply_now = '--apply' in sys.argv
    src = open(path, encoding='utf-8').read()

    new_src, report = build(src)
    print(f'[migrate] {path}')
    print(f"[migrate] providers={report['providers']} catalog_dropped={report.get('catalog_dropped')} "
          f"dropped_top_level={len(report.get('dropped', []))}")
    for key, proto in report['entries']:
        print(f"    {proto:<7} {key}")
    if not report['changed']:
        print('[migrate] nothing to do')
        return 0

    if not apply_now:
        import difflib
        head = list(difflib.unified_diff(src.splitlines(True), new_src.splitlines(True),
                                         'mykey.py.old', 'mykey.py.new', n=1))
        print(''.join(head[:120]))
        print(f'[migrate] DRY RUN — {len(head)} diff lines (pass --apply to write)')
        return 0

    backup = f'{path}.bak-{time.strftime("%Y%m%d-%H%M%S")}'
    shutil.copy2(path, backup)
    tmp = path + '.new'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new_src)
    ast.parse(new_src)                       # refuse to install a broken file
    os.replace(tmp, path)
    print(f'[migrate] backup -> {backup}')
    print(f'[migrate] wrote  -> {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
