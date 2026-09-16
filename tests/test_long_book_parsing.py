from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pdf_helpers import make_pdf
from pdf_parts import prepare_parts
from parse_jobs import managed_parse, needs_parts
from task_state import write_json, read_json, TaskCancelled


class LongParseTests(unittest.TestCase):
    def test_local_soft_target_does_not_override_split_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'book.pdf', 200)
            self.assertFalse(needs_parts(source, {'threshold_pages': 256, 'max_pages': 192}))
            self.assertTrue(needs_parts(source, {'threshold_pages': 256, 'max_pages': 192, 'service_max_pages': 100}))
    def test_modern_api_json_keeps_images_and_locations(self):
        import base64
        from mineru_runner import _write_markdown_from_json
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            result = {'results': {'sample': {'md_content': 'Text\n![](images/a.png)',
                'content_list': '[{"text":"Text","page_idx":0}]',
                'images': {'a.png': 'data:image/png;base64,' + base64.b64encode(b'png').decode()}}}}
            self.assertTrue(_write_markdown_from_json(result, out))
            self.assertEqual((out / 'images/a.png').read_bytes(), b'png')
            self.assertEqual(read_json(out / 'content_list.json')[0]['page_idx'], 0)
            result['results']['sample']['images'] = {'../private.png': 'data:image/png;base64,cG5n'}
            with self.assertRaises(RuntimeError):
                _write_markdown_from_json(result, out)
    def test_thousand_pages_cover_once_and_check_chapter_edges(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = make_pdf(root / 'book.pdf', 1001, bookmarks=[128, 256, 384, 512, 640, 768, 896])
            self.assertTrue(needs_parts(source))
            parts, _, count = prepare_parts(source, root / 'parts', max_pages=192, target_pages=128, min_pages=64, max_bytes=2**63 - 1)
            main = [p for p in parts if p.kind == 'main']
            self.assertEqual([i for p in main for i in range(p.start, p.end)], list(range(count)))
            self.assertEqual(len([p for p in parts if p.kind == 'seam']), len(main) - 1)
            self.assertTrue(all(p.end - p.start <= 192 for p in main))

    def test_resume_starts_no_service_for_verified_parts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = make_pdf(root / 'book.pdf', 6)
            calls, starts = [], []
            @contextmanager
            def service(directory):
                starts.append(directory)
                yield 'http://owned.test'
            fail = True
            def parse(part, out, url):
                nonlocal fail
                self.assertEqual(url, 'http://owned.test')
                calls.append(part.name)
                if fail and part.name == 'part_0002.pdf':
                    fail = False
                    raise TaskCancelled('stop')
                out.mkdir(parents=True, exist_ok=True)
                (out / 'full.md').write_text(part.stem, encoding='utf-8')
                return out
            kwargs = dict(strategy={'max_pages': 3, 'target_pages': 2, 'min_pages': 1}, service=service)
            with self.assertRaises(TaskCancelled):
                managed_parse(source, root / 'out', {'engine': 'test'}, parse, **kwargs)
            managed_parse(source, root / 'out', {'engine': 'test'}, parse, **kwargs)
            self.assertEqual(calls.count('part_0001.pdf'), 1)
            before = len(calls), len(starts)
            managed_parse(source, root / 'out', {'engine': 'test'}, parse, **kwargs)
            self.assertEqual((len(calls), len(starts)), before)
            managed_parse(source, root / 'out', {'engine': 'changed'}, parse, **kwargs)
            self.assertGreater(len(calls), before[0])

    def test_unknown_custom_submission_is_not_repeated(self):
        import requests
        from custom_parser import parse_custom
        class Session:
            calls = 0
            def post(self, *args, **kwargs):
                self.calls += 1
                raise requests.Timeout('uncertain')
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'image.png'
            source.write_bytes(b'fixture')
            session = Session()
            with patch('requests.Session', return_value=session):
                with self.assertRaises(requests.Timeout):
                    parse_custom(source, 'https://example.invalid', '', root / 'out')
                with self.assertRaisesRegex(RuntimeError, '未知'):
                    parse_custom(source, 'https://example.invalid', '', root / 'out')
            self.assertEqual(session.calls, 1)
