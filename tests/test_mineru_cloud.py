import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from mineru_cloud import parse_official
from mineru_merge import merge_parts, result_fingerprint, safe_extract
from http_client import RemoteError, request
from task_state import TaskCancelled, file_hash, read_json, write_json
from pdf_parts import prepare_parts
from pdf_helpers import make_pdf


class Response:
    status_code = 200
    headers = {}
    def __init__(self, data=None, content=b''):
        self.data, self.content = data, content
    def json(self):
        return self.data
    def close(self):
        pass
    def iter_content(self, chunk_size):
        yield self.content


class Cloud:
    def __init__(self):
        self.batches, self.uploads, self.calls = {}, [], []
        self.fail = set()
        self.cancel_after_put = None
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if '/api/v4/' in url:
            assert kwargs['headers'] == {'Authorization': 'Bearer dummy-secret'}
        else:
            assert not kwargs.get('headers'), 'API credentials leaked to object storage'
        if method == 'POST':
            assert len(kwargs['json']['files']) <= 50
            batch = str(len(self.batches) + 1)
            self.batches[batch] = {f['data_id']: 'waiting-file' for f in kwargs['json']['files']}
            return Response({'code': 0, 'data': {'batch_id': batch, 'file_urls': [f'https://storage.invalid/{batch}/{key}' for key in self.batches[batch]]}})
        if method == 'PUT':
            batch, key = url.split('/')[-2:]
            self.uploads.append(key)
            self.batches[batch][key] = 'done'
            if self.cancel_after_put:
                self.cancel_after_put.set()
            return Response()
        if '/extract-results/batch/' in url:
            batch = url.split('/')[-1]
            return Response({'code': 0, 'data': {'extract_result': [{'data_id': key, 'state': 'failed' if key in self.fail else status,
                              'err_msg': 'test failure', 'full_zip_url': f'https://results.invalid/{key}'} for key, status in self.batches[batch].items()]}})
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('full.md', '# Parsed ' + url.split('/')[-1])
        return Response(content=content.getvalue())


class CloudTests(unittest.TestCase):
    def test_official_upload_and_restart_make_no_duplicate_requests(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 2)
            cloud = Cloud()
            with patch('mineru_cloud.requests.Session', return_value=cloud):
                first = parse_official(source, 'https://mineru.net/api/v4', 'dummy-secret', Path(td) / 'out', poll_interval=0)
                count = len(cloud.calls)
                second = parse_official(source, 'https://mineru.net/api/v4', 'dummy-secret', Path(td) / 'out', poll_interval=0)
            self.assertEqual(first, second)
            self.assertEqual(len(cloud.calls), count)
            self.assertEqual(cloud.uploads, ['part_0001'])
            self.assertNotIn('dummy-secret', (first.parent / 'parse_state.json').read_text())

    def test_partial_failure_only_resubmits_failed_part(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 4, bookmarks=[2])
            cloud = Cloud()
            cloud.fail = {'part_0002'}
            split = lambda source, directory, **kw: prepare_parts(source, directory, max_pages=2, **kw)
            with patch('mineru_cloud.requests.Session', return_value=cloud), patch('mineru_cloud.prepare_parts', side_effect=split):
                with self.assertRaisesRegex(RemoteError, '部分解析失败'):
                    parse_official(source, 'https://mineru.net', 'dummy-secret', Path(td) / 'out', poll_interval=0)
                cloud.fail.clear()
                folder = parse_official(source, 'https://mineru.net', 'dummy-secret', Path(td) / 'out', poll_interval=0)
            self.assertTrue((folder / 'full.md').exists())
            self.assertEqual(cloud.uploads.count('part_0001'), 1)
            self.assertEqual(cloud.uploads.count('part_0002'), 1)

    def test_cancel_after_upload_resumes_by_querying_existing_task(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 2)
            cloud, event = Cloud(), threading.Event()
            cloud.cancel_after_put = event
            with patch('mineru_cloud.requests.Session', return_value=cloud):
                with self.assertRaises(TaskCancelled):
                    parse_official(source, 'https://mineru.net', 'dummy-secret', Path(td) / 'out', poll_interval=0, cancel_event=event)
                event.clear()
                cloud.cancel_after_put = None
                parse_official(source, 'https://mineru.net', 'dummy-secret', Path(td) / 'out', poll_interval=0, cancel_event=event)
            self.assertEqual(len(cloud.batches), 1)
            self.assertEqual(cloud.uploads, ['part_0001'])

    def test_corrupt_result_is_downloaded_again_without_reupload(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 1)
            cloud = Cloud()
            with patch('mineru_cloud.requests.Session', return_value=cloud):
                folder = parse_official(source, 'https://mineru.net', 'dummy-secret', Path(td) / 'out', poll_interval=0)
                state = read_json(folder.parent / 'parse_state.json')
                (Path(state['parts'][0]['result_dir']) / 'full.md').write_text('corrupt')
                parse_official(source, 'https://mineru.net', 'dummy-secret', Path(td) / 'out', poll_interval=0)
            self.assertEqual(cloud.uploads, ['part_0001'])
            self.assertEqual(sum('results.invalid' in url for _, url in cloud.calls), 2)


class MergeTests(unittest.TestCase):
    def test_archive_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr('../escaped.txt', 'bad')
            data.seek(0)
            with self.assertRaises(ValueError):
                safe_extract(data, Path(td) / 'result')
            self.assertFalse((Path(td) / 'escaped.txt').exists())

    def test_asset_collision_is_namespaced_and_pages_offset(self):
        with tempfile.TemporaryDirectory() as td:
            parts = []
            for i in range(2):
                directory = Path(td) / str(i)
                (directory / 'images').mkdir(parents=True)
                (directory / 'images' / 'same.png').write_bytes(bytes([i]))
                (directory / 'full.md').write_text(f'Part {i}\n\n![](images/same.png)', encoding='utf-8')
                write_json(directory / 'doc_content_list.json', [{'type': 'text', 'text': f'Part {i}', 'page_idx': 0}])
                parts.append({'id': f'part_{i}', 'start': i, 'end': i + 1, 'kind': 'main', 'sha256': str(i),
                              'result_dir': str(directory), 'result_hashes': result_fingerprint(directory)})
            output = merge_parts(parts, Path(td) / 'merged')
            text = (output / 'full.md').read_text()
            self.assertIn('assets/part_0/images/same.png', text)
            self.assertIn('assets/part_1/images/same.png', text)
            data = read_json(output / 'merged_content_list.json')
            self.assertEqual([data[0][0]['page_idx'], data[1][0]['page_idx']], [0, 1])

    def test_unknown_seam_preserves_all_primary_text(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / 'raw'
            directory.mkdir()
            (directory / 'full.md').write_text('Primary text must survive.')
            main = {'id': 'part', 'start': 0, 'end': 2, 'kind': 'main', 'sha256': 'x',
                    'result_dir': str(directory), 'result_hashes': result_fingerprint(directory)}
            seam = {'id': 'seam', 'start': 0, 'end': 2, 'kind': 'seam', 'sha256': 'y'}
            output = merge_parts([main, seam], Path(td) / 'merged')
            self.assertIn('Primary text must survive.', (output / 'full.md').read_text())
            self.assertTrue(read_json(output / 'parse_report.json')['warnings'])


class HttpTests(unittest.TestCase):
    def test_auth_failure_is_not_retried(self):
        response = Response()
        response.status_code = 401
        from unittest.mock import Mock
        session = Mock()
        session.request.return_value = response
        with self.assertRaises(RemoteError):
            request(session, 'GET', 'https://example.invalid')
        self.assertEqual(session.request.call_count, 1)

    def test_rate_limit_respects_retry_after(self):
        from unittest.mock import Mock
        retry = Response()
        retry.status_code, retry.headers = 429, {'Retry-After': '4'}
        session = Mock()
        session.request.side_effect = [retry, Response()]
        with patch('http_client.interruptible_wait') as wait:
            request(session, 'GET', 'https://example.invalid')
        wait.assert_called_once_with(4.0, None)
