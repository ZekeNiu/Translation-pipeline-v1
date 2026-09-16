"""Public artifacts stay at the task root; resumable data lives in _internal."""
from contextlib import contextmanager
from pathlib import Path
import os
import re
import shutil

from task_state import read_json, write_json, atomic_write, task_lock

PUBLIC = {'translated.md', 'translated.docx', 'quality_report.md', 'quality_report.html'}


def artifact(out, name):
    out = Path(out)
    if name in PUBLIC:
        return out / name
    internal = out / '_internal' / name
    # Read old tasks before their first locked mutation.
    if not (out / '_internal' / 'layout.json').exists() and (out / name).exists():
        return out / name
    return internal


def public_markdown(text):
    return re.sub(r'(!\[[^\]]*\]\()(?!(?:_internal/|https?://|data:))(?:(?:\./)?)(images/[^)]+\))',
                  r'\1_internal/\2', text)


def migrate(out):
    """Called under both legacy and new locks; backup completes before any move."""
    out = Path(out)
    internal = out / '_internal'
    internal.mkdir(parents=True, exist_ok=True)
    marker = internal / 'layout.json'
    if marker.exists():
        return
    old = [p for p in out.iterdir() if p.name not in {'_internal', '.run.lock'}]
    if old:
        backup = internal / 'legacy_backup'
        backup.mkdir(exist_ok=True)
        if not (backup / 'backup_complete.json').exists():
            for p in old:
                target = backup / p.name
                if p.is_dir():
                    shutil.copytree(p, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(p, target)
            write_json(backup / 'backup_complete.json', {'version': 1})
        for p in old:
            if p.name not in PUBLIC:
                target = internal / p.name
                if target.exists():
                    raise RuntimeError(f'迁移目标冲突，原文件与备份已保留：{p.name}')
                os.replace(p, target)
        md = out / 'translated.md'
        if md.exists():
            atomic_write(md, public_markdown(md.read_text(encoding='utf-8')))
    write_json(marker, {'version': 2})


@contextmanager
def translation_lock(out):
    out = Path(out)
    with task_lock(out / '_internal'):
        if not (out / '_internal' / 'layout.json').exists():
            with task_lock(out):
                migrate(out)
            (out / '.run.lock').unlink(missing_ok=True)
        from review_state import recover_publication, ensure_schema
        recover_publication(out)
        ensure_schema(out)
        yield
