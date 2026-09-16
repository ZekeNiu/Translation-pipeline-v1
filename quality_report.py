"""One local issue model for HTML reports and the desktop review window."""
import html
from pathlib import Path
from urllib.parse import quote

from task_paths import artifact
from task_state import atomic_write, fingerprint, write_json, file_hash, source_hash


def issue_category(reasons, kind=''):
    text = '；'.join(reasons)
    if kind == 'omission' or any(s in text for s in ('遗漏', '结构', '对应', '未完成', '失败')):
        return '完整性与结构', 0
    if '数字' in text or '数值' in text:
        return '数值与单位', 1
    if '术语' in text:
        return '术语', 2
    return ('翻译疑点', 3) if reasons else ('一般信息', 4)


def issue_detail(unit, edits, reasons):
    from review import effective_text
    from translation_checks import numbers
    text = effective_text(unit, edits)
    details = list(reasons)
    if any('数字' in reason or '数值' in reason for reason in reasons):
        source, target = numbers(unit['source']), numbers(text)
        missing, extra = source - target, target - source
        if missing:
            details.append('原文有而现译缺少：' + '、'.join(missing.elements()))
        if extra:
            details.append('现译新增或不同：' + '、'.join(extra.elements()))
    for term in unit.get('glossary_terms', []):
        if term['target'] not in text:
            details.append(f"指定译法：{term['source']} → {term['target']}")
    return '；'.join(details)


def active_issues(unit, edits):
    from review import effective_text, text_issues
    from glossary import GlossaryMatcher, GlossarySnapshot
    record = edits.get('edits', {}).get(unit['id'], {})
    text = effective_text(unit, edits)
    if unit['kind'] in {'image', 'code', 'reference', 'formula'}:
        issues = list(unit.get('issues', []))
    else:
        issues = text_issues(unit['source'], text)
        if unit['kind'] == 'heading' and unit['source'] == text and any(c.isascii() and c.isalpha() for c in text):
            issues.append('标题可能未翻译')
        issues.extend(i for i in unit.get('issues', []) if '未完成' in i or '对应' in i or '结构' in i)
        terms = unit.get('glossary_terms', [])
        if terms:
            matcher = GlossaryMatcher(GlossarySnapshot(book_enabled=True, book_entries=tuple(terms)))
            issues.extend(matcher.issues(unit['source'], text))
        if record.get('history') and record['history'][-1].get('glossary_terms', []) != terms:
            issues.append('术语已变化，人工译文已保留')
    issues = list(dict.fromkeys(issues))
    confirmation = edits.get('confirmations', {}).get(unit['id'])
    if confirmation == fingerprint(unit['source'], text, issues, unit.get('glossary_terms', [])):
        return []
    return issues


def suggestion(reason):
    if '数字' in reason:
        return '逐项核对原文与现译的数值、单位和正负号；仅格式不同且数值一致时可确认保留。'
    if '公式' in reason or '标记' in reason:
        return '对照原 PDF 核查公式及上下标；不要删除公式中的数值或变量。'
    if '遗漏' in reason:
        return '对照原页确认是否为正文；在复核窗口选择“AI 改译 / 补译”生成候选，采用并保存才插入；非遗漏可记录无需恢复的依据。'
    if '未翻译' in reason or '术语' in reason:
        return '核对是否应译为中文及本书术语；可编辑或按需生成改译候选。'
    return '若是人名、机构名、缩写或变量，核对后可确认保留；普通英文应补译，可按需生成改译候选。'


def collect_issues(document, edits, report):
    from review import all_units, effective_text
    rows = []
    for unit in all_units(document):
        if unit['kind'] == 'table' and unit.get('cells'):
            continue
        reasons = active_issues(unit, edits)
        if not reasons:
            continue
        from review_index import disposition
        category, rank = issue_category(reasons, unit['kind'])
        rows.append({'id': unit['id'], 'kind': unit['kind'], 'chapter': unit.get('chapter', ''),
                     'page': unit.get('page'), 'pages': unit.get('pages', []),
                     'row': unit.get('row'), 'column': unit.get('column'),
                     'source': unit['source'], 'translated': effective_text(unit, edits),
                     'reasons': reasons, 'severity': category, 'rank': rank, 'status': disposition(unit, edits),
                     'detail': issue_detail(unit, edits, reasons),
                     'suggestion': '\n'.join(dict.fromkeys(suggestion(r) for r in reasons))})
    recovered = {u.get('omission_id') for u in all_units(document)}
    for item in document.get('omissions', []):
        if item['id'] in recovered:
            continue
        from review_index import disposition
        if disposition({**item, 'translated': '', 'issues': [item['reason']]}, edits) == 'dismissed':
            continue
        rows.append({**item, 'kind': 'omission', 'chapter': '解析遗漏检查', 'translated': '未进入译文',
                     'severity': '解析疑似遗漏', 'reasons': [item['reason']],
                     'suggestion': suggestion('遗漏') if item.get('insert_before') else '无法可靠确定插入顺序，请对照原页核查；不会自动插入。'})
    for key in ('export_error', 'export_warnings', 'parse_warnings', 'missing_images', 'formula_artifacts'):
        values = report.get(key) or []
        if not isinstance(values, list):
            values = [values]
        for index, value in enumerate(values):
            informative = key == 'export_warnings' and '使用简洁样式' in str(value)
            rows.append({'id': f'{key}:{index}', 'chapter': '导出与解析', 'page': None,
                         'source': '', 'translated': '', 'severity': '信息' if informative else '需核对', 'reasons': [str(value)],
                         'suggestion': '请核对原页与导出结果；导出失败可单独重试。'})
    return sorted(rows, key=lambda row: row.get('rank', 0 if row.get('kind') == 'omission' else 4))


def page_links(out, document, rows):
    from pypdf import PdfReader, PdfWriter
    origin = document.get('source', {})
    source = Path(origin.get('path', ''))
    pages = sorted({p for r in rows for p in (r.get('pages') or [r.get('page')]) if isinstance(p, int) and p > 0})
    links = {}
    # Already extracted pages remain usable after the original file moves.
    for page in pages:
        target = artifact(out, '_review_pages') / f'page_{page}.pdf'
        if target.is_file():
            links[page] = target.relative_to(out).as_posix()
    if not source.is_file() or source.suffix.lower() != '.pdf':
        return links
    if origin.get('hash') and source_hash(source) != origin['hash']:
        return {}  # Never point old locations at a replaced source.
    try:
        with PdfReader(source) as reader:
            for page in pages:
                if page in links or page > len(reader.pages):
                    continue
                target = artifact(out, '_review_pages') / f'page_{page}.pdf'
                target.parent.mkdir(parents=True, exist_ok=True)
                writer = PdfWriter()
                writer.add_page(reader.pages[page - 1])
                writer.write(target)
                writer.close()
                links[page] = target.relative_to(out).as_posix()
    except Exception:
        pass  # Text review remains available for broken/encrypted/missing PDFs.
    return links


def write_reports(out, document, edits, report, assets_out=None):
    out = Path(out)
    rows = collect_issues(document, edits, report)
    report['issue_version'], report['issues'] = 1, rows
    report['status'] = ('partial_failed' if report.get('export_error') or report.get('failed_segments') or report.get('error')
                        else 'needs_review' if any(r['severity'] != '信息' for r in rows) else 'completed')
    links = page_links(Path(assets_out or out), document, rows)
    title = '翻译质量报告'
    summary = f"状态：{report['status']}；待检查 {sum(r['severity'] != '信息' for r in rows)} 项；人工修订 {len(edits.get('edits', {}))} 项。"
    intro = '本报告为本地规则检查，不是完整语义审校。页码为原 PDF 物理页码（含封面），不等于原印刷页码或译文页码。生成报告不调用模型。'
    e = html.escape
    cards, markdown = [], [f'# {title}', '', summary, '', intro, '']
    if report.get('review_export_pending'):
        intro += ' 人工修订已保存，导出尚未成功；成品仍可能是上一版，请重新导出。'
    for row in rows:
        page = row.get('page')
        pages = row.get('pages') or ([page] if page else [])
        location = f"原 PDF 第 {'、'.join(map(str, pages))} 页" if pages else '页码未定位'
        if row.get('row'):
            location += f"；表格第 {row['row']} 行、第 {row['column']} 列"
        reason = row.get('detail') or '；'.join(row['reasons'])
        anchors = ' '.join(f'<a href="{quote(links[p], safe="/")}">打开原第 {p} 页</a>' for p in pages if p in links)
        cards.append(f'<article data-category="{e(row["severity"], quote=True)}" id="{e(row["id"], quote=True)}"><h2>{e(row["severity"])} · {e(location)}</h2>'
                     f'<p>{e(row.get("chapter", ""))} · 编号 {e(row["id"])}</p><p>{anchors}</p>'
                     f'<div class="pair"><section><h3>原文</h3><pre>{e(row["source"])}</pre></section>'
                     f'<section><h3>当前译文</h3><pre>{e(row["translated"])}</pre></section></div>'
                     f'<p><b>疑点：</b>{e(reason)}</p><p><b>建议：</b>{e(row["suggestion"])}</p></article>')
        markdown.extend([f'## {location} · {row["id"]}', '', f'章节：{row.get("chapter", "")}', '',
                         f'疑点：{reason}', '', '原文：' + row['source'], '', '现译：' + row['translated'], '',
                         '建议：' + row['suggestion'], ''])
        markdown.extend(f'[打开原第 {p} 页]({quote(links[p], safe="/")})\n' for p in pages if p in links)
    usage = report.get('requests', [])
    tokens = sum(r.get('usage', {}).get('total_tokens', 0) or 0 for r in usage)
    footer = f"原任务记录模型请求 {report.get('request_count', 0)} 次；服务返回 total_tokens 合计 {tokens}（仅有返回记录的请求）。"
    markdown += [footer, '']
    html_doc = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>翻译质量报告</title><style>body{max-width:1200px;margin:32px auto;padding:0 20px;font:16px/1.7 system-ui;background:#f6f7f9;color:#202733}'
                'article{background:white;padding:24px;margin:24px 0;border:1px solid #dce1e8;border-radius:10px}h2{font-size:19px}'
                '.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#165db1}'
                '@media(max-width:750px){.pair{grid-template-columns:1fr}}</style>'
                f'<h1>{title}</h1><p>{e(summary)}</p><p>{e(intro)}</p><p>在翻译工具的“复核译文”窗口按编号选择项目，可编辑、确认保留或生成改译候选。</p>'
                + '<label>搜索原文、译文或位置 <input id="search" type="search"></label> '
                + '<label>问题类别 <select id="category"><option value="">全部</option>'
                + ''.join(f'<option>{e(c)}</option>' for c in sorted({r['severity'] for r in rows})) + '</select></label> <span id="count"></span>'
                + ''.join(cards) + f'<p>{e(footer)}</p>'
                + '<script>const s=document.getElementById("search"),c=document.getElementById("category");'
                'function filter(){let n=0;document.querySelectorAll("article").forEach(a=>{'
                'a.hidden=!(a.textContent.toLowerCase().includes(s.value.toLowerCase())&&(!c.value||a.dataset.category===c.value));'
                'if(!a.hidden)n++;});document.getElementById("count").textContent="显示 "+n+" 项";}'
                's.addEventListener("input",filter);c.addEventListener("change",filter);filter();</script></html>')
    atomic_write(out / 'quality_report.html', html_doc)
    atomic_write(out / 'quality_report.md', '\n'.join(markdown))
    write_json(artifact(out, 'quality_report.json'), report)
    return rows
