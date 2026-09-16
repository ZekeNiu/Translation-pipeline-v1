from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any
import html


HEADER_FOOTER_TYPES = {
    "header",
    "footer",
    "page_header",
    "page_footer",
    "page_number",
}
FORMULA_TYPES = {
    "equation_inline",
    "inline_equation",
    "interline_equation",
}


@dataclass
class MinerUSidecar:
    source_files: list[str] = field(default_factory=list)
    block_counts: Counter = field(default_factory=Counter)
    excluded_texts: set[str] = field(default_factory=set)
    formula_texts: list[str] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)
    furniture: list[dict] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.source_files)


def normalize_line(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _collect_text(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        text = normalize_line(value)
        if text:
            out.append(text)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_collect_text(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_collect_text(v))
    return out


def _walk(value: Any, sidecar: MinerUSidecar):
    if isinstance(value, dict):
        item_type = str(value.get("type", "")).lower()
        if item_type:
            sidecar.block_counts[item_type] += 1
        if item_type in HEADER_FOOTER_TYPES:
            for key in ("text", "content"):
                for text in _collect_text(value.get(key)):
                    if len(text) <= 160:
                        sidecar.excluded_texts.add(text)
        if item_type in FORMULA_TYPES:
            for key in ("content", "text"):
                for text in _collect_text(value.get(key)):
                    sidecar.formula_texts.append(text)
        for child in value.values():
            _walk(child, sidecar)
    elif isinstance(value, list):
        for child in value:
            _walk(child, sidecar)


def _sidecar_candidates(folder: Path) -> list[Path]:
    candidates: list[Path] = []
    for pattern in ("*_content_list_v2.json", "layout.json", "block_list.json", "*_content_list.json", "*_model.json"):
        candidates.extend(sorted(folder.glob(pattern)))
    seen = set()
    unique = []
    for path in candidates:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def load_mineru_sidecar(folder: Path) -> MinerUSidecar:
    sidecar = MinerUSidecar()
    for path in _sidecar_candidates(folder):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sidecar.source_files.append(path.name)
        _walk(data, sidecar)
    sidecar.provenance = load_provenance(folder)
    sidecar.furniture = load_furniture(folder)
    if sidecar.furniture:
        repeated = Counter(normalize_line(r['text']) for r in sidecar.furniture if r['margin'])
        body_texts = {normalize_line(r['text']) for r in sidecar.provenance}
        sidecar.excluded_texts = {normalize_line(r['text']) for r in sidecar.furniture
                                 if r['margin'] and r['text'] and normalize_line(r['text']) not in body_texts
                                 and (repeated[normalize_line(r['text'])] >= 2 or r['type'] == 'page_number')}
    else:
        sidecar.excluded_texts.clear()  # Labels without reliable positions are not deletion evidence.
    return sidecar


def load_furniture(folder):
    """The v1 content list has documented normalized 0..1000 bounding boxes."""
    for path in sorted(Path(folder).glob('*_content_list.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        records = []
        for index, node in enumerate(data):
            if not isinstance(node, dict) or node.get('type') not in HEADER_FOOTER_TYPES:
                continue
            bbox, page = node.get('bbox'), node.get('page_idx')
            if not isinstance(page, int) or not isinstance(bbox, list) or len(bbox) != 4:
                continue
            if not all(isinstance(v, (int, float)) for v in bbox) or not (0 <= bbox[0] <= bbox[2] <= 1000 and 0 <= bbox[1] <= bbox[3] <= 1000):
                continue
            records.append({'text': str(node.get('text', '')), 'type': node['type'], 'page': page + 1,
                            'bbox': bbox, 'index': index, 'margin': bbox[3] <= 100 or bbox[1] >= 900})
        if records:
            return records
    return []


def find_omissions(sidecar, markdown, units):
    """Never silently restore text. Persist only source-grounded candidates."""
    from task_state import fingerprint
    result, seen = [], set()
    content = location_key(markdown)
    headings = re.compile(r'^(?:INTRODUCTION|METHODS|RESULTS|DISCUSSION|CONCLUSION|CONTRIBUTORS)\b', re.I)
    for record in sidecar.furniture:
        text = record['text'].strip()
        key = location_key(text)
        is_heading = bool(headings.fullmatch(text))
        present = bool(re.search(r'(?im)^#{1,6}\s+' + re.escape(text) + r'\s*$', markdown)) if is_heading else key in content
        if (record['margin'] and not is_heading) or len(text) < 8 or present or key in seen:
            continue
        if not (len(text.split()) >= 3 or text.isupper()):
            continue
        seen.add(key)
        # Only insert before an existing later block on the same original page.
        following = [u for u in units if u.get('page') == record['page'] and u.get('bbox')
                     and u['bbox'][1] >= record['bbox'][3] and u.get('kind') != 'cell']
        following.sort(key=lambda u: (u['bbox'][1], u['bbox'][0]))
        result.append({'id': 'omission_' + fingerprint(record['page'], record['bbox'], text)[:16],
                       'source': '## ' + text if is_heading else text, 'kind': 'heading' if is_heading else 'text',
                       'page': record['page'], 'bbox': record['bbox'],
                       'insert_before': following[0]['id'] if following else None,
                       'reason': '解析疑似遗漏：正文区域内容被标为页眉页脚，未进入解析正文。'})
    return result


def location_key(text):
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    text = re.sub(r'(?m)^#{1,6}\s+', '', text)
    return re.sub(r'\s+', '', text).strip()


def load_provenance(folder):
    """Use a single ordered sidecar; never combine duplicate schemas."""
    candidates = []
    for pattern in ('*_content_list.json', 'content_list.json', '*_content_list_v2.json', 'content_list_v2.json'):
        candidates.extend(sorted(Path(folder).glob(pattern)))
    records = []
    def walk(node, page=None):
        if isinstance(node, list):
            for child in node:
                walk(child, page)
        elif isinstance(node, dict):
            page = node.get('page_idx', page)
            if isinstance(page, int) and node.get('type') in {'text', 'title', 'paragraph', 'table', 'image', 'equation'}:
                content = node.get('content', {})
                if not isinstance(content, dict):
                    content = {}
                texts = []
                for key in ('text', 'table_body', 'table_caption', 'img_caption'):
                    value = node.get(key)
                    if isinstance(value, str):
                        texts.append(value)
                    elif isinstance(value, list):
                        texts.extend(v for v in value if isinstance(v, str))
                for key in ('title_content', 'paragraph_content', 'table_caption', 'image_caption'):
                    value = content.get(key)
                    if isinstance(value, list):
                        texts.append(''.join(str(v.get('content', '')) for v in value if isinstance(v, dict)))
                if isinstance(content.get('html'), str):
                    texts.append(content['html'])
                for text in texts:
                    if location_key(text):
                        records.append({'text': text, 'page': page + 1, 'bbox': node.get('bbox'), 'block_id': len(records)})
                return
            for child in node.values():
                if isinstance(child, (list, dict)):
                    walk(child, page)
    for path in candidates:
        data = None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, list) and data and all(isinstance(page, list) for page in data):
            for i, page in enumerate(data):
                walk(page, i)
        else:
            walk(data)
        if records:
            break
    return records


def strip_excluded_lines(md_text: str, sidecar: MinerUSidecar) -> tuple[str, int]:
    if not sidecar.excluded_texts:
        return md_text, 0

    removed = 0
    kept = []
    for line in md_text.splitlines():
        normalized = normalize_line(line.strip())
        if normalized and normalized in sidecar.excluded_texts:
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


def write_sidecar_summary(sidecar: MinerUSidecar, out_path: Path):
    out_path.write_text(
        json.dumps(
            {
                "source_files": sidecar.source_files,
                "block_counts": dict(sidecar.block_counts.most_common()),
                "excluded_text_count": len(sidecar.excluded_texts),
                "formula_count": len(sidecar.formula_texts),
                "formula_samples": sidecar.formula_texts[:20],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
