"""A disposable per-user task index; each task directory owns its data."""
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from task_paths import artifact, translation_lock
from task_state import read_json, write_json, fingerprint
from review_state import revision_state


class TaskLibrary:
    def __init__(self, path):
        self.path = Path(path)

    def paths(self):
        return read_json(self.path, {'version': 1, 'paths': []}).get('paths', [])

    def register(self, path):
        path = str(Path(path).resolve())
        paths = [p for p in self.paths() if p != path]
        write_json(self.path, {'version': 1, 'paths': [path, *paths]})

    def start_intake(self, opts):
        safe = {k: opts[k] for k in ('input_path', 'source_mode', 'provider', 'model', 'base_url', 'output_dir', 'speed_mode',
                'mineru_url', 'mineru_exe', 'mineru_backend', 'mineru_output', 'header_mode') if k in opts}
        safe['parse_options'] = {k: v for k, v in opts.get('parse_options', {}).items() if k != 'retry_unknown'}
        safe['context_budget'] = opts.get('context_budget')
        directory = self.path.parent / 'pending_tasks' / fingerprint(safe)[:20]
        state = {'version': 1, 'phase': 'parse', 'status': 'running', 'source': safe['input_path'], 'options': safe}
        write_json(artifact(directory, 'task_state.json'), state)
        self.register(directory)
        return directory

    def finish_intake(self, path, result=None, status='stopped'):
        state = read_json(artifact(path, 'task_state.json'), {})
        state['status'] = status
        write_json(artifact(path, 'task_state.json'), state)
        if result:
            paths = [p for p in self.paths() if p != str(Path(path).resolve())]
            write_json(self.path, {'version': 1, 'paths': paths})
            self.register(result)

    def rows(self):
        rows = []
        for name in self.paths():
            out = Path(name)
            state = read_json(artifact(out, 'task_state.json'), {})
            report = read_json(artifact(out, 'quality_report.json'), {})
            revision = revision_state(out)
            origin = read_json(artifact(out, 'source_document.json'), {})
            edits_path = artifact(out, 'review_edits.json')
            state_path = artifact(out, 'task_state.json')
            updated = max((p.stat().st_mtime for p in (edits_path, state_path) if p.is_file()), default=0)
            rows.append({'path': name, 'title': Path(origin.get('path') or (state.get('source') if state.get('phase') == 'parse' else name)).stem,
                'status': state.get('status', 'missing'), 'updated': datetime.fromtimestamp(updated).strftime('%Y-%m-%d %H:%M') if updated else '',
                'issues': len(report.get('issues', [])),
                'pending': revision['revision'] != revision['exported_revision'] or bool(revision.get('export_error'))})
        return rows


def usage_summary(out):
    report = read_json(artifact(out, 'quality_report.json'), {})
    latest = report.get('requests', [])
    reviews = read_json(artifact(out, 'review_requests.json'), [])
    glossary = read_json(artifact(out, 'glossary_snapshot.json'), {})
    extraction = glossary.get('extraction_requests', [])
    from usage_records import read_usage
    cumulative = read_usage(artifact(out, 'translation_usage.jsonl')) or latest
    result = []
    for label, records in [('最近一次翻译执行', latest), ('已有记录的翻译累计', cumulative), ('局部改译 / 补译累计', reviews), ('术语提取记录', extraction)]:
        records = [r for r in records if isinstance(r, dict)]
        result.append(f"{label}：{len(records)} 次请求，服务返回 total_tokens {sum((r.get('usage') or {}).get('total_tokens', 0) or 0 for r in records)}；重试 {sum(bool(r.get('retry')) for r in records)} 次")
    return '\n'.join(result) + '\n未返回的用量不推算；未配置价格，不估算金额。'


CLEANABLE = ('_review_export', '_review_candidates')


def storage_inventory(out):
    groups = defaultdict(int)
    internal = artifact(out, 'review_document.json').parent
    for path in internal.rglob('*'):
        if path.is_file():
            head = path.relative_to(internal).parts[0]
            group = '可清理临时预览与候选' if head in CLEANABLE else '翻译缓存' if head == '_cache' else '历史与备份' if head in {'_review_history', '_export_transactions', '_schema_backups', 'legacy_backup', '_previous'} else '任务与人工修订、资源'
            groups[group] += path.stat().st_size
    return dict(groups)


def clean_temporary(out):
    """Explicit UI action; whitelist only and never follow external symlinks."""
    from task_state import task_lock
    out = Path(out).resolve()
    with task_lock(artifact(out, '_export_lock')), translation_lock(out):
        for name in CLEANABLE:
            root = artifact(out, name)
            if root.resolve() != root.absolute() or not root.resolve().is_relative_to((out / '_internal').resolve()):
                raise ValueError('清理路径超出任务内部目录。')
            if root.is_dir():
                # Delete only ordinary files; leave directories and linked resources alone.
                for path in root.rglob('*'):
                    if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                        path.unlink()
