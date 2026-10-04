"""Linux keyboard-shortcut labels; formatting only, bindings stay unchanged."""
from __future__ import annotations


# Modifier display per style. mac uses Apple HIG glyphs; others use words.
_MOD = {
    "ctrl":  "Ctrl",
    "shift": "Shift",
    "alt":   "Alt",
    "meta":  "Alt",
    "super": "Super",
    "cmd":   "Super",
}

# Bare-key display. Arrows / slash are universal; rest mac-glyphs vs words.
_KEY = {
    "enter":     "Enter",
    "tab":       "Tab",
    "escape":    "Esc",
    "esc":       "Esc",
    "backspace": "Backspace",
    "delete":    "Del",
    "space":     "Space",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "slash": "/", "underscore": "_",
}

# Joiner between modifier and key. mac concatenates (⌃B); others use '+'.
_JOIN = "+"


def fmt_key(combo: str) -> str:
    """``"ctrl+b"`` → ``"⌃B"`` (mac) / ``"Ctrl+B"`` (Win/Linux).

    Unknown single-char keys are upper-cased (``"b"`` → ``"B"``);
    multi-char names fall back to the original token unchanged.
    """
    parts = [p.strip() for p in combo.lower().split("+") if p.strip()]
    if not parts:
        return combo
    mods, key = parts[:-1], parts[-1]
    key_disp = _KEY.get(key) or (key.upper() if len(key) == 1 else key)
    mod_disp = [_MOD.get(m, m) for m in mods]
    if not mod_disp:
        return key_disp
    return _JOIN.join(mod_disp) + _JOIN + key_disp


def fmt_keys(*combos: str, sep: str = " / ") -> str:
    """Join multiple combos: ``fmt_keys("ctrl+j", "ctrl+enter")`` →
    ``"Ctrl+J / Ctrl+Enter"`` or ``"⌃J / ⌃⏎"``."""
    return sep.join(fmt_key(c) for c in combos)


# Convenience constants for f-string templates.
CTRL  = _MOD["ctrl"]
SHIFT = _MOD["shift"]
ALT   = _MOD["alt"]
