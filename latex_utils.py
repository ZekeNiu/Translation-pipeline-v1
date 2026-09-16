from __future__ import annotations

import re


SUB = str.maketrans(
    {
        "0": "₀",
        "1": "₁",
        "2": "₂",
        "3": "₃",
        "4": "₄",
        "5": "₅",
        "6": "₆",
        "7": "₇",
        "8": "₈",
        "9": "₉",
        "+": "₊",
        "-": "₋",
        "a": "ₐ",
        "e": "ₑ",
        "h": "ₕ",
        "i": "ᵢ",
        "j": "ⱼ",
        "k": "ₖ",
        "l": "ₗ",
        "m": "ₘ",
        "n": "ₙ",
        "o": "ₒ",
        "p": "ₚ",
        "r": "ᵣ",
        "s": "ₛ",
        "t": "ₜ",
        "u": "ᵤ",
        "v": "ᵥ",
        "x": "ₓ",
    }
)

SUP = str.maketrans(
    {
        "0": "⁰",
        "1": "¹",
        "2": "²",
        "3": "³",
        "4": "⁴",
        "5": "⁵",
        "6": "⁶",
        "7": "⁷",
        "8": "⁸",
        "9": "⁹",
        "+": "⁺",
        "-": "⁻",
        "n": "ⁿ",
        "a": "ᵃ",
        "b": "ᵇ",
        "c": "ᶜ",
        "d": "ᵈ",
        "e": "ᵉ",
        "f": "ᶠ",
        "g": "ᵍ",
        "h": "ʰ",
        "i": "ⁱ",
        "j": "ʲ",
        "k": "ᵏ",
        "l": "ˡ",
        "m": "ᵐ",
        "o": "ᵒ",
        "p": "ᵖ",
        "r": "ʳ",
        "s": "ˢ",
        "t": "ᵗ",
        "u": "ᵘ",
        "v": "ᵛ",
        "w": "ʷ",
        "x": "ˣ",
        "y": "ʸ",
        "z": "ᶻ",
    }
)

GREEK = {
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\Delta": "Δ",
    r"\epsilon": "ε",
    r"\theta": "θ",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\nu": "ν",
    r"\pi": "π",
    r"\rho": "ρ",
    r"\sigma": "σ",
    r"\Sigma": "Σ",
    r"\tau": "τ",
    r"\omega": "ω",
}

SYMBOLS = {
    r"\geq": "≥",
    r"\ge": "≥",
    r"\leq": "≤",
    r"\le": "≤",
    r"\neq": "≠",
    r"\approx": "≈",
    r"\sim": "~",
    r"\pm": "±",
    r"\times": "×",
    r"\cdot": "·",
    r"\circ": "°",
    r"\%": "%",
    r"\lt": "<",
    r"\gt": ">",
    r"\quad": " ",
    r"\,": " ",
    r"\:": " ",
    r"\;": " ",
}

TEXT_SYMBOLS = {
    r"\textregistered": "®",
    r"\textcopyright": "©",
    r"\texttrademark": "™",
    r"\textdegree": "°",
    r"\degree": "°",
}

MATH_SPAN_RE = re.compile(
    r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|(?<!\\)\$(?!\$)(?:\\.|[^\n$])+(?<!\\)\$",
    re.DOTALL,
)


def _strip_math_delimiters(expr: str) -> str:
    expr = expr.strip()
    if expr.startswith("$$") and expr.endswith("$$"):
        return expr[2:-2].strip()
    if expr.startswith("$") and expr.endswith("$"):
        return expr[1:-1].strip()
    if expr.startswith(r"\[") and expr.endswith(r"\]"):
        return expr[2:-2].strip()
    if expr.startswith(r"\(") and expr.endswith(r"\)"):
        return expr[2:-2].strip()
    return expr


def _compact_letters(text: str) -> str:
    parts = text.strip().split()
    if len(parts) > 1 and all(re.fullmatch(r"[A-Za-z]", p) for p in parts):
        return "".join(parts)
    return text.strip()


def _replace_group_command(expr: str, command: str) -> str:
    pattern = re.compile(rf"\\{command}\*?\s*\{{([^{{}}]*)\}}")
    for _ in range(8):
        new = pattern.sub(lambda m: _compact_letters(latex_formula_to_readable(m.group(1), strip_delimiters=False)), expr)
        if new == expr:
            break
        expr = new
    return expr


def _replace_frac(expr: str) -> str:
    pattern = re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
    for _ in range(8):
        new = pattern.sub(
            lambda m: f"{latex_formula_to_readable(m.group(1), strip_delimiters=False)}/"
            f"{latex_formula_to_readable(m.group(2), strip_delimiters=False)}",
            expr,
        )
        if new == expr:
            break
        expr = new
    return expr


def _replace_text_script(expr: str, command: str, kind: str) -> str:
    pattern = re.compile(rf"\\{command}\s*\{{([^{{}}]*)\}}")
    trans = SUB if kind == "_" else SUP
    for _ in range(8):
        new = pattern.sub(
            lambda m: re.sub(r"\s+", "", latex_formula_to_readable(m.group(1), strip_delimiters=False)).translate(trans),
            expr,
        )
        if new == expr:
            break
        expr = new
    return expr


def _format_script(content: str, kind: str) -> str:
    trans = SUB if kind == "_" else SUP
    content = latex_formula_to_readable(content, strip_delimiters=False)
    content = re.sub(r"\s+", "", content)
    match = re.fullmatch(r"([0-9+\-]+)(max|min|avg|mean)", content)
    if match:
        return match.group(1).translate(trans) + match.group(2)
    if re.fullmatch(r"[0-9+\-a-z]+", content):
        return content.translate(trans)
    if kind == "_" and re.fullmatch(r"[A-Za-z]*[0-9][A-Za-z0-9]*", content):
        return re.sub(r"\d+", lambda m: m.group(0).translate(trans), content)
    match = re.fullmatch(r"([0-9+\-]+)([A-Za-z]+)", content)
    if match:
        return match.group(1).translate(trans) + match.group(2)
    return content


def latex_formula_to_readable(expr: str, strip_delimiters: bool = True) -> str:
    original = expr
    try:
        if strip_delimiters:
            expr = _strip_math_delimiters(expr)

        expr = expr.replace("\n", " ")
        # OCR sometimes adds redundant plain groups such as {{t h}}.
        for _ in range(4):
            expr = re.sub(r'\{\s*\{([A-Za-z0-9\s]+)\}\s*\}', r'{\1}', expr)
        expr = re.sub(r"\\(?:left|right)\b", "", expr)
        expr = _replace_frac(expr)
        expr = re.sub(
            r"\\dot\s*\{?\s*([A-Za-z])\s*\}?",
            lambda m: m.group(1) + "\u0307",
            expr,
        )
        for key, value in TEXT_SYMBOLS.items():
            expr = expr.replace(key, value)
        group_commands = ("operatorname", "mathrm", "mathbf", "mathit", "mathsf", "boldsymbol", "text", "mbox", "textit", "textbf")
        for command in group_commands:
            expr = _replace_group_command(expr, command)
        expr = _replace_text_script(expr, "textsuperscript", "^")
        expr = _replace_text_script(expr, "textsubscript", "_")
        for key, value in {**GREEK, **SYMBOLS}.items():
            expr = expr.replace(key, value)

        expr = re.sub(r"_\s*\{([^{}]*)\}", lambda m: _format_script(m.group(1), "_"), expr)
        expr = re.sub(r"_\s*([A-Za-z0-9+\-])", lambda m: _format_script(m.group(1), "_"), expr)
        expr = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: _format_script(m.group(1), "^"), expr)
        expr = re.sub(r"\^\s*([A-Za-z0-9+\-])", lambda m: m.group(1).translate(SUP), expr)
        # Script groups can previously have blocked the enclosing font wrapper.
        for command in group_commands:
            expr = _replace_group_command(expr, command)

        expr = re.sub(r"[{}]", "", expr)
        # TeX ignores ordinary spaces in numeric math tokens; OCR often inserts them.
        expr = re.sub(r"(?<=\d)\s+(?=[\d.])|(?<=\.)\s+(?=\d)", "", expr)
        expr = re.sub(r"(?<=\d)\s+(?=°)", "", expr)
        expr = re.sub(r"\\(?=\s|[,.;:])", "", expr)
        expr = re.sub(r"\s*/\s*", "/", expr)
        expr = re.sub(r"\s+([,\.;:\]\)])", r"\1", expr)
        expr = re.sub(r"([\[\(])\s+", r"\1", expr)
        expr = re.sub(r"(?<=V\u0307)\s+(?=O)", "", expr)
        expr = re.sub(r"(?<=[A-Za-z])\s+(?=[₀₁₂₃₄₅₆₇₈₉])", "", expr)
        expr = re.sub(r"(?<=[₀₁₂₃₄₅₆₇₈₉])\s+(?=max\b)", "", expr)
        expr = re.sub(r"\s+", " ", expr).strip()
        return expr or original
    except Exception:
        return original


def latex_to_readable_text(text: str) -> str:
    return MATH_SPAN_RE.sub(lambda m: latex_formula_to_readable(m.group(0)), text)
