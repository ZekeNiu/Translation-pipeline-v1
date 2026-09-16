"""Custom MinerU API protocol with durable task IDs and no ambiguous POST retry."""
from pathlib import Path
import time
import uuid
import requests

from task_state import check_cancel, file_hash, fingerprint, read_json, write_json, task_lock, interruptible_wait
from mineru_merge import result_valid, result_fingerprint


def parse_custom(source, base, key, root, *, mode='auto', timeout=180, max_wait=1800,
                 poll_interval=3, progress=None, cancel_event=None, parse_options=None, managed=False):
    from mineru_runner import _auth_headers, _task_id_from_json, _task_status_from_json, _handle_api_response, locate_mineru_output_folder
    from parse_jobs import needs_parts, managed_parse
    source = Path(source).resolve()
    base = base.rstrip('/')
    options = dict(parse_options or {})
    if not managed and needs_parts(source, options):
        return managed_parse(source, root, {'backend': 'custom', 'url': base, 'mode': mode},
            lambda part, directory, _: parse_custom(part, base, key, directory, mode=mode, timeout=timeout, max_wait=max_wait,
                poll_interval=poll_interval, progress=progress, cancel_event=cancel_event, parse_options=options, managed=True),
            strategy=options, progress=progress, cancel_event=cancel_event)
    directory = Path(root) / ('custom_v2_' + fingerprint(file_hash(source), base, mode)[:20])
    def emit(message):
        if progress:
            progress({'stage': 'parse', 'msg': message})
    with task_lock(directory):
        path = directory / 'api_state.json'
        state = read_json(path, {})
        if result_valid(state):
            emit('自建解析结果校验通过，复用已完成结果。')
            return locate_mineru_output_folder(state['result_dir'])
        if state.get('status') in {'submitting', 'submission_unknown'} and not state.get('task_id'):
            if not options.get('retry_unknown'):
                raise RuntimeError('上次提交结果未知，没有可查询的任务编号。请先在服务端核实；只有明确选择“允许重交未知任务”后才会再次上传。')
            state = {}
        if state.get('status') == 'failed':
            state = {}
        session = requests.Session()
        try:
            selected = state.get('protocol', mode)
            if not state.get('task_id'):
                modes = ['file_parse', 'tasks'] if mode == 'auto' else [mode]
                # A verified health endpoint indicates the modern async protocol.
                if mode == 'auto' and hasattr(session, 'get'):
                    try:
                        health = session.get(base + '/health', headers=_auth_headers(key), timeout=min(timeout, 5))
                        try:
                            if health.status_code == 200 and health.json().get('protocol_version'):
                                modes = ['tasks', 'file_parse']
                        finally:
                            health.close()
                    except (requests.RequestException, ValueError, AttributeError):
                        pass
                for selected in modes:
                    check_cancel(cancel_event)
                    state.update(version=2, status='submitting', protocol=selected)
                    write_json(path, state)
                    emit('正在上传到自建 MinerU；请求结果未知时不会自动重复提交。')
                    try:
                        with source.open('rb') as fh:
                            response = session.post(base + '/' + selected, headers=_auth_headers(key),
                                files={'file': (source.name, fh, 'application/octet-stream')}, timeout=timeout)
                    except requests.RequestException:
                        state['status'] = 'submission_unknown'
                        write_json(path, state)
                        raise
                    if response.status_code in {404, 405} and mode == 'auto':
                        if hasattr(response, 'close'):
                            response.close()
                        state['status'] = 'unsupported'
                        write_json(path, state)
                        continue
                    if response.status_code >= 400:
                        state['status'] = 'failed' if response.status_code < 500 else 'submission_unknown'
                        write_json(path, state)
                        try:
                            response.raise_for_status()
                        finally:
                            if hasattr(response, 'close'):
                                response.close()
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                    task_id = _task_id_from_json(data) if selected == 'tasks' else None
                    if task_id:
                        state.update(task_id=task_id, status='pending')
                        write_json(path, state)
                        if hasattr(response, 'close'):
                            response.close()
                    else:
                        return _finish(response, session, base, key, directory, state, path, source)
                    break
                if not state.get('task_id'):
                    raise RuntimeError('服务不支持已配置的解析接口。')
            deadline = time.monotonic() + max_wait
            while time.monotonic() < deadline:
                check_cancel(cancel_event)
                from http_client import request
                response = request(session, 'GET', base + '/tasks/' + state['task_id'], headers=_auth_headers(key),
                                   timeout=timeout, cancel_event=cancel_event)
                try:
                    data = response.json()
                    status = _task_status_from_json(data)
                    emit(f'自建 MinerU：{status or "正在查询"}；任务编号已保存。')
                    if status in {'done', 'completed', 'success', 'succeeded', 'finished'}:
                        from mineru_runner import _result_url_from_json
                        if not _result_url_from_json(data) and not any(k in data for k in ('markdown', 'full_md', 'md', 'results')):
                            response.close()
                            response = request(session, 'GET', base + '/tasks/' + state['task_id'] + '/result',
                                headers=_auth_headers(key), timeout=timeout, cancel_event=cancel_event)
                        return _finish(response, session, base, key, directory, state, path, source)
                    if status in {'failed', 'error', 'cancelled', 'canceled'}:
                        state['status'] = 'failed'
                        write_json(path, state)
                        raise RuntimeError('自建 MinerU 任务失败；任务编号及已完成分片已保留。')
                finally:
                    response.close()
                interruptible_wait(poll_interval, cancel_event)
            raise TimeoutError('等待自建解析超时；继续任务会查询已保存的任务编号。')
        finally:
            if hasattr(session, 'close'):
                session.close()


def _finish(response, session, base, key, directory, state, path, source):
    from mineru_runner import _handle_api_response, locate_mineru_output_folder
    target = directory / ('result_' + uuid.uuid4().hex)
    target.mkdir()
    try:
        _handle_api_response(response, target, session, base, key)
        folder = locate_mineru_output_folder(target)
        write_json(folder / 'source_document.json', {'version': 1, 'path': str(source), 'hash': file_hash(source)})
        state.update(status='done', result_dir=str(target), result_hashes=result_fingerprint(target))
        write_json(path, state)
        return folder
    finally:
        if hasattr(response, 'close'):
            response.close()
