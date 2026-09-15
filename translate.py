"""Translation Pipeline v2.1.

High-quality MinerU markdown translation with structure protection,
OpenAI-compatible provider selection, and resumable chunk cache.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from inline_semantics import find_inline_artifacts, normalize_inline_output, protect_inline_semantics
from table_utils import html_table_blocks, iter_cells, parse_html_tables, render_html_table
from task_state import check_cancel, TaskCancelled, fingerprint, read_json, redact, write_json
from document_blocks import blocks as document_blocks, segments as structure_segments
from http_client import request as http_request, thread_session, RemoteError
from translation_checks import validate_protected, concerns, numbers, TOKEN


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
    api_key: str = field(repr=False)
    model: str
    temperature: float = 0.1
    timeout: int = 180
    cancel_event: object = field(default=None, repr=False, compare=False)
    metrics: object = field(default=None, repr=False, compare=False)
    phase: str = "body"
    is_retry: bool = False
    request_progress: object = field(default=None, repr=False, compare=False)
    glossary: object = field(default=None, repr=False, compare=False)
    glossary_prompt: str = ""
    alignments: object = field(default=None, repr=False, compare=False)
    cache_only: bool = False

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
    parts = document_blocks(md_text)
    body = "\n\n".join(p.text for p in parts if p.kind != "reference")
    refs = "\n\n".join(p.text for p in parts if p.kind == "reference")
    return body, refs + "\n" if refs else ""


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


def protect_fragments(text: str, placeholders=None) -> tuple[str, dict[str, str]]:
    placeholders = placeholders if placeholders is not None else {}

    def protect_table(match):
        return _store_placeholder("TABLE", match.group(0), placeholders)

    def protect_image(match):
        return _store_placeholder("IMAGE", match.group(0), placeholders)

    text = TABLE_RE.sub(protect_table, text)
    text = IMAGE_RE.sub(protect_image, text)
    text = re.sub(r"\[(?:\d{1,4}(?:\s*[-–,;]\s*\d{1,4})*)\]", lambda m: _store_placeholder("CITATION", m[0], placeholders), text)

    text = protect_inline_semantics(text, lambda kind, value: _store_placeholder(kind, value, placeholders))
    return text, placeholders


def restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
    text = normalize_inline_output(text)
    for token, value in placeholders.items():
        text = text.replace(token, value)
    return text


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
    check_cancel(config.cancel_event)
    if config.cache_only:
        raise OfflineCacheMiss('缺少可恢复的请求缓存；未调用翻译服务。')
    if not config.api_key:
        raise ValueError(
            f"Missing API key for {config.provider_name}. "
            f"Set {config.api_key_env} or enter an API key in the GUI."
        )
    if not config.model:
        raise ValueError("Model is empty. Please choose or enter a model name.")

    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    payload = {"model": config.model, "messages": messages, "temperature": config.temperature}
    if config.request_progress:
        config.request_progress(config.is_retry)
    started = time.monotonic()
    resp, data = None, None
    try:
        resp = http_request(thread_session(), "POST", config.endpoint(), headers=headers, json=payload,
                            timeout=timeout or config.timeout, cancel_event=config.cancel_event)
        data = resp.json()
    finally:
        if resp is not None:
            resp.close()
        if config.metrics is not None:
            config.metrics.append({"seconds": round(time.monotonic() - started, 3), "phase": config.phase,
                                   "retry": config.is_retry, "glossary_chars": len(config.glossary_prompt), "usage": data.get("usage", {}) if isinstance(data, dict) else {}})
    try:
        choice = data["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ValueError("模型响应截断或被过滤，不能作为完整译文。")
        content = choice["message"].get("content", "")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("翻译服务返回了不完整的响应。") from exc
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
            else:
                parts.append(str(item))
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回空译文。")
    return content.strip()


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
    items = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
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
def split_body_into_segments(body: str, max_chunk: int = 10000, min_chunk: int = 1800) -> list[str]:
    return structure_segments(body, max_chunk, min_chunk)


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
    data = read_json(cache_path)
    if isinstance(data, dict) and data.get("version") == 2 and isinstance(data.get("text"), str):
        if data["text"].strip() and data.get("sha256") == fingerprint(data["text"]):
            return data["text"]
    return None


def _write_cache(cache_path: Path, text: str):
    if text.strip():
        write_json(cache_path, {"version": 2, "text": text, "sha256": fingerprint(text)})


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


class OfflineCacheMiss(RuntimeError):
    pass


class _CellValidationError(ValueError):
    def __init__(self, accepted, invalid):
        super().__init__("表格部分单元格未通过数字或结构检查。")
        self.accepted, self.invalid = accepted, invalid


def _table_issues(config, source, translated):
    return len(find_untranslated_table_english(TOKEN.sub("", translated))) + len(config.glossary.issues(source, translated) if config.glossary else [])


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
    term_prompt = config.glossary.prompt("\n".join(items)) if config.glossary else ""
    config = replace(config, glossary_prompt=term_prompt)
    system_prompt = TABLE_SYSPROMPT + ("\n\n" + consistency_guide if consistency_guide else "") + term_prompt
    check_cancel(config.cancel_event)
    key = _hash_text(config.provider_name, config.base_url, config.model, str(config.temperature), system_prompt, payload)
    cache_path = cache_dir / f"{prefix}_{key}.json"
    cached = _read_cache(cache_path)
    if cached is not None:
        try:
            translated_items = json.loads(cached)
            if not isinstance(translated_items, list) or len(translated_items) != len(items):
                raise ValueError("Invalid cached table length")
            for original, translated in zip(protected_items, translated_items):
                validate_protected(original, translated)
                if numbers(original) != numbers(translated):
                    raise ValueError("Cached table numbers changed")
        except (ValueError, TypeError):
            cached = None
    if cached is None:
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
        accepted, invalid = {}, []
        for i, (original, translated) in enumerate(zip(protected_items, translated_items)):
            try:
                validate_protected(original, translated)
                if numbers(original) != numbers(translated):
                    raise ValueError("表格译文改变了数字。")
                accepted[i] = restore_placeholders(translated, placeholder_maps[i])
            except (ValueError, TypeError):
                invalid.append(i)
        if invalid:
            raise _CellValidationError(accepted, invalid)
        suspect = [i for i, text in enumerate(translated_items) if find_untranslated_table_english(TOKEN.sub("", text)) or (config.glossary and config.glossary.issues(items[i], restore_placeholders(text, placeholder_maps[i])))]
        if suspect:
            try:
                retry_terms = config.glossary.prompt("\n".join(items[i] for i in suspect)) if config.glossary else ""
                retry_system = TABLE_SYSPROMPT + ("\n\n" + consistency_guide if consistency_guide else "") + retry_terms
                correction = call_chat_completion(replace(config, is_retry=True, glossary_prompt=retry_terms), [
                    {"role": "system", "content": retry_system + "\nCheck untranslated ordinary words carefully; preserve clear names and acronyms."},
                    {"role": "user", "content": json.dumps([protected_items[i] for i in suspect], ensure_ascii=False)},
                ])
                candidates = _extract_json_array(correction)
                if not isinstance(candidates, list) or len(candidates) != len(suspect):
                    raise ValueError("Correction length mismatch")
                for i, candidate in zip(suspect, candidates):
                    validate_protected(protected_items[i], candidate)
                    if numbers(protected_items[i]) == numbers(candidate) and _table_issues(config, items[i], restore_placeholders(candidate, placeholder_maps[i])) < _table_issues(config, items[i], restore_placeholders(translated_items[i], placeholder_maps[i])):
                        translated_items[i] = candidate
            except (RemoteError, ValueError, TypeError):
                pass  # Keep the already validated first candidate.
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
    except (TaskCancelled, RemoteError, OfflineCacheMiss):
        raise
    except Exception as exc:
        if len(items) == 1:
            if failures is not None:
                failures.append(f"{items[0][:80]} :: {redact(exc, [config.api_key])}")
            return [items[0]]
        mid = len(items) // 2
        retry_config = replace(config, is_retry=True)
        if isinstance(exc, _CellValidationError):
            result = list(items)
            for i, value in exc.accepted.items():
                result[i] = value
            for i in exc.invalid:
                result[i] = _translate_text_batch_resilient([items[i]], retry_config, cache_dir, prefix, consistency_guide, failures)[0]
            return result
        left = _translate_text_batch_resilient(items[:mid], retry_config, cache_dir, prefix, consistency_guide, failures)
        right = _translate_text_batch_resilient(items[mid:], retry_config, cache_dir, prefix, consistency_guide, failures)
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
        key = _hash_text("cell", config.base_url, config.model, str(config.temperature), TABLE_SYSPROMPT, consistency_guide + (config.glossary.prompt(item) if config.glossary else ""), item)
        if key not in translation_memory:
            cached = _read_cache(cache_dir / f"cell_{key}.json")
            if cached is not None:
                translation_memory[key] = cached
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
        local_failures = []
        translated = _translate_text_batch_resilient(
            unique_values,
            config,
            cache_dir,
            "table",
            consistency_guide,
            failures=local_failures,
        )
        if failures is not None:
            failures.extend(local_failures)
        for source, value in zip(unique_values, translated):
            if not local_failures or source != value:
                cell_key = _hash_text("cell", config.base_url, config.model, str(config.temperature), TABLE_SYSPROMPT, consistency_guide + (config.glossary.prompt(source) if config.glossary else ""), source)
                translation_memory[cell_key] = value
                _write_cache(cache_dir / f"cell_{cell_key}.json", value)
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
            "glossary_warnings": [issue for src, dst in zip(source_texts, translated_texts) for issue in (config.glossary.issues(src, dst) if config.glossary else [])],
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
def _protect_segment(segment):
    placeholders, pieces = {}, []
    for block in document_blocks(segment):
        begin = _store_placeholder("BEGIN", "", placeholders)
        if block.kind in {"reference", "code"}:
            protected = _store_placeholder("REFERENCE" if block.kind == "reference" else "CODE", block.text, placeholders)
        else:
            protected, _ = protect_fragments(block.text, placeholders)
        finish = _store_placeholder("END", "", placeholders)
        pieces.append(f"{begin}\n{protected}\n{finish}")
    return "\n\n".join(pieces), placeholders


def translate_segment(
    segment, prev_context, idx, total, config=None, cache_dir=None, consistency_guide="",
    translation_memory=None, quality_warnings=None, next_context="", chapter_context="",
):
    config = config or resolve_provider_config()
    check_cancel(config.cancel_event)
    cache_dir = cache_dir or (OUTPUT_ROOT / "_cache")
    translation_memory = translation_memory if translation_memory is not None else {}
    protected, placeholders = _protect_segment(segment)
    context = (
        f"Current chapter: {chapter_context[:200]}\n"
        f"Previous source excerpt (read-only): {prev_context[-300:]}\n"
        f"Next source excerpt (read-only): {next_context[:300]}\n"
        "These excerpts are context only. Never translate or output them.\n\n"
    )
    instruction = (
        context + "Translate only the text between <SOURCE> and </SOURCE>.\n"
        "Preserve ALL [[[TP_...]]] tokens exactly, including BEGIN and END, in their original order. "
        "Every BEGIN/END pair must contain the complete corresponding paragraph. "
        "Do not output SOURCE tags or any content outside the block markers.\n\n"
    )
    prompt = instruction + f"<SOURCE>\n{protected}\n</SOURCE>"
    term_prompt = config.glossary.prompt(protected) if config.glossary else ""
    config = replace(config, glossary_prompt=term_prompt)
    system_prompt = SYSPROMPT + ("\n\n" + consistency_guide if consistency_guide else "") + term_prompt
    key = _hash_text("validated-v3", config.provider_name, config.base_url, config.model,
                     str(config.temperature), system_prompt, prompt, segment)
    cache_path = cache_dir / f"seg_{key}.json"

    pair_pattern = r"\[\[\[TP_BEGIN_\d+\]\]\][\s\S]*?\[\[\[TP_END_\d+\]\]\]"
    source_pairs = re.findall(pair_pattern, protected)

    def block_issues(original, candidate):
        return concerns(original, candidate) + find_untranslated_english(candidate) + (config.glossary.issues(original, candidate) if config.glossary else [])

    def issues(candidate):
        return [f"段落 {i + 1}：{issue}" for i, (original, result) in enumerate(zip(source_pairs, re.findall(pair_pattern, candidate)))
                for issue in block_issues(original, result)]

    def finish(candidate):
        warnings = issues(candidate)
        if warnings and quality_warnings is not None:
            quality_warnings.append({"segment": idx, "phrases": warnings})
        translation_memory[key] = candidate
        payloads = re.findall(r"\[\[\[TP_BEGIN_\d+\]\]\]([\s\S]*?)\[\[\[TP_END_\d+\]\]\]", candidate)
        restored = [restore_placeholders(payload.strip(), placeholders).strip() for payload in payloads]
        if config.alignments is not None:
            original = document_blocks(segment)
            if len(original) != len(restored):
                raise ValueError("原译文结构对应数量不一致。")
            config.alignments.extend({'kind': block.kind, 'source': block.text, 'translated': text}
                                     for block, text in zip(original, restored))
        return "\n\n".join(restored)

    cached = translation_memory.get(key) or _read_cache(cache_path)
    if cached is not None:
        try:
            validate_protected(protected, cached)
            return finish(cached)
        except ValueError:
            translation_memory.pop(key, None)

    if not _needs_translation(TOKEN.sub("", protected)):
        return finish(protected)
    best, best_issues, last_error = None, [], None
    feedback, active_prompt, retry_indexes = "", prompt, None
    for attempt in range(3):
        check_cancel(config.cancel_event)
        try:
            selected_source = "\n\n".join(source_pairs[i] for i in retry_indexes) if retry_indexes is not None else protected
            request_terms = config.glossary.prompt(selected_source) if config.glossary else ""
            request_system = SYSPROMPT + ("\n\n" + consistency_guide if consistency_guide else "") + request_terms
            candidate = clean_translation_text(call_chat_completion(replace(config, is_retry=attempt > 0, glossary_prompt=request_terms), [
                {"role": "system", "content": request_system},
                {"role": "user", "content": active_prompt + feedback},
            ]))
            if retry_indexes is not None:
                selected = "\n\n".join(source_pairs[i] for i in retry_indexes)
                validate_protected(selected, candidate)
                previous_pairs = re.findall(pair_pattern, best)
                for i, replacement in zip(retry_indexes, re.findall(pair_pattern, candidate)):
                    if len(block_issues(source_pairs[i], replacement)) < len(block_issues(source_pairs[i], previous_pairs[i])):
                        previous_pairs[i] = replacement
                candidate = "\n\n".join(previous_pairs)
            validate_protected(protected, candidate)
            candidate_issues = issues(candidate)
            if best is None or len(candidate_issues) < len(best_issues):
                best, best_issues = candidate, candidate_issues
            if not best_issues or attempt > 0:
                break
            retry_indexes = [i for i, (original, result) in enumerate(zip(source_pairs, re.findall(pair_pattern, best))) if block_issues(original, result)]
            selected = "\n\n".join(source_pairs[i] for i in retry_indexes)
            active_prompt = instruction + f"<SOURCE>\n{selected}\n</SOURCE>"
            feedback = "\n\nCheck these possible problems, preserving all other content and markers: " + "; ".join(best_issues)
        except RemoteError:
            if best is not None:
                break
            raise
        except TaskCancelled:
            raise
        except (ValueError, TypeError) as exc:
            last_error = exc
            feedback = "\n\nThe last response failed integrity checks: " + str(exc) + " Return the complete block sequence."
    if best is None:
        raise ValueError(f"第 {idx} 段未通过完整性检查：{last_error}")
    _write_cache(cache_path, best)
    return finish(best)


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
    cancel_event=None,
    parse_seconds=0,
    glossary=None,
    source_info=None,
):
    check_cancel(cancel_event)
    from translation_job import run_job
    config = resolve_provider_config(provider=provider, base_url=base_url, api_key=api_key,
                                     api_key_env=api_key_env, model=model, temperature=temperature, timeout=timeout)
    return run_job(sys.modules[__name__], config, input_folder, output_dir, progress_callback=progress_callback,
                   speed_mode=speed_mode, max_workers=max_workers, cancel_event=cancel_event, parse_seconds=parse_seconds, glossary=glossary, source_info=source_info)


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
    parser.add_argument("--global-glossary", help="Explicitly enable a global glossary CSV")
    parser.add_argument("--book-glossary", help="Explicitly enable a book glossary CSV")
    return parser


def main(argv=None):
    # Redirected streams on non-Chinese Windows can otherwise use cp1252.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_arg_parser().parse_args(argv)
    from glossary import GlossarySnapshot, read_csv
    glossary = GlossarySnapshot(bool(args.global_glossary), bool(args.book_glossary),
                                tuple(read_csv(args.global_glossary)) if args.global_glossary else (),
                                tuple(read_csv(args.book_glossary)) if args.book_glossary else ())
    md, _ = run(
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
        progress_callback=lambda info: print(info.get("msg", ""), flush=True),
        glossary=glossary,
    )
    report = read_json(md.parent / "quality_report.json", {})
    print(f"质量报告：{md.parent / 'quality_report.md'}")
    return 1 if report.get("status") == "partial_failed" else 0


if __name__ == "__main__":
    sys.exit(main())
