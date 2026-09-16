"""Revision metadata and recoverable publication of a set of public artifacts."""
from pathlib import Path
import os
import shutil
import uuid

from task_paths import artifact
from task_state import read_json, write_json

OUTPUTS = ('translated.md', 'translated.docx', 'quality_report.json', 'quality_report.md', 'quality_report.html')


def ensure_schema(out):
    marker = artifact(out, 'review_state.json')
    if marker.exists() or not artifact(out, 'review_document.json').is_file():
        return
    backup = artifact(out, '_schema_backups') / 'review-v1'
    backup.mkdir(parents=True, exist_ok=True)
    for name in (*OUTPUTS, 'review_document.json', 'review_edits.json', 'recovered_units.json', 'task_state.json'):
        source = artifact(out, name)
        if source.is_file():
            shutil.copy2(source, backup / name)
    write_json(marker, {'version': 1, 'revision': 0, 'exported_revision': 0})


def revision_state(out):
    return read_json(artifact(out, 'review_state.json'), {'version': 1, 'revision': 0, 'exported_revision': 0})


def changed(out):
    state = revision_state(out)
    state['revision'] += 1
    write_json(artifact(out, 'review_state.json'), state)
    return state['revision']


def copy_atomic(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        shutil.copyfile(source, temporary)
        with temporary.open('r+b') as fh:
            os.fsync(fh.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def recover_publication(out):
    """Caller owns the task lock. Interrupted publications roll back as a set."""
    marker = artifact(out, 'export_transaction.json')
    txn = read_json(marker, {})
    if txn.get('status') != 'publishing':
        return
    backup = artifact(out, '_export_transactions') / txn['id']
    if backup.resolve().parent != artifact(out, '_export_transactions').resolve():
        raise ValueError('Invalid export recovery path')
    for name in OUTPUTS:
        target = artifact(out, name)
        if name in txn['existing']:
            copy_atomic(backup / name, target)
        else:
            target.unlink(missing_ok=True)
    write_json(marker, {**txn, 'status': 'rolled_back'})


def publish(out, stage, revision):
    """Publish under the task lock; backups/journal precede every public mutation."""
    recover_publication(out)
    txn = {'version': 1, 'id': uuid.uuid4().hex, 'status': 'preparing', 'existing': [], 'revision': revision}
    backup = artifact(out, '_export_transactions') / txn['id']
    backup.mkdir(parents=True)
    for name in OUTPUTS:
        source = artifact(stage, name)
        if not source.is_file() or not source.stat().st_size:
            raise ValueError('导出文件缺失或为空：' + name)
        old = artifact(out, name)
        if old.is_file():
            shutil.copy2(old, backup / name)
            txn['existing'].append(name)
    marker = artifact(out, 'export_transaction.json')
    write_json(marker, {**txn, 'status': 'publishing'})
    try:
        for name in OUTPUTS:
            copy_atomic(artifact(stage, name), artifact(out, name))
        write_json(marker, {**txn, 'status': 'committed'})
    except Exception:
        recover_publication(out)
        raise
    state = revision_state(out)
    state.update(exported_revision=revision, export_error=None)
    write_json(artifact(out, 'review_state.json'), state)


def export_failed(out, exc):
    state = revision_state(out)
    state['export_error'] = str(exc)
    write_json(artifact(out, 'review_state.json'), state)
