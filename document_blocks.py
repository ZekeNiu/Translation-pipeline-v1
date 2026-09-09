"""A small Markdown block layer for the structures produced by MinerU."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import re

HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
REFERENCE = re.compile(r"(?i)^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:references|bibliography|works cited|literature cited|参考文献|參考文獻)\s*[:：]?$")
CAPTION = re.compile(r"(?i)^(?:fig(?:ure)?\.?|table|图|表)\s*\d+")


@dataclass(frozen=True)
class DocumentBlock:
    id: str
    kind: str
    text: str


def blocks(markdown: str):
    lines = markdown.splitlines()
    parsed = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        text, kind = [lines[i]], "text"
        stripped = lines[i].strip()
        heading = HEADING.match(stripped)
        if heading:
            kind = "heading"
        elif re.match(r"<table\b", stripped, re.I):
            kind = "table"
            while "</table>" not in text[-1].lower() and i + 1 < len(lines):
                i += 1
                text.append(lines[i])
        elif stripped.startswith(("```", "~~~")):
            kind = "code"
            fence = re.match(r"(`{3,}|~{3,})", stripped)[0]
            while i + 1 < len(lines):
                i += 1
                text.append(lines[i])
                if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", lines[i].strip()):
                    break
        elif stripped.startswith("$$") or stripped.startswith(r"\["):
            kind = "formula"
            close = "$$" if stripped.startswith("$$") else r"\]"
            if close not in stripped[2:]:
                while i + 1 < len(lines):
                    i += 1
                    text.append(lines[i])
                    if close in lines[i]:
                        break
        elif stripped.startswith("!["):
            kind = "image"
        else:
            kind = "caption" if CAPTION.match(stripped) else "text"
            while i + 1 < len(lines) and lines[i + 1].strip():
                nxt = lines[i + 1].strip()
                if HEADING.match(nxt) or nxt.startswith(("![", "```", "~~~", "$$", r"\[")) or re.match(r"<table\b", nxt, re.I):
                    break
                i += 1
                text.append(lines[i])
        parsed.append((kind, "\n".join(text).strip()))
        i += 1
    result, i = [], 0
    while i < len(parsed):
        kind, text = parsed[i]
        heading = HEADING.match(text) if kind == "heading" else None
        if heading and REFERENCE.fullmatch(heading[2]):
            level, reference = len(heading[1]), [text]
            i += 1
            while i < len(parsed):
                next_heading = HEADING.match(parsed[i][1]) if parsed[i][0] == "heading" else None
                if next_heading and len(next_heading[1]) <= level:
                    break
                reference.append(parsed[i][1])
                i += 1
            kind, text = "reference", "\n\n".join(reference)
        else:
            i += 1
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        result.append(DocumentBlock(f"b{len(result):06d}_{digest}", kind, text))
    return result


def split_prose(text, limit):
    """Split at whitespace, never inside math, an image, or an inline tag."""
    protected = {}
    def stash(match):
        key = f"\ue000{len(protected)}\ue001"
        protected[key] = match[0]
        return key
    masked = re.sub(r"\$\$[\s\S]*?\$\$|\$(?:\\.|[^$\n])+\$|\\\[[\s\S]*?\\\]|!\[[^\]]*\]\([^)]+\)|`[^`]+`|<[^>]+>", stash, text)
    sentences = re.split(r"(?<=[.!?。！？])\s+", masked)
    atoms = []
    for sentence in sentences:
        atoms.extend(sentence.split() if len(sentence) > limit else [sentence])
    result, buf, size = [], [], 0
    for atom in atoms:
        if buf and size + len(atom) + 1 > limit:
            result.append(" ".join(buf))
            buf, size = [], 0
        buf.append(atom)
        size += len(atom) + 1
    if buf:
        result.append(" ".join(buf))
    for i, item in enumerate(result):
        for key, value in protected.items():
            item = item.replace(key, value)
        result[i] = item
    return result


def segments(markdown, max_chunk=10000, min_chunk=1800):
    result, current, size = [], [], 0
    previous_kind = ""
    for block in blocks(markdown):
        pieces = split_prose(block.text, max_chunk) if block.kind == "text" and len(block.text) > max_chunk else [block.text]
        for piece in pieces:
            cost = 60 if block.kind in {"table", "formula", "image", "code", "reference"} else len(piece)
            heading_boundary = block.kind == "heading" and piece.startswith(("# ", "## ")) and size >= min_chunk
            if current and (size + cost > max_chunk or heading_boundary) and previous_kind not in {"image", "table"}:
                result.append("\n\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += cost + 2
            previous_kind = block.kind
    if current:
        result.append("\n\n".join(current))
    return result
