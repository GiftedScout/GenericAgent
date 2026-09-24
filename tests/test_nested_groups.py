# -*- coding: utf-8 -*-
"""Regression tests for the interface-grouped nested mykey.py layout.

The file now groups providers by API interface, then vendor, then router:

    native_oai_config    = { 'OpenAI': { 'aihub': { 'aihub0': {...} } }, ... }
    native_claude_config = { 'Claude': { '4router': { 'opus': {...} } }, ... }
    native_chat_config   = { 'Gemini': { 'Google': { 'google': {...} } }, ... }
    native_image_config  = { 'OpenAI': { 'aihub': { 'image2': {...} } }, ... }

llmcore._expand_nested_groups flattens that into the legacy flat keys
(`native_<group>_<entry_key>`) that the runtime / picker already consume.
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


def test_flatten_three_levels():
    print("1) three-level flatten (vendor -> router -> entry)")
    mk = {
        "native_oai_config": {
            "OpenAI": {
                "aihub": {
                    "aihub0": {
                        "name": "x", "apikey": "k", "apibase": "https://b",
                        "model": "m", "type": "OpenAI", "router": "aihub",
                    }
                }
            }
        },
        "native_claude_config": {
            "Claude": {
                "4router": {
                    "opus": {"name": "c", "apikey": "k", "apibase": "https://b", "model": "c"},
                }
            }
        },
    }
    out = llmcore._expand_nested_groups(mk)
    check("oai entry flattened", "native_oai_config_aihub0" in out)
    check("api_mode derived responses", out.get("native_oai_config_aihub0", {}).get("api_mode") == "responses")
    check("model preserved", out.get("native_oai_config_aihub0", {}).get("model") == "m")
    check("apibase preserved", out.get("native_oai_config_aihub0", {}).get("apibase") == "https://b")
    check("claude entry flattened", "native_claude_config_opus" in out)
    check("api_mode derived claude", out.get("native_claude_config_opus", {}).get("api_mode") == "claude")
    check("group var dropped", "native_oai_config" not in out and "native_claude_config" not in out)
    cat = out.get("LLM_CATALOG", {})
    check("catalog has router", cat.get("native_oai_config_aihub0", {}).get("router") == "aihub")
    check("catalog has type", cat.get("native_oai_config_aihub0", {}).get("type") == "OpenAI")


def test_api_mode_explicit_override():
    print("2) explicit api_mode wins over group default")
    mk = {"native_image_config": {"OpenAI": {"aihub": {"image2": {
        "apikey": "k", "apibase": "https://b", "model": "img",
        "api_mode": "images/generations"}}}}}
    out = llmcore._expand_nested_groups(mk)
    check("image entry flattened", "native_image_config_image2" in out)
    check("explicit api_mode kept", out.get("native_image_config_image2", {}).get("api_mode") == "images/generations")


def test_chat_group_default():
    print("3) chat group derives chat_completions")
    mk = {"native_chat_config": {"Gemini": {"Google": {"google": {
        "apikey": "k", "apibase": "https://b", "model": "g"}}}}}
    out = llmcore._expand_nested_groups(mk)
    check("chat entry flattened", "native_chat_config_google" in out)
    check("api_mode derived chat_completions", out.get("native_chat_config_google", {}).get("api_mode") == "chat_completions")


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
    test_flatten_three_levels()
    test_api_mode_explicit_override()
    test_chat_group_default()
    test_legacy_flat_untouched()
    test_single_dict_compat()
    print(f"\n{'='*40}\n{'ALL PASSED' if FAIL == 0 else f'{FAIL} FAILED'}")
    sys.exit(1 if FAIL else 0)
