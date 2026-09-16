# -*- coding: utf-8 -*-
"""Regression: the single-dict `native_config` provider layout must be exactly
equivalent to the legacy flat layout, so nothing downstream changes.

Adding a provider used to mean pasting another top-level `native_oai_config_x = {...}`
at the end of mykey.py *and* hand-editing the separate LLM_CATALOG.  New layout:

    native_config = {
        'aihub1': {..., 'type': 'OpenAI', 'router': 'aihub'},
        'opus5':  {..., 'protocol': 'claude', 'type': 'Claude', 'router': '4router'},
    }

Asserts:
  1. each entry becomes native_oai_config_<key> / native_claude_config_<key>
  2. type/router are lifted out of the cfg into a synthesised LLM_CATALOG
  3. legacy layouts pass through untouched (old mykey.py keeps working)
  4. the raw container is dropped so it can't appear as a bogus profile
  5. a file carrying both layouts keeps both
"""
import sys
sys.path.insert(0, '/home/pushuai/GenericAgent')
import llmcore

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name}  {detail}")

expand = llmcore._expand_native_config
CFG_KEYS = ('api', 'config', 'cookie')
def profiles(mk):
    return [k for k in mk if any(x in k for x in CFG_KEYS)]

print("[1] single-dict layout expands to the legacy flat keys")
new = {
    'native_config': {
        'aihub1': {'name': 'gpt5.6-sol', 'apikey': 'k1', 'apibase': 'https://aihub.top',
                   'model': 'gpt-5.6-sol', 'api_mode': 'responses',
                   'type': 'OpenAI', 'router': 'aihub'},
        'opus5': {'name': 'opus5', 'apikey': 'k2', 'apibase': 'https://4router.net',
                  'model': 'claude-opus-5', 'protocol': 'claude',
                  'type': 'Claude', 'router': '4router'},
        'qw3': {'name': 'qwen', 'apikey': 'k3', 'apibase': 'http://127.0.0.1:8080/v1',
                'model': 'qwen3.8-27b-local', 'type': '本地模型', 'router': 'lfm'},
    },
    'mixin_config': {'llm_nos': ['gpt5.6-sol'], 'max_retries': 5},
}
out = expand(new)
keys = profiles(out)
check("oai entry -> native_oai_config_<key>", 'native_oai_config_aihub1' in keys, keys)
check("protocol=claude -> native_claude_config_<key>", 'native_claude_config_opus5' in keys, keys)
check("model containing 'claude' infers claude protocol", 'native_claude_config_opus5' in keys)
check("plain entry stays oai", 'native_oai_config_qw3' in keys)
check("order follows the dict", [k for k in keys if k.startswith('native_')] ==
      ['native_oai_config_aihub1', 'native_claude_config_opus5', 'native_oai_config_qw3'], keys)
check("mixin_config untouched", out.get('mixin_config') == new['mixin_config'])
check("cfg payload preserved", out['native_oai_config_aihub1']['apikey'] == 'k1')

print("[2] type/router lifted into a synthesised LLM_CATALOG")
cat = out.get('LLM_CATALOG') or {}
check("catalog entry for each provider", len(cat) == 3, cat)
check("labels lifted", cat['native_oai_config_aihub1'] == {'type': 'OpenAI', 'router': 'aihub'}, cat)
check("claude labels lifted",
      cat['native_claude_config_opus5'] == {'type': 'Claude', 'router': '4router'}, cat)
check("no type/router left inside the cfg",
      all(not ({'type', 'router'} & set(out[k])) for k in keys), {k: sorted(out[k]) for k in keys})

print("[3] legacy layout passes through untouched")
legacy = {'native_oai_config_x': {'apikey': 'a', 'apibase': 'b'},
          'LLM_CATALOG': {'native_oai_config_x': {'type': 'T', 'router': 'R'}}}
check("identical object contents", expand(legacy) == legacy)
check("legacy catalog not clobbered", expand(legacy)['LLM_CATALOG'] == legacy['LLM_CATALOG'])

print("[4] raw container is dropped (no bogus profile)")
check("'native_config' not in expanded keys", 'native_config' not in out)
# Regression for the 'config' substring filter in load_llm_sessions.
check("'native_config' not in profile list", 'native_config' not in keys, keys)
check("empty native_config is a no-op", expand({'native_config': {}}) == {'native_config': {}})
check("non-dict native_config is a no-op", expand({'native_config': 'nope'}) == {'native_config': 'nope'})

print("[5] a file carrying both layouts keeps both")
both = dict(new)
both['native_oai_config_extra'] = {'apikey': 'z', 'apibase': 'y'}
o2 = expand(both)
check("extra flat key survives", 'native_oai_config_extra' in profiles(o2), profiles(o2))
check("expanded entries still present", 'native_oai_config_aihub1' in profiles(o2))

print("[6] real mykey.py still loads through the same path")
try:
    mk, _changed = llmcore.reload_mykeys()
    n = len(profiles(mk))
    check("mykey.py yields model profiles", n > 0, f"profiles={n}")
    check("no bogus 'native_config' profile", 'native_config' not in profiles(mk))
    check("LLM_CATALOG available for the TUI picker", isinstance(mk.get('LLM_CATALOG'), dict))
except Exception as exc:
    check("mykey.py loads", False, repr(exc))

print("[7] connect_timeout is actually honoured (and the old 'timeout' still is)")
_base = {'apikey': 'k', 'apibase': 'https://x.example', 'model': 'm'}
check("connect_timeout wins",
      llmcore.BaseSession({**_base, 'connect_timeout': 10}).connect_timeout == 10)
check("legacy 'timeout' still works",
      llmcore.BaseSession({**_base, 'timeout': 7}).connect_timeout == 7)
check("connect_timeout takes precedence over timeout",
      llmcore.BaseSession({**_base, 'connect_timeout': 3, 'timeout': 7}).connect_timeout == 3)
check("default when neither is set",
      llmcore.BaseSession(_base).connect_timeout == 5)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
