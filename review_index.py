"""Searchable, grouped review index. No model calls and no checks while filtering."""
from review import all_units, effective_text
from quality_report import active_issues, issue_category, issue_detail
from task_state import fingerprint


def decision_key(unit, edits):
    unit = {'source_hash': fingerprint(unit['source']), **unit}
    return fingerprint('review-rules-v2', unit['source'], effective_text(unit, edits),
                       unit.get('glossary_terms', []), unit.get('issues', []), unit.get('reason'))


def disposition(unit, edits):
    saved = edits.get('dispositions', {}).get(unit['id'], {})
    return saved.get('status', 'open') if saved.get('key') == decision_key(unit, edits) else 'open'


class ReviewIndex:
    def __init__(self, document, edits, previous=None):
        self.document, self.edits = document, edits
        self.units, self.parents, self.rows = {}, {}, {}
        self.top = []
        recovered = set()
        for segment in document.get('segments', []):
            for unit in segment['units']:
                self.top.append(unit['id'])
                self.units[unit['id']] = unit
                recovered.add(unit.get('omission_id'))
                for cell in unit.get('cells', []):
                    self.units[cell['id']] = cell
                    self.parents[cell['id']] = unit['id']
        for item in document.get('omissions', []):
            if item['id'] not in recovered:
                self.units[item['id']] = {**item, 'kind': 'omission', 'translated': '',
                    'chapter': '解析遗漏检查', 'source_hash': fingerprint(item['source']), 'issues': [item['reason']]}
                self.top.append(item['id'])
        self.cache = previous.cache.copy() if previous else {}
        self.update(edits)

    def update(self, edits, ids=None):
        self.edits = edits
        for key in (ids if ids is not None else self.units):
            unit = self.units[key]
            signature = fingerprint(unit, edits.get('edits', {}).get(key), edits.get('confirmations', {}).get(key), edits.get('dispositions', {}).get(key))
            if key in self.cache and self.cache[key][0] == signature:
                self.rows[key] = {**self.cache[key][1], 'unit': unit}
                continue
            reasons = unit.get('issues', []) if unit['kind'] == 'omission' else active_issues(unit, {**edits, 'confirmations': {}})
            state = disposition(unit, edits)
            if edits.get('confirmations', {}).get(key) == fingerprint(unit['source'], effective_text(unit, edits), reasons, unit.get('glossary_terms', [])):
                state = 'confirmed'
            category, rank = issue_category(reasons, unit['kind'])
            chapter = unit.get('chapter') or (f"原第 {unit['page']} 页" if unit.get('page') else '页码未定位')
            self.rows[key] = {'unit': unit, 'issues': reasons, 'status': state, 'category': category, 'rank': rank,
                'chapter': chapter, 'detail': issue_detail(unit, edits, reasons),
                'search': (' '.join((key, chapter, unit['source'], effective_text(unit, edits)))).casefold()}
            self.cache[key] = signature, self.rows[key]

    def select(self, *, query='', chapter='', category='', status='open', issues_only=True):
        query = query.strip().casefold()
        chosen = {}
        for key, row in self.rows.items():
            if issues_only and (not row['issues'] or (status != 'all' and row['status'] != status)):
                continue
            if query and query not in row['search']:
                continue
            if chapter and row['chapter'] != chapter:
                continue
            if category and row['category'] != category:
                continue
            parent = self.parents.get(key, key)
            chosen[parent] = min(chosen.get(parent, row['rank']), row['rank'])
        result = [key for key in self.top if key in chosen]
        if issues_only:
            result.sort(key=lambda key: chosen[key])
        return result

    def unresolved(self):
        return sum(bool(r['issues']) and r['status'] not in {'confirmed', 'dismissed'}
                   for key, r in self.rows.items() if not self.units[key].get('cells'))
