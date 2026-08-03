"""Terminal-friendly LaTeX math adaptation for Markdown frontends.

Rich Markdown deliberately has no math renderer.  This module converts the
small, common LaTeX vocabulary emitted by LLMs to readable Unicode before Rich
parses the message.  Display math becomes a ``text`` fence (so Markdown cannot
reinterpret underscores); inline math becomes inline code.  Existing Markdown
code spans and fences are protected byte-for-byte.

This is intentionally a loss-tolerant formatter, not a TeX engine: unknown
commands are kept verbatim rather than silently discarded.
"""
from __future__ import annotations

import re
from typing import Callable


_SYMBOLS = {
    # Greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ϵ", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "ϕ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # Operators and relations
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "iiint": "∭",
    "oint": "∮", "partial": "∂", "nabla": "∇", "infty": "∞",
    "pm": "±", "mp": "∓", "times": "×", "cdot": "·", "ast": "∗",
    "div": "÷", "circ": "∘", "bullet": "•", "oplus": "⊕", "otimes": "⊗",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "neq": "≠",
    "ne": "≠", "approx": "≈", "sim": "∼", "simeq": "≃", "equiv": "≡",
    "propto": "∝", "ll": "≪", "gg": "≫", "in": "∈", "notin": "∉",
    "subset": "⊂", "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃",
    "rightarrow": "→", "to": "→", "leftarrow": "←",
    "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "Leftrightarrow": "⇔", "mapsto": "↦",
    "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "angle": "∠", "perp": "⊥", "parallel": "∥",
    # Named functions
    "log": "log", "ln": "ln", "exp": "exp", "max": "max", "min": "min",
    "sin": "sin", "cos": "cos", "tan": "tan", "arcsin": "arcsin",
    "arccos": "arccos", "arctan": "arctan", "det": "det", "lim": "lim",
}

_MATH_STYLE = {
    "mathcal": {
        "A": "𝒜", "B": "ℬ", "C": "𝒞", "D": "𝒟", "E": "ℰ", "F": "ℱ",
        "G": "𝒢", "H": "ℋ", "I": "ℐ", "J": "𝒥", "K": "𝒦", "L": "ℒ",
        "M": "ℳ", "N": "𝒩", "O": "𝒪", "P": "𝒫", "Q": "𝒬", "R": "ℛ",
        "S": "𝒮", "T": "𝒯", "U": "𝒰", "V": "𝒱", "W": "𝒲", "X": "𝒳",
        "Y": "𝒴", "Z": "𝒵",
    },
}

_SUPERSCRIPT = str.maketrans({
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵",
    "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻",
    "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
})
_SUBSCRIPT = str.maketrans({
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅",
    "6": "₆", "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋",
    "=": "₌", "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ",
    "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ",
    "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ",
    "v": "ᵥ", "x": "ₓ",
})

_SPACING = {",": " ", ";": " ", ":": " ", "!": "", "quad": "  ", "qquad": "    "}
_STYLE_COMMANDS = {"mathrm", "textrm", "text", "mathbf", "mathit", "mathsf", "mathtt", "rm", "bf", "it"}
_DELIMITER_COMMANDS = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr"}
_ESCAPED = {"{": "{", "}": "}", "_": "_", "^": "^", "%": "%", "$": "$", "#": "#", "&": "&", "|": "|", " ": " "}


def _unicode_script(value: str, table: dict[int, str], marker: str) -> str:
    """Use Unicode scripts only when every non-space character is supported."""
    compact = value.replace(" ", "")
    converted = compact.translate(table)
    if compact and all(ord(ch) in table for ch in compact):
        return converted
    if len(compact) == 1:
        return marker + value
    opener, closer = ("₍", "₎") if marker == "_" else ("⁽", "⁾")
    return opener + value + closer


class _LatexReader:
    def __init__(self, source: str):
        self.source = source
        self.pos = 0

    def render(self, stop: str | None = None) -> str:
        out: list[str] = []
        while self.pos < len(self.source):
            ch = self.source[self.pos]
            if stop and ch == stop:
                self.pos += 1
                break
            if ch == "\\":
                out.append(self._command())
            elif ch in "_^":
                self.pos += 1
                value = self._atom()
                out.append(_unicode_script(value, _SUBSCRIPT if ch == "_" else _SUPERSCRIPT, ch))
            elif ch == "{":
                self.pos += 1
                out.append(self.render("}"))
            elif ch == "}":
                # Unbalanced closing braces are safer to preserve.
                self.pos += 1
                out.append(ch)
            elif ch == "&":
                self.pos += 1  # alignment marker in aligned/cases environments
            elif ch == "~":
                self.pos += 1
                out.append(" ")
            else:
                self.pos += 1
                out.append(ch)
        return "".join(out)

    def _atom(self) -> str:
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1
        if self.pos >= len(self.source):
            return ""
        if self.source[self.pos] == "{":
            self.pos += 1
            return self.render("}").strip()
        if self.source[self.pos] == "\\":
            return self._command().strip()
        ch = self.source[self.pos]
        self.pos += 1
        return ch

    def _required_group(self) -> str | None:
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1
        if self.pos >= len(self.source) or self.source[self.pos] != "{":
            return None
        self.pos += 1
        return self.render("}")

    def _command(self) -> str:
        self.pos += 1
        if self.pos >= len(self.source):
            return "\\"
        if self.source[self.pos] == "\\":
            self.pos += 1
            return "\n"
        if not self.source[self.pos].isalpha():
            symbol = self.source[self.pos]
            self.pos += 1
            if symbol in _SPACING:
                return _SPACING[symbol]
            return _ESCAPED.get(symbol, "\\" + symbol)

        start = self.pos
        while self.pos < len(self.source) and self.source[self.pos].isalpha():
            self.pos += 1
        command = self.source[start:self.pos]

        if command in _SYMBOLS:
            return _SYMBOLS[command]
        if command in _SPACING:
            return _SPACING[command]
        if command in _DELIMITER_COMMANDS:
            return ""
        if command in {"begin", "end"}:
            environment = self._required_group()
            return "" if environment in {"aligned", "align", "align*", "equation", "equation*", "gathered"} else ("\\" + command + "{" + (environment or "") + "}")
        if command == "operatorname":
            return self._required_group() or "operatorname"
        if command in _STYLE_COMMANDS:
            return self._required_group() or ""
        if command == "mathcal":
            value = self._required_group()
            if value is None:
                value = self._atom()
            return "".join(_MATH_STYLE["mathcal"].get(ch, ch) for ch in value)
        if command in {"frac", "dfrac", "tfrac"}:
            numerator = self._required_group()
            denominator = self._required_group()
            if numerator is None or denominator is None:
                return "\\" + command
            left = numerator if len(numerator) == 1 else f"({numerator})"
            right = denominator if len(denominator) == 1 else f"({denominator})"
            return f"{left}⁄{right}"
        if command == "sqrt":
            # Ignore an optional root index for now, but preserve it visibly.
            index = ""
            if self.pos < len(self.source) and self.source[self.pos] == "[":
                end = self.source.find("]", self.pos + 1)
                if end != -1:
                    index = self.source[self.pos + 1:end]
                    self.pos = end + 1
            value = self._required_group()
            return (index.translate(_SUPERSCRIPT) if index else "") + "√(" + (value or "") + ")"

        # Unknown TeX is displayed, not dropped: correctness beats prettiness.
        return "\\" + command


def latex_to_unicode(source: str) -> str:
    """Convert common LaTeX math syntax to loss-tolerant terminal Unicode."""
    rendered = _LatexReader(source.strip()).render()
    rendered = re.sub(r"[ \t]+", " ", rendered)
    rendered = re.sub(r" *\n *", "\n", rendered)
    return rendered.strip()


def _protect_code(markdown: str) -> tuple[str, list[str]]:
    """Replace fenced and inline Markdown code with collision-resistant tokens."""
    saved: list[str] = []
    token_prefix = "\x00GA_CODE_"

    def save(value: str) -> str:
        token = f"{token_prefix}{len(saved)}\x00"
        saved.append(value)
        return token

    # Fences first. The usual equal-length closing fence is protected here.
    fence_re = re.compile(
        r"(?ms)^[ ]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ ]{0,3}(?P=fence)[ \t]*(?:\n|$)"
    )
    protected = fence_re.sub(lambda match: save(match.group(0)), markdown)
    # Protect indented code before inline spans. A math-looking shell/Python
    # example must remain byte-for-byte unchanged just like fenced code.
    indented_re = re.compile(r"(?m)(?:(?<=\n)|\A)(?:(?: {4}|\t)[^\n]*(?:\n|\Z))+")
    protected = indented_re.sub(lambda match: save(match.group(0)), protected)
    inline_re = re.compile(r"(?s)(?P<ticks>`+)(?!`)(.+?)(?<!`)(?P=ticks)(?!`)")
    protected = inline_re.sub(lambda match: save(match.group(0)), protected)
    return protected, saved


def _restore_code(markdown: str, saved: list[str]) -> str:
    for index, value in enumerate(saved):
        markdown = markdown.replace(f"\x00GA_CODE_{index}\x00", value)
    return markdown


def adapt_math_markdown(markdown: str) -> str:
    """Adapt TeX math delimiters in Markdown for a Unicode terminal.

    Supported delimiters: ``\\[...\\]``, ``$$...$$``, ``\\(...\\)`` and
    conservative single-dollar inline math. Existing code is left untouched.
    """
    if not markdown or not any(marker in markdown for marker in ("\\[", "$$", "\\(", "$")):
        return markdown

    protected, saved = _protect_code(markdown)

    def display(match: re.Match[str]) -> str:
        formula = latex_to_unicode(match.group(1))
        return f"\n\n```text\n{formula}\n```\n\n"

    def inline(match: re.Match[str]) -> str:
        formula = latex_to_unicode(match.group(1))
        return f"`{formula}`"

    protected = re.sub(r"(?s)\\\[(.+?)\\\]", display, protected)
    protected = re.sub(r"(?s)(?<!\\)\$\$(.+?)(?<!\\)\$\$", display, protected)
    protected = re.sub(r"(?s)\\\((.+?)\\\)", inline, protected)
    # Avoid common currency prose: delimiters cannot touch whitespace, and a
    # dollar preceded/followed by a digit is treated as money rather than math.
    protected = re.sub(
        r"(?s)(?<![\\\d$])\$(?![$\s])(.+?)(?<![\\\s])\$(?![\d$])",
        inline,
        protected,
    )
    return _restore_code(protected, saved)
