"""Bounded source context; operating budgets are not model capability claims."""
import re
from document_blocks import blocks
from task_state import fingerprint

DEFAULT_BUDGET = 32768
OUTPUT_RESERVE = 8192
MODEL_BUDGETS = {'deepseek-v4-flash': 32768, 'deepseek-chat': 32768}


def budget_for(config):
    value = config.context_budget or MODEL_BUDGETS.get(config.model, DEFAULT_BUDGET)
    if value < 4096:
        raise ValueError('上下文预算至少需要 4096。')
    return value


def estimated_tokens(text):
    # Byte count is deliberately conservative for unknown tokenizers.
    return len(text.encode('utf-8'))


def neighbor(text, *, tail=False, limit=1800):
    paragraphs = [b.text for b in blocks(text) if b.kind in {'text', 'heading', 'caption'}]
    atoms = []
    for paragraph in paragraphs:
        atoms.extend([paragraph] if len(paragraph) <= limit else re.split(r'(?<=[.!?。！？])\s+', paragraph))
    selected, size = [], 0
    for atom in (reversed(atoms) if tail else atoms):
        if size + len(atom) + 2 > limit:
            break
        selected.append(atom)
        size += len(atom) + 2
    return '\n\n'.join(reversed(selected) if tail else selected)


def chapter_paths(segments):
    stack, result = [], []
    for text in segments:
        first = None
        for heading in re.finditer(r'(?m)^(#{1,6})\s+(.+)$', text):
            depth, title = len(heading[1]), heading[2]
            stack = [(d, t) for d, t in stack if d < depth] + [(depth, title)]
            if first is None:
                first = ' / '.join(t for _, t in stack)
        result.append(first if first is not None else ' / '.join(t for _, t in stack))
    return result


def table_contexts(tables, caption=''):
    result = []
    for table in tables:
        occupied, placed = {}, []
        for row_no, row in enumerate(table.rows, 1):
            column = 1
            for cell in row.cells:
                while (row_no, column) in occupied:
                    column += 1
                height, width = max(1, int(cell.attrs.get('rowspan', 1))), max(1, int(cell.attrs.get('colspan', 1)))
                placed.append((cell, row_no, column, width))
                for r in range(row_no, row_no + height):
                    for c in range(column, column + width):
                        occupied[r, c] = cell
                column += width
        header_columns = {}
        for prior, pr, pc, pw in placed:
            if prior.tag == 'th' or pr == 1:
                for col in range(pc, pc + pw):
                    header_columns.setdefault(col, []).append((pr, prior.text))
        for cell, row, column, width in placed:
            headers = []
            for col in range(column, column + width):
                for prior_row, text in header_columns.get(col, []):
                    if prior_row < row and text not in headers:
                        headers.append(text)
            label = occupied.get((row, 1))
            result.append({'id': f'r{row}c{column}', 'row': row, 'column': column,
                'table': caption[:400], 'column_headers': [h[:200] for h in headers[:8]],
                'row_header': label.text[:400] if label is not None and label is not cell else ''})
    return result


def stable_content_ids(document, previous=None):
    previous = previous or {}
    old = [u for s in previous.get('segments', []) for u in s['units'] if not u.get('omission_id')]
    new = [u for s in document.get('segments', []) for u in s['units'] if not u.get('omission_id')]
    key = lambda u: (u['kind'], u['source'])
    aligned = len(old) == len(new) and all(key(a) == key(b) for a, b in zip(old, new))
    occurrences = {}
    for i, unit in enumerate(new):
        digest = fingerprint(unit['kind'], unit['source'])[:24]
        ordinal = occurrences.get(digest, 0)
        occurrences[digest] = ordinal + 1
        unit['content_id'] = f'u_{digest}_{ordinal}'
        if aligned:
            unit['id'] = old[i]['id']
        elif not previous:
            unit['id'] = unit['content_id']
        for j, cell in enumerate(unit.get('cells', [])):
            cell['content_id'] = f"{unit['content_id']}:r{cell['row']}c{cell['column']}"
            if aligned and j < len(old[i].get('cells', [])):
                cell['id'] = old[i]['cells'][j]['id']
            elif not previous:
                cell['id'] = cell['content_id']
    if previous and not aligned:
        document['identity_warning'] = '原文块序列已变化，旧人工记录已保留，未猜测迁移对应关系。'
    return document
