import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_parse import ParseProgress, parser_signature, run_observed
from mineru_runner import MinerUDetection, parse_with_local_cli
from task_state import TaskCancelled, task_lock, write_json


class LocalParseTests(unittest.TestCase):
    def test_unknown_logs_do_not_invent_percent(self):
        events = []
        status = ParseProgress(events.append)
        status.feed('unrecognized log message')
        self.assertNotIn('total', events[-1])
        status.feed('Completed batch 1/1 | Processed 2/2 pages')
        status.emit(True)
        self.assertEqual(events[-1]['completed'], 2)
        self.assertEqual(events[-1]['total'], 2)

    def test_live_utf8_logs_arrive_before_process_exit(self):
        with tempfile.TemporaryDirectory() as td:
            events = []
            result = run_observed([sys.executable, '-X', 'utf8', '-u', '-c',
                                   'import time; print("模型加载", flush=True); time.sleep(1.2); print("Processed 2/2 pages")'],
                                  td, os.environ.copy(), 10, events.append)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(any('模型加载' in e['msg'] and 'total' not in e for e in events))
            self.assertEqual(events[-1]['total'], 2)
            self.assertIn('模型加载', (Path(td) / 'cli_stdout.txt').read_text(encoding='utf-8'))

    def test_cancel_and_timeout_leave_logs_and_stop_child(self):
        with tempfile.TemporaryDirectory() as td:
            event = threading.Event()
            timer = threading.Timer(0.5, event.set)
            timer.start()
            try:
                with self.assertRaises(TaskCancelled):
                    run_observed([sys.executable, '-c', 'import time; time.sleep(60)'], td, os.environ.copy(), cancel_event=event)
            finally:
                timer.join()
            self.assertTrue((Path(td) / 'cli_stderr.txt').exists())
            with self.assertRaises(TimeoutError):
                run_observed([sys.executable, '-c', 'import time; time.sleep(60)'], td, os.environ.copy(), timeout=0.3)

    def test_version_and_model_change_invalidate_signature(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / 'model'
            model.mkdir()
            weight = model / 'weights.bin'
            weight.write_bytes(b'first')
            config = root / 'mineru.json'
            write_json(config, {'models-dir': {'pipeline': str(model)}})
            env = {'MINERU_MODEL_SOURCE': 'local', 'MINERU_TOOLS_CONFIG_JSON': str(config)}
            detector = lambda _: MinerUDetection(True, version='3.4.5')
            first, valid = parser_signature('mineru', 'auto', env, detector)
            self.assertTrue(valid)
            weight.write_bytes(b'changed')
            second, _ = parser_signature('mineru', 'auto', env, detector)
            self.assertNotEqual(first, second)
            third, _ = parser_signature('mineru', 'auto', env, lambda _: MinerUDetection(True, version='next'))
            self.assertNotEqual(second, third)
            self.assertFalse(parser_signature('mineru', 'auto', {}, detector)[1])

    def test_cache_reuse_and_fresh_attempt_after_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'source.pdf'
            source.write_bytes(b'%PDF')
            calls = []
            def parse(cmd, *args):
                out = Path(cmd[cmd.index('-o') + 1])
                calls.append(out)
                (out / 'full.md').write_text('Parsed', encoding='utf-8')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            with patch('mineru_runner._resolve_executable', return_value=('mineru', 'mineru')), patch('mineru_runner.run_observed', side_effect=parse), patch('mineru_runner.parser_signature', return_value=({'engine': '1'}, True)) as sig:
                first = parse_with_local_cli(source, root / 'out')
                self.assertEqual(first, parse_with_local_cli(source, root / 'out'))
                self.assertEqual(len(calls), 1)
                sig.return_value = ({'engine': '2'}, True)
                second = parse_with_local_cli(source, root / 'out')
                self.assertNotEqual(first, second)
                self.assertTrue((first / 'full.md').exists())
                (second / 'full.md').write_text('corrupt')
                self.assertNotEqual(second, parse_with_local_cli(source, root / 'out'))
                with task_lock(first.parents[1]):
                    with self.assertRaises(RuntimeError):
                        parse_with_local_cli(source, root / 'out')
