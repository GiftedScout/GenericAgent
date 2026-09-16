# -*- coding: utf-8 -*-
"""Regression: mykey provider edits must stay inside the single `native_config`
dict and always leave a loadable file.

Before, adding a model appended another top-level variable AND required a
hand-edited LLM_CATALOG.  Now the desktop bridge rewrites one entry, and
llmcore._expand_native_config flattens it back to the legacy names on load.

Each case works on a throwaway GA root so the developer's real mykey.py is never
touched.
"""
import ast
import os
import shutil
import sys
import tempfile

ROOT = '/home/pushuai/GenericAgent'
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'frontends'))

import desktop_bridge as db
import llmcore

PASS = FAIL = 0

def check(name, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")

BASE = """mixin_config = {'llm_nos': ['gpt5-sol']}

native_config = {
    'aihub1': {
        'name': 'gpt5-sol',
        'apikey': 'k1',
        'apibase': 'https://aihub.top',
        'model': 'gpt-5-sol',
        'protocol': 'oai',
    },
    'opus0': {
        'name': 'opus5',
        'apikey': 'k2',
        'apibase': 'https://4router.net',
        'model': 'claude-opus-5',
        'protocol': 'claude',
    },
}
"""

def make_root(text=BASE):
    tmp = tempfile.mkdtemp(prefix='ga_mykey_')
    os.makedirs(os.path.join(tmp, 'temp'), exist_ok=True)
    open(os.path.join(tmp, 'mykey_template.py'), 'w').write('')
    open(os.path.join(tmp, 'mykey.py'), 'w').write(text)
    # llmcore caches the resolved mykey path globally; each case re-points it at
    # its own throwaway root (and drops the cached module) so the assertions
    # below read the file this case just wrote.
    import llmcore
    sys.modules.pop('mykey', None)
    llmcore._mykey_path = os.path.join(tmp, 'mykey.py')
    llmcore._mykey_mtime = None
    mgr = db.AgentManager()
    mgr.ga_root = tmp
    return tmp, mgr

def read(tmp):
    return open(os.path.join(tmp, 'mykey.py'), encoding='utf-8').read()

def ids(mgr):
    return {p['varName']: p['id'] for p in mgr.list_model_profiles()}

def parses(text):
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False

print("[1] add writes one entry into native_config (no top-level append)")
tmp, mgr = make_root()
res = mgr.add_model_profile({'apibase': 'https://new.example/v1', 'model': 'gpt-5.7',
                             'apikey': 'k9', 'name': 'gpt5.7', 'protocol': 'oai'})
text = read(tmp)
check("file still parses", parses(text))
check("new entry inside native_config", text.count("native_config = {") == 1)
check("no new top-level native_*_config_ var", text.count("native_oai_config_") == 0)
check("returned varName resolves", res['varName'] in ids(mgr), f"{res['varName']} vs {ids(mgr)}")
check("reload sees the new profile", 'gpt5.7' in [p.get('name') for p in mgr.list_model_profiles()])
shutil.rmtree(tmp)

print("[2] delete removes the entry and leaves a loadable file")
tmp, mgr = make_root()
mgr.delete_model_profile(ids(mgr)['native_oai_config_aihub1'])
text = read(tmp)
check("file still parses", parses(text), text)
check("no dangling comma line", ',\n,\n' not in text and text.count('\n,\n') == 0)
check("deleted entry gone", "'aihub1'" not in text)
check("sibling survives", "'opus0'" in text)
check("reload drops it", 'native_oai_config_aihub1' not in ids(mgr))
shutil.rmtree(tmp)

print("[3] delete a middle entry keeps the rest intact")
extra = ("    'mid0': {\n        'name': 'mid',\n        'apikey': 'k3',\n"
         "        'apibase': 'https://mid.example',\n        'model': 'mid-1',\n"
         "        'protocol': 'oai',\n    },\n")
three = BASE.replace("    'opus0': {", extra + "    'opus0': {")
tmp, mgr = make_root(three)
assert 'native_oai_config_mid0' in ids(mgr), ids(mgr)
mgr.delete_model_profile(ids(mgr)['native_oai_config_mid0'])
text = read(tmp)
check("file still parses", parses(text), text)
check("neighbours survive", "'aihub1'" in text and "'opus0'" in text)
check("reload keeps both", {'native_oai_config_aihub1', 'native_claude_config_opus0'} <= set(ids(mgr)))
shutil.rmtree(tmp)

print("[4] update rewrites in place, and can change the protocol prefix")
tmp, mgr = make_root()
mgr.update_model_profile(ids(mgr)['native_oai_config_aihub1'],
                         {'apibase': 'https://aihub.top', 'model': 'claude-sonnet-4-5',
                          'name': 'sonnet45', 'protocol': 'claude'})
text = read(tmp)
check("file still parses", parses(text), text)
check("entry rewritten", "claude-sonnet-4-5" in text)
check("still one config dict", text.count("native_config = {") == 1)
check("reload reflects the new model",
      'claude-sonnet-4-5' in [p.get('model') for p in mgr.list_model_profiles()])
shutil.rmtree(tmp)

print("[5] legacy flat layout is untouched by the same operations")
LEGACY = """native_oai_config_old1 = {
    'name': 'old1',
    'apikey': 'k',
    'apibase': 'https://old.example',
    'model': 'old-1',
}
"""
tmp, mgr = make_root(LEGACY)
res = mgr.add_model_profile({'apibase': 'https://x.example', 'model': 'plain',
                             'apikey': 'kz', 'protocol': 'oai'})
text = read(tmp)
check("file still parses", parses(text))
check("legacy var appended as before", 'native_oai_config_oai1' in text or res['varName'] in text)
check("legacy entry preserved", 'native_oai_config_old1' in text)
shutil.rmtree(tmp)

print("[6] configure_mykey writes the single-dict layout, and can read it back")
import importlib.util
_spec = importlib.util.spec_from_file_location('cm', os.path.join(ROOT, 'assets', 'configure_mykey.py'))
cm = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(cm)
except SystemExit:
    pass
wiz_cfgs = [
    {'name': 'gpt-sol', 'type': 'native_oai', 'apikey': 'sk-x',
     'apibase': 'https://aihub.top', 'model': 'gpt-5.6', 'api_mode': 'responses'},
    {'name': 'opus5', 'type': 'native_claude', 'apikey': 'sk-y',
     'apibase': 'https://4router.net', 'model': 'claude-opus-5'},
]
wiz_out = cm.generate_mykey(wiz_cfgs, [])
check("wizard output parses", parses(wiz_out), wiz_out[:200])
check("wizard emits one native_config dict", wiz_out.count('native_config = {') == 1)
check("no top-level provider vars", 'native_oai_config_' not in wiz_out and 'native_claude_config_' not in wiz_out)
check("protocol recorded", "'protocol': 'oai'" in wiz_out and "'protocol': 'claude'" in wiz_out)
check("display metadata recorded", "'type': 'OpenAI'" in wiz_out and "'router':" in wiz_out)
wiz_mk = {'native_config': ast.literal_eval(
    wiz_out[wiz_out.index('{', wiz_out.index('native_config')):wiz_out.index('\n}\n', wiz_out.index('native_config')) + 2])}
wiz_exp = llmcore._expand_native_config(wiz_mk)
check("expansion yields both providers",
      [k for k in wiz_exp if 'native_' in k and 'config' in k] ==
      ['native_oai_config_oai1', 'native_claude_config_claude1'], wiz_exp.keys())
check("expansion carries picker catalog", isinstance(wiz_exp.get('LLM_CATALOG'), dict) and wiz_exp['LLM_CATALOG'])
_tmp = tempfile.mkdtemp(prefix='ga_wiz_')
_wiz_path = os.path.join(_tmp, 'mykey.py')
open(_wiz_path, 'w').write(wiz_out)
_old_path = cm.MYKPY_PATH
cm.MYKPY_PATH = _wiz_path
try:
    back = cm._parse_existing_llm_cfgs()
finally:
    cm.MYKPY_PATH = _old_path
    shutil.rmtree(_tmp)
check("wizard can re-read its own output", len(back) == 2, back)
check("re-read keeps name/model/type",
      [(c.get('name'), c.get('type')) for c in back] ==
      [('gpt-sol', 'native_oai'), ('opus5', 'native_claude')], back)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
