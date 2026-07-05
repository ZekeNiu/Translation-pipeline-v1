from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import re

from latex_utils import MATH_SPAN_RE, latex_formula_to_readable, latex_to_readable_text


SUPERSCRIPT_MAP = str.maketrans(
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
        "=": "⁼",
        "(": "⁽",
        ")": "⁾",
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
        "n": "ⁿ",
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

SUBSCRIPT_MAP = str.maketrans(
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
        "=": "₌",
        "(": "₍",
        ")": "₎",
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

SUPERSCRIPT_CHARS = "".join(str(v) for v in SUPERSCRIPT_MAP.values())
SUBSCRIPT_CHARS = "".join(str(v) for v in SUBSCRIPT_MAP.values())
SCRIPT_RE = re.compile(f"([{re.escape(SUPERSCRIPT_CHARS)}]+|[{re.escape(SUBSCRIPT_CHARS)}]+)")
HTML_SCRIPT_RE = re.compile(r"<\s*(sup|sub)\b[^>]*>(.*?)<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
INLINE_TAG_RE = re.compile(r"<\s*(/?)\s*(sup|sub|i|em|b|strong)\b[^>]*>", re.IGNORECASE)
TEXT_MARKER_RE = re.compile(r"^(?P<base>.+?)\s*\^\s*\{?\s*(?P<marker>[A-Za-z0-9,+\- ]{1,12})\s*\}?$")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z-]*")


@dataclass(frozen=True)
class StyledRun:
    text: str
    bold: bool = False
    italic: bool = False
    superscript: bool = False
    subscript: bool = False


def to_superscript(text: str) -> str:
    return text.translate(SUPERSCRIPT_MAP)


def to_subscript(text: str) -> str:
    return text.translate(SUBSCRIPT_MAP)


def strip_math_delimiters(expr: str) -> str:
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


def _is_display_math(span: str) -> bool:
    stripped = span.strip()
    return stripped.startswith("$$") or stripped.startswith(r"\[")


def _has_translatable_words(text: str) -> bool:
    words = WORD_RE.findall(text)
    ordinary = [w for w in words if len(w) >= 3 and not w.isupper()]
    if not ordinary:
        return False
    if len(ordinary) >= 2:
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", text)
    return bool(ordinary[0][0].isupper()) and not re.search(r"\d", compact)


def _is_plain_text_math(inner: str) -> bool:
    if "\\" in inner:
        return False
    if re.search(r"[=<>+\u00d7*/]|_", inner):
        return False
    return _has_translatable_words(inner)


def _safe_readable_formula(span: str) -> str:
    readable = latex_formula_to_readable(span)
    if not readable:
        return span
    if _is_display_math(span):
        return span
    if "\\" in readable or "{" in readable or "}" in readable:
        return span
    if len(readable) > 120:
        return span
    return readable


def _protect_math_span(span: str, store: Callable[[str, str], str]) -> str:
    inner = strip_math_delimiters(span)
    marker = TEXT_MARKER_RE.match(inner)
    if marker and _has_translatable_words(marker.group("base")):
        return marker.group("base").strip() + store("SUP", to_superscript(marker.group("marker").strip()))

    if _is_plain_text_math(inner):
        return inner.strip()

    return store("MATH", _safe_readable_formula(span))


def _protect_html_script(match: re.Match, store: Callable[[str, str], str]) -> str:
    tag = match.group(1).lower()
    content = normalize_inline_output(match.group(2))
    if tag == "sup":
        return store("SUP", to_superscript(re.sub(r"\s+", "", content)))
    return store("SUB", to_subscript(re.sub(r"\s+", "", content)))


def protect_inline_semantics(text: str, store: Callable[[str, str], str]) -> str:
    text = HTML_SCRIPT_RE.sub(lambda m: _protect_html_script(m, store), text)
    return MATH_SPAN_RE.sub(lambda m: _protect_math_span(m.group(0), store), text)


def normalize_inline_output(text: str) -> str:
    text = re.sub(r"&lt;\s*(/?)\s*(sup|sub|i|em|b|strong)\s*&gt;", r"<\1\2>", text, flags=re.IGNORECASE)
    text = HTML_SCRIPT_RE.sub(
        lambda m: to_superscript(re.sub(r"\s+", "", m.group(2)))
        if m.group(1).lower() == "sup"
        else to_subscript(re.sub(r"\s+", "", m.group(2))),
        text,
    )
    return latex_to_readable_text(text)


def find_inline_artifacts(text: str) -> list[str]:
    artifacts: list[str] = []
    if re.search(r"<\s*/?\s*(sup|sub)\b", text, flags=re.IGNORECASE):
        artifacts.append("html_script")
    if re.search(r"\\(?:mathrm|operatorname|frac|dot|text|mathbf|mathit)\b", text):
        artifacts.append("latex_command")
    if re.search(r"(?<!\\)\$(?!\$)(?:\\.|[^\n$])+(?<!\\)\$", text):
        artifacts.append("math_span")
    return artifacts


def _emit_script_runs(text: str, style: dict[str, bool]) -> Iterator[StyledRun]:
    pos = 0
    for match in SCRIPT_RE.finditer(text):
        if match.start() > pos:
            yield StyledRun(text[pos : match.start()], **style)
        part = match.group(0)
        run_style = dict(style)
        if all(ch in SUPERSCRIPT_CHARS for ch in part):
            run_style["superscript"] = True
            run_style["subscript"] = False
        elif all(ch in SUBSCRIPT_CHARS for ch in part):
            run_style["subscript"] = True
            run_style["superscript"] = False
        yield StyledRun(part, **run_style)
        pos = match.end()
    if pos < len(text):
        yield StyledRun(text[pos:], **style)


def iter_styled_runs(text: str) -> Iterator[StyledRun]:
    text = normalize_inline_output(text)
    pos = 0
    style = {"bold": False, "italic": False, "superscript": False, "subscript": False}
    for match in INLINE_TAG_RE.finditer(text):
        if match.start() > pos:
            yield from _emit_script_runs(text[pos : match.start()], style)
        closing = bool(match.group(1))
        tag = match.group(2).lower()
        enabled = not closing
        if tag in {"b", "strong"}:
            style["bold"] = enabled
        elif tag in {"i", "em"}:
            style["italic"] = enabled
        elif tag == "sup":
            style["superscript"] = enabled
            if enabled:
                style["subscript"] = False
        elif tag == "sub":
            style["subscript"] = enabled
            if enabled:
                style["superscript"] = False
        pos = match.end()
    if pos < len(text):
        yield from _emit_script_runs(text[pos:], style)
