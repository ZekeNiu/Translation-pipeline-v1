"""Persisted block alignment and reversible human edits, independent of model caches."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import uuid

from document_blocks import blocks
from glossary import GlossaryMatcher, GlossarySnapshot, GlossaryStore
from mineru_sidecar import location_key
from table_utils import parse_html_tables, iter_cells, render_html_table
from task_state import atomic_write, file_hash, fingerprint, read_json, write_json, task_lock
from translation_checks import concerns


READONLY = {'reference', 'code', 'formula', 'image', 'unmapped'}


def all_units(document):
    for segment in document['segments']:
        for unit in segment['units']:
            yield unit
            yield from unit.get('cells', [])


def unit_by_id(document, unit_id):
    return next(unit for unit in all_units(document) if unit['id'] == unit_id)


def recover_alignment(engine, config, segment, expected, cache, guide, previous, following, chapter, index, total):
    """Read validated request caches; a cache miss is an explicit offline failure."""
    alignment, pieces, source_tables = [], [], []
    config = replace(config, cache_only=True, metrics=None, request_progress=None, alignments=None)
    for block in blocks(segment):
        if block.kind == 'table':
            source_tables.append(block.text)
            pieces.append(engine.translate_tables_in_text(block.text, config, cache, lambda *_: None, consistency_guide=guide))
        else:
            pieces.append(block.text)
    result = engine.translate_segment('\n\n'.join(pieces), previous, index, total,
        config=replace(config, alignments=alignment), cache_dir=cache, consistency_guide=guide,
        next_context=following, chapter_context=chapter)
    if result.strip() != expected.strip():
        raise ValueError('缓存恢复结果与现有分段不同，不能可靠建立对应关系。')
    originals = iter(source_tables)
    for unit in alignment:
        if unit['kind'] == 'table':
            unit['source'] = next(originals)
    return alignment


def recover_existing(out, config):
    import translate
    from mineru_sidecar import load_mineru_sidecar, strip_excluded_lines
    from translation_job import _source_hash, _excerpt
    out = Path(out)
    with task_lock(out):
        state = read_json(out / 'task_state.json', {})
        source = Path(state.get('source', ''))
        if not source.is_file():
            raise ValueError('原 Markdown 不存在，无法离线恢复对应记录。')
        sidecar = load_mineru_sidecar(source.parent)
        if _source_hash(source.parent, source, sidecar) != state.get('source_hash'):
            raise ValueError('原文已变化，不能从旧任务猜测对应关系。')
        expected_config = fingerprint('pipeline-v5', config.provider_name, config.base_url, config.model, config.temperature,
                                      translate.SYSPROMPT, translate.TABLE_SYSPROMPT)
        if expected_config != state.get('config_hash'):
            raise ValueError('请先选择原任务的服务、模型及参数，再离线恢复；不会自动调用模型。')
        raw = translate.normalize_abstract_heading(translate.normalize_source_text(source.read_text(encoding='utf-8')))
        raw, _ = strip_excluded_lines(raw, sidecar)
        prepared = [translate.normalize_references(b.text)[0] if b.kind == 'reference' else b.text for b in blocks(raw)]
        segments = translate.split_body_into_segments('\n\n'.join(prepared))
        excerpts = [_excerpt(s) for s in segments]
        chapters, chapter, translations = [], '', []
        config = replace(config, glossary=GlossaryMatcher(GlossarySnapshot.from_dict(read_json(out / 'glossary_snapshot.json', {}))))
        guide = translate.build_consistency_guide(raw)
        for i, segment in enumerate(segments):
            headings = re.findall(r'(?m)^#{1,3}\s+.+', segment)
            chapters.append(headings[0] if headings else chapter)
            if headings:
                chapter = headings[-1]
            key = f's{i:05d}_{fingerprint(segment)[:12]}'
            record = state['segments'].get(key, {})
            path = out / '_chunks' / f'{key}.md'
            translated = path.read_text(encoding='utf-8') if path.is_file() else segment
            translations.append(translated)
            if path.is_file() and file_hash(path) == record.get('output_hash') and not record.get('alignment'):
                try:
                    record['alignment'] = recover_alignment(translate, config, segment, translated, out / '_cache', guide,
                        excerpts[i - 1][-300:] if i else '', excerpts[i + 1][:300] if i + 1 < len(segments) else '', chapters[i], i + 1, len(segments))
                except (translate.OfflineCacheMiss, ValueError, TypeError):
                    pass
        document = make_document(state, segments, translations, chapters, sidecar,
                                 read_json(out / 'source_document.json', read_json(source.parent / 'source_document.json', {})), config)
        backup_artifacts(out)
        write_json(out / 'review_document.json', document)
        write_json(out / 'task_state.json', state)
        return document


def make_document(state, segments, translations, chapters, sidecar, source_info, config):
    document = {'version': 1, 'identity': state['identity'], 'source': source_info or {},
                'config': {'provider': config.provider_name, 'base_url': config.base_url,
                           'model': config.model, 'temperature': config.temperature}, 'segments': []}
    matcher = config.glossary or GlossaryMatcher()
    for index, (source, translated) in enumerate(zip(segments, translations)):
        seg_id = f's{index:05d}_{fingerprint(source)[:12]}'
        record = state['segments'].get(seg_id, {})
        alignment = deepcopy(record.get('alignment', []))
        if not alignment:
            alignment = [{'source': source, 'translated': translated, 'kind': 'unmapped'}]
        for i, unit in enumerate(alignment):
            unit.update(id=f'{seg_id}:b{i}', chapter=chapters[index], segment=index + 1,
                        page=None, bbox=None, source_hash=fingerprint(unit['source']))
            unit['issues'] = concerns(unit['source'], unit['translated']) + matcher.issues(unit['source'], unit['translated']) if unit['kind'] not in READONLY else []
            unit['glossary_terms'] = matcher.matches(unit['source'])
            if unit['kind'] == 'unmapped':
                unit['issues'] = ['没有可靠的原译对应记录；可尝试离线恢复，不能直接编辑此分段。']
            if record.get('status') == 'failed':
                unit['issues'].append('本分段翻译未完成，请继续任务。')
            if unit['kind'] == 'table':
                originals, results = parse_html_tables(unit['source']), parse_html_tables(unit['translated'])
                left, right = list(iter_cells(originals)), list(iter_cells(results))
                unit['cells'] = []
                shape = lambda tables: [[(c.tag, c.attrs) for c in row.cells] for table in tables for row in table.rows]
                if shape(originals) == shape(results) and len(left) == len(right):
                    for j, (src, dst) in enumerate(zip(left, right)):
                        unit['cells'].append({'id': f"{unit['id']}:c{j}", 'kind': 'cell', 'source': src.text,
                            'translated': dst.text, 'chapter': chapters[index], 'segment': index + 1,
                            'source_hash': fingerprint(src.text), 'page': None, 'bbox': None,
                            'issues': concerns(src.text, dst.text) + matcher.issues(src.text, dst.text),
                            'glossary_terms': matcher.matches(src.text)})
                else:
                    unit['issues'].append('表格结构不能可靠对应，单元格编辑不可用。')
        document['segments'].append({'id': seg_id, 'chapter': chapters[index], 'units': alignment,
                                     'previous_context': segments[index - 1][-300:] if index else '',
                                     'next_context': segments[index + 1][:300] if index + 1 < len(segments) else ''})
    top_units = [unit for segment in document['segments'] for unit in segment['units']]
    counts = Counter(location_key(u['source']) for u in top_units)
    locations = {}
    for item in sidecar.provenance:
        locations.setdefault(location_key(item['text']), []).append(item)
    used = Counter()
    for unit in top_units:
        key = location_key(unit['source'])
        choices = locations.get(key, [])
        if choices and (len(choices) == 1 or len(choices) == counts[key]):
            match = choices[min(used[key], len(choices) - 1)]
            used[key] += 1
            unit.update(page=match['page'], bbox=match['bbox'], source_block_id=match['block_id'])
            for cell in unit.get('cells', []):
                cell.update(page=match['page'], bbox=match['bbox'])
    return document


def load_edits(out, identity):
    path = Path(out) / 'review_edits.json'
    data = read_json(path, None)
    if data is None:
        if path.exists():
            raise ValueError('人工修订文件损坏，已保留原文件，请从历史版本恢复。')
        data = {'version': 1, 'identity': identity, 'edits': {}}
    if not isinstance(data, dict):
        raise ValueError('人工修订格式不支持，原文件已保留。')
    if data.get('identity') != identity:
        return {'version': 1, 'identity': identity, 'edits': {}}
    if data.get('version') != 1 or not isinstance(data.get('edits'), dict):
        raise ValueError('人工修订格式不支持，原文件已保留。')
    return data


def current_glossary(out):
    saved = GlossarySnapshot.from_dict(read_json(Path(out) / 'glossary_snapshot.json', {}))
    return GlossaryStore().snapshot(saved.book_id) if saved.book_id else saved


def effective_text(unit, edits):
    record = edits['edits'].get(unit['id'], {})
    if record.get('source_hash') == unit['source_hash'] and record.get('history'):
        return record['history'][-1]['text']
    return unit['translated']


def render_document(document, edits):
    segments = []
    for segment in document['segments']:
        pieces = []
        for unit in segment['units']:
            if unit.get('cells'):
                tables = parse_html_tables(unit['translated'])
                for cell, saved in zip(iter_cells(tables), unit['cells']):
                    cell.text = effective_text(saved, edits)
                pieces.append('\n'.join(render_html_table(t) for t in tables))
            else:
                pieces.append(effective_text(unit, edits))
        segments.append('\n\n'.join(pieces))
    return '\n\n'.join(segments).strip() + '\n'


def edit_issues(document, edits):
    result = []
    for unit in all_units(document):
        record = edits['edits'].get(unit['id'], {})
        if record.get('source_hash') == unit['source_hash'] and record.get('history'):
            last = record['history'][-1]
            issues = list(last.get('issues', []))
            if last.get('glossary_terms', []) != unit.get('glossary_terms', []):
                issues.append('术语约束已变化，已保留人工修订，请复核。')
            if issues:
                result.append({'unit': unit['id'], 'issues': issues})
    return result


def validate_edit(unit, text):
    import translate
    from translation_checks import validate_protected
    if unit['kind'] in READONLY or unit['kind'] == 'table':
        raise ValueError('此内容受结构保护或缺少对应关系，不能直接编辑。')
    original, original_map = translate.protect_fragments(unit['source'])
    candidate, candidate_map = translate.protect_fragments(text)
    values = lambda mapping: list(mapping.values())
    if values(original_map) != values(candidate_map):
        raise ValueError('公式、图片、链接或行内结构发生变化，未覆盖现有译文。')
    validate_protected(original, candidate)
    if re.findall(r'(?m)^(#{1,6})\s+', unit['source']) != re.findall(r'(?m)^(#{1,6})\s+', text):
        raise ValueError('标题层级不能在复核中改变。')
    if len(blocks(text)) != 1 or (unit['kind'] == 'cell' and re.search(r'</?(?:table|tr|td|th)\b', text, re.I)):
        raise ValueError('请保持当前段落或单元格结构，不要插入新段落或表格。')
    return concerns(unit['source'], text)


def backup_artifacts(out):
    directory = Path(out) / '_review_history' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    for name in ('translated.md', 'translated.docx', 'quality_report.md', 'quality_report.json', 'review_edits.json', 'review_document.json', 'task_state.json'):
        if (Path(out) / name).exists():
            shutil.copy2(Path(out) / name, directory / name)
    return directory


def _export_review(out, document, edits):
    from make_docx import make_docx
    text = render_document(document, edits)
    report = read_json(out / 'quality_report.json', {})
    report['manual_review'] = edit_issues(document, edits)
    report['manual_edit_count'] = len(edits['edits'])
    stage = out / '_review_export'
    stage.mkdir(exist_ok=True)
    try:
        warnings = make_docx(text, stage / 'translated.docx', out) or []
        report.update(export_error=None, export_warnings=warnings, review_export_pending=False)
        unresolved = any(unit.get('issues') for unit in all_units(document) if unit['id'] not in edits['edits'])
        report['status'] = 'partial_failed' if report.get('failed_segments') or report.get('error') else 'needs_review' if unresolved or report['manual_review'] or warnings or report.get('parse_warnings') or report.get('missing_images') or report.get('formula_artifacts') else 'completed'
        atomic_write(stage / 'translated.md', text)
        write_json(stage / 'quality_report.json', report)
        lines = ['# 翻译质量报告', '', f"状态：{report['status']}；人工修订 {report['manual_edit_count']} 项。", '', '人工修订已应用；原始模型检查记录保留在 JSON 报告。']
        for unit in all_units(document):
            if unit['id'] not in edits['edits']:
                lines.extend(f"- {unit['id']}：{issue}" for issue in unit.get('issues', []))
        for item in report['manual_review']:
            lines.append(f"- {item['unit']}：" + '；'.join(item['issues']))
        lines.extend(f'- 导出：{warning}' for warning in warnings)
        atomic_write(stage / 'quality_report.md', '\n'.join(lines) + '\n')
        for name in ('translated.md', 'translated.docx', 'quality_report.json', 'quality_report.md'):
            os.replace(stage / name, out / name)
        state = read_json(out / 'task_state.json', {})
        state.update(status=report['status'])
        write_json(out / 'task_state.json', state)
    except Exception as exc:
        # The durable edit journal remains ready for an export-only retry.
        report.update(status='partial_failed', export_error=str(exc), review_export_pending=True)
        write_json(out / 'quality_report.json', report)
        atomic_write(out / 'quality_report.md', '# 翻译质量报告\n\n人工修订已保存，但重新导出失败；现有译文文件可能仍为上一版。请点击“重新导出”。\n')
        raise


def save_edit(out, unit_id, text, *, allow_warnings=False, restore=False, expected_revision=None):
    out = Path(out)
    with task_lock(out):
        document = read_json(out / 'review_document.json')
        unit = unit_by_id(document, unit_id)
        edits = load_edits(out, document['identity'])
        if expected_revision is not None and fingerprint(document, edits) != expected_revision:
            raise ValueError('其他窗口已修改此任务，请刷新后重试。')
        previous = edits['edits'].get(unit_id, {'source_hash': unit['source_hash'], 'history': []})
        if restore:
            history = previous['history']
            text = history[-2]['text'] if len(history) >= 2 else unit['translated']
        matcher = GlossaryMatcher(current_glossary(out))
        issues = validate_edit(unit, text) + matcher.issues(unit['source'], text)
        if issues and not allow_warnings:
            raise ValueError('译文有待核对项：' + '；'.join(issues))
        backup_artifacts(out)
        unit['glossary_terms'] = matcher.matches(unit['source'])
        previous['history'].append({'text': text, 'time': datetime.now(timezone.utc).isoformat(),
                                    'issues': issues, 'glossary_terms': unit.get('glossary_terms', [])})
        edits['edits'][unit_id] = previous
        write_json(out / 'review_edits.json', edits)
        write_json(out / 'review_document.json', document)
        _export_review(out, document, edits)
        return issues


def reexport(out):
    out = Path(out)
    with task_lock(out):
        document = read_json(out / 'review_document.json')
        backup_artifacts(out)
        _export_review(out, document, load_edits(out, document['identity']))


def propose_translation(out, unit_id, config):
    import translate
    out = Path(out)
    with task_lock(out):
        document = read_json(out / 'review_document.json')
        unit = unit_by_id(document, unit_id)
        if unit['kind'] in READONLY or unit['kind'] == 'table':
            raise ValueError('此项不能单独重译。')
        matcher = GlossaryMatcher(current_glossary(out))
        metrics = []
        config = replace(config, glossary=matcher, metrics=metrics, phase='review')
        cache = out / '_review_candidates' / uuid.uuid4().hex
        cache.mkdir(parents=True)
        segment = document['segments'][unit['segment'] - 1]
        try:
            if unit['kind'] == 'cell':
                result = translate.translate_table_cells([unit['source']], config, cache)[0]
            else:
                result = translate.translate_segment(unit['source'], segment['previous_context'], unit['segment'], len(document['segments']), config=config,
                    cache_dir=cache, next_context=segment['next_context'], chapter_context=unit['chapter'])
            validate_edit(unit, result)
            write_json(cache / 'candidate.json', {'unit': unit_id, 'text': result, 'requests': metrics, 'glossary_terms': matcher.matches(unit['source'])})
            return result
        finally:
            requests = read_json(out / 'review_requests.json', [])
            write_json(out / 'review_requests.json', requests + metrics)


def extract_original_page(out, unit_id):
    from pypdf import PdfReader, PdfWriter
    out = Path(out)
    document = read_json(out / 'review_document.json')
    unit = unit_by_id(document, unit_id)
    origin = document.get('source', {})
    source = Path(origin.get('path', ''))
    if not unit.get('page') or not source.is_file() or source.suffix.lower() != '.pdf':
        raise ValueError('没有可用的原 PDF 或可靠页码；仍可在窗口对照文本。')
    if origin.get('hash') and file_hash(source) != origin['hash']:
        raise ValueError('原 PDF 已被替换，不能用旧页码定位。')
    target = out / '_review_pages' / f"page_{unit['page']}.pdf"
    target.parent.mkdir(exist_ok=True)
    with PdfReader(source) as reader:
        writer = PdfWriter()
        writer.add_page(reader.pages[unit['page'] - 1])
        writer.write(target)
        writer.close()
    return target
