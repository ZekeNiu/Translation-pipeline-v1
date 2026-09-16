"""Durable local/custom parser partitions, independent of the parser's RAM window."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import os
import socket
import subprocess
import time
import uuid

from pdf_parts import prepare_parts, PDF_PLAN_VERSION
from mineru_merge import merge_parts, result_valid, result_fingerprint
from task_state import check_cancel, file_hash, fingerprint, read_json, write_json, task_lock, interruptible_wait, TaskCancelled


def needs_parts(source, strategy=None):
    strategy = strategy or {}
    if strategy.get('split') == 'off' or Path(source).suffix.lower() != '.pdf':
        return False
    from pypdf import PdfReader
    try:
        with PdfReader(source) as reader:
            count = len(reader.pages)
        return count > int(strategy.get('threshold_pages', 256)) or (strategy.get('max_bytes') and Path(source).stat().st_size > strategy['max_bytes']) or count > int(strategy.get('max_pages', 10**9))
    except Exception:
        # The parser still owns validation for legacy/direct invocations.
        return False


def managed_parse(source, root, signature, run_part, *, strategy=None, progress=None, cancel_event=None, service=None):
    source = Path(source).resolve()
    strategy = strategy or {}
    identity = fingerprint(file_hash(source), signature, strategy, PDF_PLAN_VERSION)
    directory = Path(root) / ('parts_v2_' + identity[:20])
    with task_lock(directory):
        manifest = directory / 'parse_state.json'
        state = read_json(manifest, {})
        if not state.get('parts') or any(not Path(p['path']).is_file() or file_hash(p['path']) != p['sha256'] for p in state['parts']):
            parts, warnings, count = prepare_parts(source, directory / 'inputs',
                max_pages=int(strategy.get('max_pages', 192)), max_bytes=int(strategy.get('max_bytes', 2**63 - 1)),
                target_pages=int(strategy.get('target_pages', 128)), min_pages=int(strategy.get('min_pages', 64)), cancel_event=cancel_event)
            state = {'version': 2, 'identity': identity, 'parts': [p.record() for p in parts], 'warnings': warnings, 'page_count': count}
            write_json(manifest, state)
        parts = state['parts']
        pending = [p for p in parts if not result_valid(p)]
        completed = len(parts) - len(pending)
        if pending:
            with (service(directory) if service else nullcontext(None)) as api_url:
                for part in pending:
                    check_cancel(cancel_event)
                    if progress:
                        progress({'stage': 'parse', 'msg': f"解析原页 {part['start'] + 1}—{part['end']}（{'接缝核对' if part['kind'] == 'seam' else '正文分片'}）", 'completed': completed, 'total': len(parts)})
                    part.update(status='running')
                    write_json(manifest, state)
                    try:
                        result = Path(run_part(Path(part['path']), directory / 'results' / part['id'], api_url))
                        part.update(status='done', result_dir=str(result.resolve()), result_hashes=result_fingerprint(result))
                        completed += 1
                    except TaskCancelled:
                        part['status'] = 'stopped'
                        write_json(manifest, state)
                        raise
                    except Exception:
                        part['status'] = 'failed'
                        write_json(manifest, state)
                        if part['kind'] == 'main':
                            raise
                    write_json(manifest, state)
        check_cancel(cancel_event)
        folder = merge_parts(parts, directory / 'merged', state.get('warnings', []))
        write_json(folder / 'source_document.json', {'version': 1, 'path': str(source), 'hash': file_hash(source)})
        state['status'] = 'completed'
        write_json(manifest, state)
        return folder


@contextmanager
def local_service(executable, env, directory, *, progress=None, cancel_event=None):
    """One owned MinerU API process for serial CLI clients; always terminate tree."""
    import requests
    from local_parse import terminate_tree
    executable = Path(executable)
    python = executable.parent.parent / 'python.exe'
    if not python.is_file():
        yield None
        return
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    log_dir = Path(directory) / ('service_' + uuid.uuid4().hex[:8])
    log_dir.mkdir(parents=True)
    with (log_dir / 'service.log').open('wb') as log:
        options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        child = subprocess.Popen([str(python), '-m', 'mineru.cli.fast_api', '--host', '127.0.0.1', '--port', str(port)],
            env={**env, 'MINERU_MAX_CONCURRENT_REQUESTS': '1', 'PYTHONUNBUFFERED': '1'}, stdout=log, stderr=log, stdin=subprocess.DEVNULL, **options)
        try:
            start = time.monotonic()
            with requests.Session() as session:
                while True:
                    check_cancel(cancel_event)
                    if child.poll() is not None:
                        raise RuntimeError(f'本地共享解析服务启动失败，日志：{log_dir}')
                    if time.monotonic() - start > 180:
                        raise TimeoutError(f'本地解析服务启动超时，日志：{log_dir}')
                    try:
                        with session.get(url + '/health', timeout=2) as response:
                            if response.ok and response.json().get('processing_window_size'):
                                break
                    except (requests.RequestException, ValueError):
                        pass
                    if progress:
                        progress({'stage': 'parse', 'msg': f'正在启动共享解析服务，已用 {int(time.monotonic() - start)} 秒', 'log': False})
                    interruptible_wait(1, cancel_event)
            yield url
        finally:
            terminate_tree(child)
