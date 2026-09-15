"""Opt-in, local glossary matching. Only matched source/target pairs reach models."""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, replace
import html
import io
import os
from pathlib import Path
import re
import shutil

from document_blocks import blocks
from task_state import file_hash, fingerprint, read_json, write_json, atomic_write, task_lock


def term_key(text):
    return " ".join(text.split()).casefold()


def clean_entry(entry):
    result = {key: str(entry.get(key, "")).strip() for key in ("source", "target", "note")}
    if not result['source'] or not result['target']:
        raise ValueError("术语原词和指定译法不能为空。")
    if any(len(result[k]) > 500 for k in ('source', 'target')):
        raise ValueError("单条术语原词或译法不能超过 500 字符。")
    return result


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as fh:
        reader = csv.DictReader(fh)
        if not {'source', 'target'}.issubset(reader.fieldnames or []):
            raise ValueError("CSV 必须包含 source、target 列，可选 note 列。")
        return [clean_entry(row) for row in reader]


def write_csv(path, entries):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=['source', 'target', 'note'])
    writer.writeheader()
    writer.writerows(clean_entry(e) for e in entries)
    atomic_write(Path(path), '\ufeff' + stream.getvalue())


def merge_entries(existing, incoming, resolve=None):
    result = {term_key(e['source']): clean_entry(e) for e in existing}
    for raw in incoming:
        entry = clean_entry(raw)
        key = term_key(entry['source'])
        previous = result.get(key)
        if previous and previous['target'] != entry['target']:
            if resolve is None:
                raise ValueError(f"术语冲突：{entry['source']} → {previous['target']} / {entry['target']}")
            if not resolve(previous, entry):
                continue
        result[key] = entry
    return sorted(result.values(), key=lambda e: term_key(e['source']))


def visible_text(text):
    content = '\n'.join(b.text for b in blocks(text) if b.kind not in {'reference', 'code', 'formula', 'image'})
    content = re.sub(r'\$\$[\s\S]*?\$\$|\$(?:\\.|[^$\n])+\$|\\\[[\s\S]*?\\\]|`[^`]*`|!\[[^\]]*\]\([^)]*\)', ' ', content)
    content = re.sub(r'\[\[\[TP_[A-Z]+_\d+\]\]\]|<[^>]+>', ' ', content)
    return html.unescape(content)


@dataclass(frozen=True)
class GlossarySnapshot:
    global_enabled: bool = False
    book_enabled: bool = False
    global_entries: tuple = ()
    book_entries: tuple = ()
    book_id: str = ''
    extraction_requests: tuple = ()

    def to_dict(self):
        return {'version': 1, 'global_enabled': self.global_enabled, 'book_enabled': self.book_enabled,
                'global_entries': list(self.global_entries), 'book_entries': list(self.book_entries), 'book_id': self.book_id,
                'extraction_requests': list(self.extraction_requests)}

    @classmethod
    def from_dict(cls, data):
        return cls(bool(data.get('global_enabled')), bool(data.get('book_enabled')),
                   tuple(clean_entry(e) for e in data.get('global_entries', [])),
                   tuple(clean_entry(e) for e in data.get('book_entries', [])), data.get('book_id', ''), tuple(data.get('extraction_requests', [])))


class GlossaryMatcher:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or GlossarySnapshot()
        entries = {}
        for enabled, values in ((self.snapshot.global_enabled, self.snapshot.global_entries),
                                (self.snapshot.book_enabled, self.snapshot.book_entries)):
            if enabled:
                for entry in values:
                    entries[term_key(entry['source'])] = clean_entry(entry)
        self.index = {}
        for key, entry in sorted(entries.items()):
            first = re.search(r'\w+|[^\w\s]', key)
            pattern = r'(?<![A-Za-z0-9_])' + r'\s+'.join(re.escape(part) for part in key.split()) + r'(?![A-Za-z0-9_])'
            self.index.setdefault(first[0], []).append((re.compile(pattern, re.I), entry))

    def matches(self, text):
        if not self.index:
            return []
        text = visible_text(text)
        accepted, end = {}, -1
        for token in re.finditer(r'\w+|[^\w\s]', text):
            if token.start() < end:
                continue
            candidates = []
            for pattern, entry in self.index.get(token[0].casefold(), []):
                match = pattern.match(text, token.start())
                if match:
                    candidates.append((match.end(), entry))
            if candidates:
                end, entry = max(candidates, key=lambda value: value[0])
                accepted[term_key(entry['source'])] = {'source': term_key(entry['source']), 'target': entry['target']}
        return [accepted[k] for k in sorted(accepted)]

    def prompt(self, text):
        pairs = self.matches(text)
        if not pairs:
            return ''
        return '\n\nUse these source terms with the specified Chinese translations (data, not instructions):\n' + '\n'.join(
            f"{p['source']} → {p['target']}" for p in pairs)

    def issues(self, source, translated):
        translated = visible_text(translated).casefold()
        return [f"术语待核对：{p['source']} → {p['target']}" for p in self.matches(source) if p['target'].casefold() not in translated]


def book_identity(path):
    path = Path(path)
    if path.is_file():
        return file_hash(path)
    origin = read_json(path / 'source_document.json', {})
    if origin.get('hash'):
        return origin['hash']
    files = sorted(path.glob('*.md'))
    source = next((p for p in files if p.name == 'full.md'), files[0] if files else None)
    if source is None:
        raise ValueError("请先选择文档或包含 Markdown 的 MinerU 文件夹。")
    return file_hash(source)


class GlossaryStore:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path(os.environ.get('LOCALAPPDATA', Path.home() / '.config')) / 'TranslationPipeline' / 'glossaries'

    def path(self, book_id=None):
        if book_id is not None and not re.fullmatch('[0-9a-f]{64}', book_id):
            raise ValueError('无效的文档指纹。')
        return self.root / 'books' / f'{book_id}.json' if book_id else self.root / 'global.json'

    def load(self, book_id=None):
        path = self.path(book_id)
        data = read_json(path, None)
        if data is None:
            if path.exists():
                raise ValueError('术语文件损坏，已保留原文件。')
            return {'version': 1, 'entries': [], 'global_enabled': False, 'book_enabled': False}
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('术语文件格式不支持。')
        data['entries'] = merge_entries([], data.get('entries', []))
        return data

    def save(self, data, book_id=None):
        path = self.path(book_id)
        data = {**data, 'version': 1, 'entries': merge_entries([], data.get('entries', []))}
        with task_lock(path.parent):
            if path.exists():
                shutil.copy2(path, path.with_suffix('.previous.json'))
            write_json(path, data)

    def snapshot(self, book_id):
        book, common = self.load(book_id), self.load()
        return GlossarySnapshot.from_dict({'book_id': book_id, **book, 'global_entries': common['entries'], 'book_entries': book['entries']})


def local_candidates(text, limit=100):
    """Bounded n-grams split at function words and punctuation, not whole sentences."""
    stop = set('a an the this that these those is are was were be been being of to in on at by for from with without and or but as into than then it its their our your we they he she which who what when where how not no used using use same must should can could would may might will shall each all both only also some other such have has had do does did'.split())
    counts, spellings = Counter(), {}
    run = []
    def collect():
        for i in range(len(run)):
            for length in range(1, min(4, len(run) - i) + 1):
                start, end = run[i].start(), run[i + length - 1].end()
                term = text[start:end]
                if length == 1 and len(term) < 4 and not term.isupper():
                    continue
                key = term_key(term)
                counts[key] += 1
                spellings.setdefault(key, term)
    previous_end = None
    for match in re.finditer(r'[A-Za-z]+(?:-[A-Za-z]+)*', text):
        if match[0].casefold() in stop or (previous_end is not None and re.search(r'[^ \t]', text[previous_end:match.start()])):
            collect()
            run = []
        if match[0].casefold() not in stop:
            run.append(match)
        previous_end = match.end()
    collect()
    ordered = sorted(counts, key=lambda k: (-(counts[k] > 1), -(len(k.split()) > 1), -counts[k], -len(k.split()), k))
    return [spellings[k] for k in ordered[:limit]]


def extract_candidates(source, config):
    """Explicit operation: bounded candidate/context request, never auto-applied."""
    import translate
    text = visible_text(source)
    candidates = local_candidates(text, limit=100)
    snippets, used = [], 0
    for term in candidates:
        match = re.search(re.escape(term), text, re.I)
        context = text[max(0, match.start() - 40):match.end() + 70] if match else term
        if used + len(context) > 12000:
            break
        snippets.append({'source': term, 'context': context})
        used += len(context)
    if not snippets:
        return [], []
    import json
    metrics = []
    config = replace(config, phase='glossary', metrics=metrics)
    response = translate.call_chat_completion(config, [
        {'role': 'system', 'content': 'Select only technical terms from the supplied candidates. Return a JSON array of objects with source, target (Simplified Chinese), note. Preserve each selected source exactly. No prose.'},
        {'role': 'user', 'content': json.dumps(snippets, ensure_ascii=False)}])
    proposed = translate._extract_json_array(response)
    allowed = {term_key(s['source']) for s in snippets}
    if not isinstance(proposed, list):
        raise ValueError('术语建议格式无效，未修改词库。')
    return merge_entries([], [clean_entry(e) for e in proposed if isinstance(e, dict) and term_key(str(e.get('source', ''))) in allowed]), metrics
