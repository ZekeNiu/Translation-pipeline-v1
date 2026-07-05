"""Translation Pipeline v2.1.

High-quality MinerU markdown translation with structure protection,
OpenAI-compatible provider selection, and resumable chunk cache.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from inline_semantics import find_inline_artifacts, normalize_inline_output, protect_inline_semantics
from mineru_sidecar import load_mineru_sidecar, strip_excluded_lines, write_sidecar_summary
from table_utils import html_table_blocks, iter_cells, parse_html_tables, render_html_table


# -- Config -----------------------------------------------------------
def load_env(override: bool = False):
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            key = k.strip()
            if override or key not in os.environ:
                os.environ[key] = v.strip()


load_env()

OUTPUT_ROOT = Path(__file__).parent / "translations"
OUTPUT_ROOT.mkdir(exist_ok=True)


@dataclass(frozen=True)
class ProviderConfig:
    provider_name: str
    base_url: str
    api_key_env: str
    api_key: str
    model: str
    temperature: float = 0.1
    timeout: int = 180

    def endpoint(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if not base:
            raise ValueError("Base URL is empty. Please choose a provider or enter a custom URL.")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


PROVIDER_PRESETS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-flash",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4.1-mini",
    },
    "qwen": {
        "label": "Qwen / DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "model": "qwen-plus",
    },
    "gemini": {
        "label": "Gemini OpenAI-compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
        "model": "gemini-2.5-flash",
    },
    "siliconflow": {
        "label": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "model": "Qwen/Qwen2.5-72B-Instruct",
    },
    "custom": {
        "label": "Custom OpenAI-compatible",
        "base_url": "",
        "api_key_env": "AI_API_KEY",
        "model": "",
    },
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def resolve_provider_config(
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: int | None = None,
) -> ProviderConfig:
    provider_name = (provider or os.environ.get("AI_PROVIDER") or "deepseek").strip().lower()
    preset = PROVIDER_PRESETS.get(provider_name, PROVIDER_PRESETS["custom"])
    key_env = (api_key_env or os.environ.get("AI_API_KEY_ENV") or preset["api_key_env"]).strip()

    env_model = os.environ.get("AI_MODEL") or os.environ.get(f"{provider_name.upper()}_MODEL")
    if provider_name == "deepseek":
        env_model = os.environ.get("DEEPSEEK_MODEL") or env_model

    resolved_base_url = (base_url or os.environ.get("AI_BASE_URL") or preset["base_url"]).strip()
    resolved_model = (model or env_model or preset["model"]).strip()
    resolved_key = (api_key or os.environ.get("AI_API_KEY") or os.environ.get(key_env, "")).strip()
    resolved_temperature = temperature if temperature is not None else _env_float("AI_TEMPERATURE", 0.1)
    resolved_timeout = timeout if timeout is not None else _env_int("AI_TIMEOUT", 180)

    return ProviderConfig(
        provider_name=provider_name,
        base_url=resolved_base_url,
        api_key_env=key_env,
        api_key=resolved_key,
        model=resolved_model,
        temperature=resolved_temperature,
        timeout=resolved_timeout,
    )


# -- Source normalization and placeholders ---------------------------
REFERENCE_HEADING_RE = re.compile(
    r"(?im)^#{1,6}\s*(references|bibliography|works\s+cited|literature\s+cited|参考文献|參考文獻)\s*$"
)
REFERENCE_ITEM_RE = re.compile(r"^\s*(?:\[(?P<bracket>\d{1,4})\]|(?P<dot>\d{1,4})\.)\s+")
TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")


def normalize_source_text(text: str) -> str:
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text


def normalize_abstract_heading(text: str) -> str:
    text = re.sub(r"(?im)^abstract\s*$", "## 摘要", text)
    text = re.sub(r"(?im)^abstract\s+(.+)$", r"## 摘要\n\n\1", text)
    return text


def split_references(md_text: str) -> tuple[str, str]:
    match = REFERENCE_HEADING_RE.search(md_text)
    if not match:
        return md_text, ""
    body = md_text[: match.start()].rstrip()
    refs = md_text[match.start() :].strip() + "\n"
    return body, refs


def normalize_references(refs: str) -> tuple[str, dict]:
    refs = normalize_inline_output(normalize_source_text(refs))
    lines = refs.splitlines()
    if not lines:
        return "", {"entries": 0, "continuation_lines_merged": 0, "numbering_jumps": []}

    heading = lines[0].strip()
    entries: list[str] = []
    preamble: list[str] = []
    current: list[str] = []
    current_number: int | None = None
    previous_number: int | None = None
    continuation_lines_merged = 0
    numbering_jumps: list[dict[str, int]] = []

    def flush_current():
        nonlocal current
        if current:
            entries.append(re.sub(r"\s+", " ", " ".join(current)).strip())
            current = []

    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        match = REFERENCE_ITEM_RE.match(line)
        if match:
            flush_current()
            number = int(match.group("bracket") or match.group("dot"))
            if previous_number is not None and number != previous_number + 1:
                numbering_jumps.append({"from": previous_number, "to": number})
            previous_number = number
            current_number = number
            current = [line]
            continue
        if current_number is None:
            preamble.append(line)
        else:
            current.append(line)
            continuation_lines_merged += 1
    flush_current()

    normalized_lines = [heading]
    if preamble:
        normalized_lines.extend(["", re.sub(r"\s+", " ", " ".join(preamble)).strip()])
    if entries:
        normalized_lines.append("")
        normalized_lines.extend(entries)
    normalized = "\n\n".join(part for part in normalized_lines if part).strip() + "\n"
    report = {
        "entries": len(entries),
        "continuation_lines_merged": continuation_lines_merged,
        "numbering_jumps": numbering_jumps,
    }
    return normalized, report


def _store_placeholder(kind: str, value: str, placeholders: dict[str, str]) -> str:
    token = f"[[[TP_{kind}_{len(placeholders):04d}]]]"
    placeholders[token] = value
    return token


def protect_fragments(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def protect_table(match):
        return _store_placeholder("TABLE", match.group(0), placeholders)

    def protect_image(match):
        return _store_placeholder("IMAGE", match.group(0), placeholders)

    text = TABLE_RE.sub(protect_table, text)
    text = IMAGE_RE.sub(protect_image, text)

    text = protect_inline_semantics(text, lambda kind, value: _store_placeholder(kind, value, placeholders))
    return text, placeholders


def restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
    for token, value in placeholders.items():
        text = text.replace(token, value)
    return normalize_inline_output(text)


def clean_translation_text(text: str) -> str:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:markdown|md|text)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text


def clean_latex(text: str) -> str:
    """Kept for compatibility; intentionally non-destructive."""
    return text


# -- OpenAI-compatible chat API --------------------------------------
def call_chat_completion(config: ProviderConfig, messages, timeout: int | None = None) -> str:
    if not config.api_key:
        raise ValueError(
            f"Missing API key for {config.provider_name}. "
            f"Set {config.api_key_env} or enter an API key in the GUI."
        )
    if not config.model:
        raise ValueError("Model is empty. Please choose or enter a model name.")

    import requests as req

    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    payload = {"model": config.model, "messages": messages, "temperature": config.temperature}
    resp = req.post(config.endpoint(), headers=headers, json=payload, timeout=timeout or config.timeout)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"].get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
            else:
                parts.append(str(item))
        content = "".join(parts)
    return str(content).strip()


def list_available_models(config: ProviderConfig) -> list[str]:
    if not config.api_key:
        raise ValueError(f"Missing API key for {config.provider_name}.")
    import requests as req

    base = config.base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    resp = req.get(
        f"{base}/models",
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout=min(config.timeout, 30),
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", data if isinstance(data, list) else [])
    models = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, str):
            models.append(item)
    return sorted(set(models))


def call_deepseek(messages, timeout=180):
    return call_chat_completion(resolve_provider_config("deepseek", timeout=timeout), messages, timeout=timeout)


# -- Prompts ----------------------------------------------------------
SYSPROMPT = """You are a British-Chinese bilingual scientific translator specializing in academic papers and books.

Rules:
1. Translate English into polished, professional Simplified Chinese.
2. Preserve paragraph boundaries and heading markers.
3. Preserve every placeholder token like [[[TP_MATH_0000]]] exactly.
4. Keep citation numbers such as [1], [1, 2], [1-3] exactly.
5. Keep author, institution, journal, and reference-list entries in English.
6. Keep established acronyms in English unless the source explains them.
7. Return only the translated text, with no explanations or notes."""

TABLE_SYSPROMPT = """Translate table-cell text from English into concise Simplified Chinese.

Rules:
1. Return only a JSON array of strings.
2. The output array length and order must exactly match the input array.
3. Preserve numbers, units, formulas, citation numbers, acronyms, and placeholder tokens exactly.
4. Do not add explanations."""


# -- Segmentation -----------------------------------------------------
def _split_long_block(text: str, max_chunk: int) -> list[str]:
    if len(text) <= max_chunk:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    buf = ""
    paragraphs = re.split(r"\n\n+", text)
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > max_chunk:
            pieces = re.split(r"(?<=[.!?。！？])\s+", paragraph)
        else:
            pieces = [paragraph]
        for piece in pieces:
            if not piece:
                continue
            if len(piece) > max_chunk:
                hard_parts = [piece[i : i + max_chunk] for i in range(0, len(piece), max_chunk)]
            else:
                hard_parts = [piece]
            for part in hard_parts:
                if len(buf) + len(part) + 2 <= max_chunk:
                    buf = f"{buf}\n\n{part}".strip()
                else:
                    if buf:
                        chunks.append(buf)
                    buf = part
    if buf:
        chunks.append(buf)
    return chunks


def _merge_small_segments(chunks: list[str], min_chunk: int, max_chunk: int) -> list[str]:
    merged: list[str] = []
    buf = ""
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if not buf:
            buf = chunk
            continue
        if len(buf) < min_chunk and len(buf) + len(chunk) + 2 <= max_chunk:
            buf = f"{buf}\n\n{chunk}"
        else:
            merged.append(buf)
            buf = chunk
    if buf:
        merged.append(buf)
    return merged


def split_body_into_segments(body: str, max_chunk: int = 10000, min_chunk: int = 1800) -> list[str]:
    segs = re.split(r"(?m)^(?=#{1,2} )", body)
    chunks: list[str] = []
    for seg in segs:
        seg = seg.strip()
        if not seg:
            continue
        if len(seg) <= max_chunk:
            chunks.append(seg)
            continue
        subs = re.split(r"(?m)^(?=### )", seg)
        for sub in subs:
            chunks.extend(_split_long_block(sub, max_chunk))
    return _merge_small_segments(chunks, min_chunk, max_chunk)


def segment_document(md_text, max_chunk=5000):
    body, refs = split_references(md_text)
    return split_body_into_segments(body, max_chunk), refs


# -- Structure extraction --------------------------------------------
def extract_title(md_text):
    for line in md_text.splitlines():
        if line.strip().startswith("# ") and not line.strip().startswith("## "):
            title = line.strip()[2:].strip()
            title = re.sub(r"\$[^$]+\$", "", title).strip()
            title = re.sub(r"[\\{}]", "", title)
            title = re.sub(r'[<>:"/\\|?*]', "", title)
            return title[:60] if title else "translated"
    return "translated"


def extract_structure(md_text):
    lines = md_text.splitlines()
    sections = []
    images = []
    current_heading = "preamble"
    current_start = 0

    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("## ") or s.startswith("### "):
            if current_heading != "preamble":
                sections.append((current_heading, current_start, i))
            current_heading = s
            current_start = i

    if current_heading != "preamble":
        sections.append((current_heading, current_start, len(lines)))

    for i, line in enumerate(lines):
        m = re.match(r"!\[[^\]]*\]\(([^)]+)\)", line.strip())
        if m:
            sec = "preamble"
            for h, start, end in sections:
                if start <= i < end:
                    sec = h
                    break
            captions = []
            j = i + 1
            while j < len(lines):
                l = lines[j].strip()
                if not l or l.startswith("#") or l.startswith("!["):
                    break
                captions.append(l)
                j += 1
            images.append(
                {
                    "path": m.group(1),
                    "line_no": i,
                    "image_line": line.strip(),
                    "captions": captions,
                    "section": sec,
                }
            )
    return sections, images


def _top_items(items, limit=40):
    counts = {}
    for item in items:
        item = re.sub(r"\s+", " ", item).strip()
        if item:
            counts[item] = counts.get(item, 0) + 1
    return [k for k, _v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def build_consistency_guide(source_text: str) -> str:
    acronyms = re.findall(r"\b(?:[A-Z]{2,}[A-Za-z0-9]*|[A-Z]+[0-9]+[A-Za-z]*)\b", source_text)
    acronyms = [a for a in acronyms if len(a) <= 20 and a not in {"PDF", "DOI", "ISSN"}]

    acronym_counts = {}
    for item in acronyms:
        acronym_counts[item] = acronym_counts.get(item, 0) + 1
    acronym_list = [
        item
        for item, count in sorted(acronym_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if count >= 2
    ][:45]

    lines = [
        "Consistency constraints for this document:",
        "- Translate only source content. Do not add definitions, commentary, or outside facts.",
        "- Reuse the same Chinese wording for recurring technical terms when the same source term recurs.",
        "- Do not leave ordinary English headings, table headers, or technical phrases untranslated.",
        "- Preserve author names, journal names, institution names, and reference-list entries in English only when they are clearly names or references.",
    ]
    if acronym_list:
        lines.append("- Preserve these acronyms exactly unless the source itself expands them: " + ", ".join(acronym_list))
    return "\n".join(lines)


def extract_term_audit_candidates(source_text: str, limit: int = 80) -> list[str]:
    source_text = TABLE_RE.sub(" ", source_text)
    source_text = IMAGE_RE.sub(" ", source_text)
    candidates = re.findall(
        r"\b[A-Z]?[a-z]+(?:-[A-Za-z]+|\s+[A-Z]?[a-z]+){1,5}\b",
        source_text,
    )
    blocked = {"et al", "in vivo", "in vitro"}
    out = []
    seen = set()
    for item in _top_items(candidates, limit * 2):
        key = item.lower()
        if key in blocked:
            continue
        if key not in seen:
            seen.add(key)
            out.append(item)
        if len(out) >= limit:
            break
    return out


def find_untranslated_english(text: str, limit: int = 8) -> list[str]:
    cleaned = TABLE_RE.sub(" ", text)
    cleaned = IMAGE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"https?://\S+|doi:\S+", " ", cleaned, flags=re.IGNORECASE)
    candidates = re.findall(r"\b[A-Z][A-Za-z]+(?:[- ][A-Za-z][A-Za-z]+){2,}\b", cleaned)
    skip_words = {
        "Springer International Publishing Switzerland",
        "Martin Buchheit Paul Laursen",
    }
    out = []
    seen = set()
    for cand in candidates:
        cand = re.sub(r"\s+", " ", cand).strip()
        if cand in skip_words or len(cand) < 18:
            continue
        if re.fullmatch(r"(?:Sports|Medicine|Journal|University|Institute|Department|School|College|Press)(?: .*)?", cand):
            continue
        key = cand.lower()
        if key not in seen:
            seen.add(key)
            out.append(cand)
        if len(out) >= limit:
            break
    return out


def find_untranslated_table_english(text: str, limit: int = 8) -> list[str]:
    cleaned = re.sub(r"\[[^\]]+\]", " ", text)
    cleaned = re.sub(r"\b(?:HIT|RST|SIT|VO2max|VO₂max|DOI|ISBN|ISSN)\b", " ", cleaned)
    cleaned = re.sub(r"[ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ⁰¹²³⁴⁵⁶⁷⁸⁹]+", "", cleaned)
    phrases = re.findall(r"\b[A-Z]?[a-z]{3,}(?:[- ][A-Z]?[a-z]{2,})+\b|\b[A-Z][a-z]{3,}\b", cleaned)
    out = []
    seen = set()
    for phrase in phrases:
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            out.append(phrase)
        if len(out) >= limit:
            break
    return out


def analyze_table_quality(translated_texts: list[str]) -> dict[str, list[str]]:
    residual_english: list[str] = []
    inline_artifacts: list[str] = []
    for text in translated_texts:
        for item in find_untranslated_table_english(text, limit=3):
            if item not in residual_english:
                residual_english.append(item)
        for item in find_inline_artifacts(text):
            if item not in inline_artifacts:
                inline_artifacts.append(item)
    return {
        "residual_english": residual_english[:12],
        "inline_artifacts": inline_artifacts,
    }


def find_latex_artifacts(text: str, limit: int = 30) -> list[str]:
    patterns = [
        r"\\(?:mathrm|operatorname|textsuperscript|textsubscript|textregistered|textcopyright|texttrademark|frac|dot)\b",
        r"\\[A-Za-z]{2,}\b",
        r"\b(?:mathrm|operatorname|textsuperscript|textsubscript|textregistered|textcopyright|texttrademark)\b",
    ]
    out = []
    seen = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            item = match.group(0)
            key = item.lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
            if len(out) >= limit:
                return out
    return out


# -- Cache ------------------------------------------------------------
def _hash_text(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _read_cache(cache_path: Path) -> str | None:
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    return None


def _write_cache(cache_path: Path, text: str):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")


# -- Table translation ------------------------------------------------
def _needs_translation(text: str) -> bool:
    if not text or len(text.strip()) < 2:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    return bool(re.search(r"[A-Za-z]{2,}", text))


def _extract_json_array(text: str):
    text = clean_translation_text(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _translate_text_batch(
    items: list[str],
    config: ProviderConfig,
    cache_dir: Path,
    prefix: str,
    consistency_guide: str = "",
) -> list[str]:
    protected_items = []
    placeholder_maps = []
    for item in items:
        protected, placeholders = protect_fragments(item)
        protected_items.append(protected)
        placeholder_maps.append(placeholders)

    payload = json.dumps(protected_items, ensure_ascii=False)
    system_prompt = TABLE_SYSPROMPT + ("\n\n" + consistency_guide if consistency_guide else "")
    key = _hash_text(config.provider_name, config.base_url, config.model, system_prompt, payload)
    cache_path = cache_dir / f"{prefix}_{key}.json"
    cached = _read_cache(cache_path)
    if cached is not None:
        translated_items = json.loads(cached)
    else:
        response = call_chat_completion(
            config,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload},
            ],
        )
        translated_items = _extract_json_array(response)
        if not isinstance(translated_items, list) or len(translated_items) != len(items):
            raise ValueError("Table translation response length did not match input length.")
        _write_cache(cache_path, json.dumps(translated_items, ensure_ascii=False, indent=2))

    restored = []
    for translated, placeholders in zip(translated_items, placeholder_maps):
        restored.append(restore_placeholders(str(translated), placeholders))
    return restored


def _translate_text_batch_resilient(
    items: list[str],
    config: ProviderConfig,
    cache_dir: Path,
    prefix: str,
    consistency_guide: str = "",
    failures: list[str] | None = None,
) -> list[str]:
    if not items:
        return []
    try:
        return _translate_text_batch(items, config, cache_dir, prefix, consistency_guide)
    except Exception as exc:
        if len(items) == 1:
            if failures is not None:
                failures.append(f"{items[0][:80]} :: {exc}")
            return [items[0]]
        mid = len(items) // 2
        left = _translate_text_batch_resilient(items[:mid], config, cache_dir, prefix, consistency_guide, failures)
        right = _translate_text_batch_resilient(items[mid:], config, cache_dir, prefix, consistency_guide, failures)
        return left + right


def translate_table_cells(
    items: list[str],
    config: ProviderConfig,
    cache_dir: Path,
    consistency_guide: str = "",
    translation_memory: dict[str, str] | None = None,
    failures: list[str] | None = None,
) -> list[str]:
    translation_memory = translation_memory if translation_memory is not None else {}
    result = list(items)
    indexed = []
    for i, item in enumerate(items):
        if not _needs_translation(item):
            continue
        key = _hash_text("cell", item)
        if key in translation_memory:
            result[i] = translation_memory[key]
        else:
            indexed.append((i, item))
    batch: list[tuple[int, str]] = []
    batch_chars = 0

    def flush():
        nonlocal batch, batch_chars
        if not batch:
            return
        unique_values = []
        unique_indexes = {}
        for index, value in batch:
            if value not in unique_indexes:
                unique_indexes[value] = []
                unique_values.append(value)
            unique_indexes[value].append(index)
        translated = _translate_text_batch_resilient(
            unique_values,
            config,
            cache_dir,
            "table",
            consistency_guide,
            failures=failures,
        )
        for source, value in zip(unique_values, translated):
            translation_memory[_hash_text("cell", source)] = value
            for index in unique_indexes[source]:
                result[index] = value
        batch = []
        batch_chars = 0

    for item in indexed:
        item_len = len(item[1])
        if batch and (len(batch) >= 32 or batch_chars + item_len > 3500):
            flush()
        batch.append(item)
        batch_chars += item_len
    flush()
    return result


def translate_tables_in_text(
    text: str,
    config: ProviderConfig,
    cache_dir: Path,
    progress,
    consistency_guide: str = "",
    translation_memory: dict[str, str] | None = None,
    table_reports: list[dict] | None = None,
) -> str:
    if not any(True for _ in html_table_blocks(text)):
        return text

    def repl(match):
        html = match.group(0)
        tables = parse_html_tables(html)
        if not tables:
            return html
        cells = list(iter_cells(tables))
        source_texts = [cell.text for cell in cells]
        failures: list[str] = []
        translated_texts = translate_table_cells(
            source_texts,
            config,
            cache_dir,
            consistency_guide=consistency_guide,
            translation_memory=translation_memory,
            failures=failures,
        )
        for cell, translated in zip(cells, translated_texts):
            cell.text = normalize_inline_output(translated)
        quality = analyze_table_quality([cell.text for cell in cells])
        report = {
            "cells": len(source_texts),
            "translated_cells": sum(1 for src, dst in zip(source_texts, translated_texts) if src != dst),
            "failures": failures,
            "residual_english": quality["residual_english"],
            "inline_artifacts": quality["inline_artifacts"],
        }
        if table_reports is not None:
            table_reports.append(report)
        if failures:
            progress(f"⚠️ Table translated with {len(failures)} cell fallback(s)")
        elif quality["residual_english"] or quality["inline_artifacts"]:
            progress("⚠️ Table translated with quality warnings")
        else:
            progress(f"✓ Table translated ({report['translated_cells']}/{report['cells']} changed)")
        return "\n" + "\n".join(render_html_table(table) for table in tables) + "\n"

    return TABLE_RE.sub(repl, text)


# -- Segment translation ---------------------------------------------
def translate_segment(
    segment,
    prev_context,
    idx,
    total,
    config: ProviderConfig | None = None,
    cache_dir: Path | None = None,
    consistency_guide: str = "",
    translation_memory: dict[str, str] | None = None,
    quality_warnings: list[dict] | None = None,
):
    config = config or resolve_provider_config()
    cache_dir = cache_dir or (OUTPUT_ROOT / "_cache")
    translation_memory = translation_memory if translation_memory is not None else {}
    memory_key = _hash_text("segment", segment)
    if memory_key in translation_memory:
        return translation_memory[memory_key]

    protected, placeholders = protect_fragments(segment)
    ctx = f"Previous section context for terminology only, do not translate or output it: {prev_context[:200]}\n\n" if prev_context else ""
    prompt = (
        f"{ctx}"
        f"Translate only the text between <SOURCE> and </SOURCE> ({idx}/{total}).\n"
        f"Do not output the <SOURCE> tags.\n\n"
        f"<SOURCE>\n{protected}\n</SOURCE>"
    )
    system_prompt = SYSPROMPT + ("\n\n" + consistency_guide if consistency_guide else "")
    key = _hash_text(config.provider_name, config.base_url, config.model, system_prompt, prompt)
    cache_path = cache_dir / f"seg_{idx:04d}_{key}.md"
    cached = _read_cache(cache_path)
    if cached is not None:
        translation_memory[memory_key] = cached
        return cached

    def request_translation(user_prompt: str):
        return call_chat_completion(
            config,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

    for attempt in range(3):
        try:
            translated = request_translation(prompt)
            translated = restore_placeholders(clean_translation_text(translated), placeholders)
            warnings = find_untranslated_english(translated)
            if warnings:
                retry_prompt = (
                    prompt
                    + "\n\nThe previous translation left these ordinary English phrases untranslated: "
                    + "; ".join(warnings)
                    + "\nTranslate those phrases into Chinese unless they are author, institution, journal, or reference names."
                )
                retry = request_translation(retry_prompt)
                retry = restore_placeholders(clean_translation_text(retry), placeholders)
                retry_warnings = find_untranslated_english(retry)
                if len(retry_warnings) <= len(warnings):
                    translated = retry
                    warnings = retry_warnings
            if warnings and quality_warnings is not None:
                quality_warnings.append({"segment": idx, "phrases": warnings})
            _write_cache(cache_path, translated)
            translation_memory[memory_key] = translated
            return translated
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    return segment


# -- Main pipeline ----------------------------------------------------
def run(
    input_folder,
    output_dir=None,
    model=None,
    progress_callback=None,
    provider=None,
    base_url=None,
    api_key=None,
    api_key_env=None,
    temperature=None,
    timeout=None,
    speed_mode=None,
    max_workers=None,
):
    config = resolve_provider_config(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        model=model,
        temperature=temperature,
        timeout=timeout,
    )

    def progress(msg, **kw):
        if progress_callback:
            progress_callback({"msg": msg, **kw})

    input_path = Path(input_folder)
    if not input_path.exists():
        raise FileNotFoundError(f"❌ Folder not found: {input_folder}")

    md_files = sorted(input_path.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"❌ No .md file in {input_folder}")
    src_md = next((p for p in md_files if p.name.lower() == "full.md"), md_files[0])

    sidecar = load_mineru_sidecar(input_path)
    raw = normalize_abstract_heading(normalize_source_text(src_md.read_text(encoding="utf-8")))
    raw, excluded_line_count = strip_excluded_lines(raw, sidecar)
    title = extract_title(raw)
    timestamp = time.strftime("%Y-%m-%d_%H%M")
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:40]
    auto_dirname = f"{timestamp}_{safe_title}"

    out_dir = Path(output_dir) if output_dir else OUTPUT_ROOT / auto_dirname
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if sidecar.has_data:
        write_sidecar_summary(sidecar, out_dir / "mineru_structure_summary.json")

    progress(f"📁 Output: {out_dir}")
    progress(f"📄 Source: {src_md.name}")
    progress(f"🤖 Provider: {config.provider_name} / {config.model}")
    progress(f"📏 Size: {len(raw)} chars")
    if sidecar.has_data:
        progress(
            f"🧱 MinerU JSON: {len(sidecar.source_files)} files, "
            f"{sum(sidecar.block_counts.values())} typed blocks, removed {excluded_line_count} header/footer/page lines"
        )

    consistency_guide = build_consistency_guide(raw)
    term_audit_candidates = extract_term_audit_candidates(raw)
    translation_memory: dict[str, str] = {}
    table_reports: list[dict] = []
    quality_warnings: list[dict] = []
    reference_report = {"entries": 0, "continuation_lines_merged": 0, "numbering_jumps": []}

    body_raw, refs = split_references(raw)
    if refs:
        (out_dir / "references_original.md").write_text(refs, encoding="utf-8")
        refs, reference_report = normalize_references(refs)
        (out_dir / "references_normalized.md").write_text(refs, encoding="utf-8")

    progress("📊 Translating table cells...")
    body_raw = translate_tables_in_text(
        body_raw,
        config,
        cache_dir,
        progress,
        consistency_guide=consistency_guide,
        translation_memory=translation_memory,
        table_reports=table_reports,
    )

    segments = split_body_into_segments(body_raw)
    progress(f"🧩 Segments: {len(segments)}")
    for i, seg in enumerate(segments):
        h = seg.split("\n")[0][:60]
        progress(f"   {i + 1}. {h} ({len(seg)}c)")

    _, source_images = extract_structure(raw)
    progress(f"📷 Source images: {len(source_images)}")

    translations = []
    contexts = []
    prev_ctx = ""
    for seg in segments:
        contexts.append(prev_ctx)
        for line in seg.splitlines():
            if line.startswith("#"):
                prev_ctx = line[:200]
                break

    speed_mode = speed_mode or os.environ.get("AI_SPEED_MODE", "balanced")
    if max_workers is None:
        env_workers = os.environ.get("AI_MAX_WORKERS")
        max_workers = int(env_workers) if env_workers and env_workers.isdigit() else None
    speed_workers = {"safe": 1, "balanced": 2, "fast": 4}
    workers = max_workers or speed_workers.get(str(speed_mode).lower(), 2)
    workers = max(1, min(4, int(workers)))
    progress(f"⚙️ Translation workers: {workers}")

    if workers == 1:
        for i, seg in enumerate(segments):
            progress(f"🌐 [{i + 1}/{len(segments)}]", segment=i + 1, total=len(segments))
            translated = translate_segment(
                seg,
                contexts[i],
                i + 1,
                len(segments),
                config=config,
                cache_dir=cache_dir,
                consistency_guide=consistency_guide,
                translation_memory=translation_memory,
                quality_warnings=quality_warnings,
            )
            translations.append(translated)
            progress(f"✓ ({len(translated)}c)")
    else:
        translations = [""] * len(segments)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(
                    translate_segment,
                    seg,
                    contexts[i],
                    i + 1,
                    len(segments),
                    config,
                    cache_dir,
                    consistency_guide,
                    translation_memory,
                    quality_warnings,
                ): i
                for i, seg in enumerate(segments)
            }
            completed = 0
            for future in concurrent.futures.as_completed(future_map):
                i = future_map[future]
                translated = future.result()
                translations[i] = translated
                completed += 1
                progress(f"✓ [{completed}/{len(segments)}] segment {i + 1} ({len(translated)}c)", segment=completed, total=len(segments))

    body = "\n\n".join(translations)

    body_lines = body.splitlines()
    img_present = set()
    for line in body_lines:
        m = re.search(r"!\[[^\]]*\]\(([^)]+)\)", line)
        if m:
            img_present.add(m.group(1))

    missing = [img for img in source_images if img["path"] not in img_present]
    if missing:
        progress(f"📷 Re-inserting {len(missing)} missing images...")
        for img in missing:
            sec_heading = img["section"]
            sec_num = re.search(r"([\d.]+)", sec_heading)
            insert_idx = -1

            for j, line in enumerate(body_lines):
                m = re.search(r"^(#{1,3})\s+([\d.]+)", line)
                if m and sec_num and m.group(2).rstrip(".") == sec_num.group(1).rstrip("."):
                    insert_idx = j + 1
                    break

            if insert_idx < 0:
                heading_words = sec_heading.split()
                keyword = heading_words[-1].lower() if heading_words else ""
                for j, line in enumerate(body_lines):
                    if keyword and line.startswith("#") and keyword in line.lower():
                        insert_idx = j + 1
                        break

            if insert_idx >= 0:
                block = [img["image_line"]] + img["captions"]
                body_lines = body_lines[:insert_idx] + block + body_lines[insert_idx:]

    result = "\n".join(body_lines).strip()
    if refs:
        result = result.rstrip() + "\n\n" + refs.strip() + "\n"
    result = normalize_inline_output(result)
    latex_artifacts = find_latex_artifacts(result)

    out_md = out_dir / "translated.md"
    out_md.write_text(result, encoding="utf-8")

    out_docx = None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from make_docx import make_docx

        out_docx = out_dir / "translated.docx"
        make_docx(result, out_docx, input_path)
        progress(f"✅ DOCX: {out_docx}")
    except Exception as e:
        progress(f"⚠️ DOCX skipped: {e}")

    log = out_dir / "translation.log"
    log.write_text(
        f"Source: {src_md}\n"
        f"Title: {title}\n"
        f"Segments: {len(segments)}\n"
        f"Images: {len(source_images)} (re-inserted: {len(missing)})\n"
        f"Provider: {config.provider_name}\n"
        f"Base URL: {config.base_url}\n"
        f"Model: {config.model}\n"
        f"References isolated: {bool(refs)}\n"
        f"MinerU JSON files: {', '.join(sidecar.source_files) if sidecar.source_files else 'none'}\n"
        f"Header/footer/page lines removed: {excluded_line_count}\n"
        f"Sidecar formulas found: {len(sidecar.formula_texts)}\n"
        f"Table reports: {json.dumps(table_reports, ensure_ascii=False)}\n"
        f"Quality warnings: {json.dumps(quality_warnings, ensure_ascii=False)}\n"
        f"Reference normalization: {json.dumps(reference_report, ensure_ascii=False)}\n"
        f"LaTeX artifacts: {json.dumps(latex_artifacts, ensure_ascii=False)}\n"
        f"Term audit candidates: {json.dumps(term_audit_candidates[:40], ensure_ascii=False)}\n"
        f"Speed mode: {speed_mode}, workers: {workers}\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M')}\n",
        encoding="utf-8",
    )

    progress(f"✅ MD: {out_md}")
    return out_md, out_docx


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Translate a MinerU markdown output folder.")
    parser.add_argument("input_folder", help="MinerU output folder containing a .md file")
    parser.add_argument("output_dir_pos", nargs="?", help="Optional output directory")
    parser.add_argument("--output-dir", dest="output_dir_opt", help="Optional output directory")
    parser.add_argument("--provider", choices=sorted(PROVIDER_PRESETS), help="AI provider preset")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", help="API key. Prefer environment variables for shared machines.")
    parser.add_argument("--api-key-env", help="Environment variable name containing the API key")
    parser.add_argument("--model", help="Model name")
    parser.add_argument("--temperature", type=float, help="Sampling temperature")
    parser.add_argument("--timeout", type=int, help="Request timeout in seconds")
    parser.add_argument("--speed-mode", choices=["safe", "balanced", "fast"])
    parser.add_argument("--max-workers", type=int, help="Override translation worker count (1-4)")
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    run(
        args.input_folder,
        args.output_dir_opt or args.output_dir_pos,
        model=args.model,
        provider=args.provider,
        base_url=args.base_url,
        api_key=args.api_key,
        api_key_env=args.api_key_env,
        temperature=args.temperature,
        timeout=args.timeout,
        speed_mode=args.speed_mode,
        max_workers=args.max_workers,
    )
