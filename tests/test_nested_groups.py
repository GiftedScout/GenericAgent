# -*- coding: utf-8 -*-
"""Regression tests for the interface-grouped nested mykey.py layout.

The file groups providers by API interface, then vendor, then router;
entries are keyless list items:

    native_oai_config    = { 'OpenAI': { 'aihub': [ {...}, {...} ] } }
    native_claude_config = { 'Claude': { '4router': [ {...} ] } }
    native_chat_config   = { 'Gemini': { 'Google': [ {...} ] } }
    native_image_config  = { 'OpenAI': { 'aihub': [ {...} ] } }

Entries carry NO type/router/api_mode: the group variable name gives
api_mode, and the vendor/router keys give the LLM_CATALOG metadata.
Flat runtime keys are `{group}_{vendor}_{router}_{i}`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llmcore  # noqa: E402

FAIL = 0


def check(name, cond):
    global FAIL
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def test_flatten_list_entries():
    print("1) keyless list entries, meta derived from structure")
    mk = {
        "native_oai_config": {
            "OpenAI": {
                "aihub": [
                    {"name": "x", "apikey": "k", "apibase": "https://b", "model": "m"},
                    {"apikey": "k", "apibase": "https://b", "model": "m2"},
                ]
            },
            "Grok": {
                "fluxionai": [{"apikey": "k", "apibase": "https://f", "model": "g"}]
            },
        },
        "native_claude_config": {
            "Claude": {
                "4router": [{"name": "c", "apikey": "k", "apibase": "https://b", "model": "c"}]
            }
        },
    }
    out = llmcore._expand_nested_groups(mk)
    k0 = "native_oai_config_OpenAI_aihub_0"
    k1 = "native_oai_config_OpenAI_aihub_1"
    kg = "native_oai_config_Grok_fluxionai_0"
    kc = "native_claude_config_Claude_4router_0"
    check("entry 0 flattened", k0 in out)
    check("entry 1 flattened", k1 in out)
    check("second vendor flattened", kg in out)
    check("claude entry flattened", kc in out)
    check("api_mode derived responses", out.get(k0, {}).get("api_mode") == "responses")
    check("api_mode derived claude", out.get(kc, {}).get("api_mode") == "claude")
    check("model preserved", out.get(k0, {}).get("model") == "m")
    check("entry has no type field", "type" not in out.get(k0, {}))
    check("entry has no router field", "router" not in out.get(k0, {}))
    check("group vars dropped", "native_oai_config" not in out and "native_claude_config" not in out)
    cat = out.get("LLM_CATALOG", {})
    check("catalog router from structure", cat.get(k0, {}).get("router") == "aihub")
    check("catalog type from structure", cat.get(k0, {}).get("type") == "OpenAI")


def test_api_mode_explicit_override():
    print("2) explicit api_mode wins over group default")
    mk = {"native_image_config": {"OpenAI": {"aihub": [
        {"apikey": "k", "apibase": "https://b", "model": "img",
         "api_mode": "images/generations"}]}}}
    out = llmcore._expand_nested_groups(mk)
    k = "native_image_config_OpenAI_aihub_0"
    check("image entry flattened", k in out)
    check("explicit api_mode kept", out.get(k, {}).get("api_mode") == "images/generations")


def test_chat_group_default():
    print("3) chat group derives chat_completions")
    mk = {"native_chat_config": {"Gemini": {"Google": [
        {"apikey": "k", "apibase": "https://b", "model": "g"}]}}}
    out = llmcore._expand_nested_groups(mk)
    k = "native_chat_config_Gemini_Google_0"
    check("chat entry flattened", k in out)
    check("api_mode derived chat_completions", out.get(k, {}).get("api_mode") == "chat_completions")


def test_legacy_flat_untouched():
    print("4) legacy flat variables pass through untouched")
    mk = {"native_oai_config_xxx": {"apikey": "k", "model": "m", "apibase": "https://b"}}
    out = llmcore._expand_nested_groups(mk)
    check("legacy flat survives", "native_oai_config_xxx" in out)
    check("no catalog added for flat", "LLM_CATALOG" not in out or not out["LLM_CATALOG"])


def test_single_dict_compat():
    print("5) old single-dict native_config still expands")
    mk = {"native_config": {"aihub1": {
        "apikey": "k", "apibase": "https://b", "model": "m", "api_mode": "responses"}}}
    out = llmcore._expand_mykey_layout(mk)
    check("single-dict entry expanded", "native_oai_config_aihub1" in out)


if __name__ == "__main__":
    test_flatten_list_entries()
    test_api_mode_explicit_override()
    test_chat_group_default()
    test_legacy_flat_untouched()
    test_single_dict_compat()
    print(f"\n{'='*40}\n{'ALL PASSED' if FAIL == 0 else f'{FAIL} FAILED'}")
    sys.exit(1 if FAIL else 0)
