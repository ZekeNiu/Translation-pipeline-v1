"""Deterministic integrity checks; semantic suspicions are reported, not proof."""
from collections import Counter
import re

TOKEN = re.compile(r"\[\[\[TP_[A-Z]+_\d+\]\]\]")


def plain(text):
    text = TOKEN.sub(" ", text)
    text = re.sub(r"https?://\S+|<[^>]+>", " ", text)
    return text.strip()


def numbers(text):
    # CJK text normally has no spaces before numbers (for example, 为45秒).
    return Counter(re.findall(r"(?<![A-Za-z0-9_])\d+(?:[.,]\d+)*(?:%|‰)?", plain(text)))


def validate_protected(source, translated):
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError("译文为空。")
    if TOKEN.findall(source) != TOKEN.findall(translated):
        raise ValueError("译文丢失、重复或调换了段落、公式、图片、表格或引用标记。")
    if "[[[TP_" in TOKEN.sub("", translated):
        raise ValueError("译文包含损坏的结构标记。")
    pattern = r"(\[\[\[TP_BEGIN_\d+\]\]\])([\s\S]*?)(\[\[\[TP_END_\d+\]\]\])"
    original_blocks, translated_blocks = re.findall(pattern, source), re.findall(pattern, translated)
    if original_blocks and re.sub(pattern, "", translated).strip():
        raise ValueError("译文在段落标记之外增加了内容。")
    for (_, original, _), (_, result, _) in zip(original_blocks, translated_blocks):
        if original.strip() and not result.strip():
            raise ValueError("译文存在空段落。")
        source_headings = re.findall(r"(?m)^(#{1,6})\s+", original.strip())
        result_headings = re.findall(r"(?m)^(#{1,6})\s+", result.strip())
        if source_headings != result_headings:
            raise ValueError("译文标题层级发生变化。")


def concerns(source, translated):
    source, translated = plain(source), plain(translated)
    warnings = []
    if numbers(source) != numbers(translated):
        warnings.append("数字可能发生变化")
    words = re.findall(r"[A-Za-z]{2,}", source)
    if len(words) >= 6 and not re.search(r"[\u4e00-\u9fff]", translated):
        warnings.append("疑似整段未翻译")
    if len(source) >= 120 and len(translated) < len(source) * 0.12:
        warnings.append("译文异常短，可能漏译")
    if len(source) >= 120 and len(translated) > len(source) * 2:
        warnings.append("译文异常长，可能增加内容")
    return warnings
