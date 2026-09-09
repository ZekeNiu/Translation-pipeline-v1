from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import translate
from document_blocks import blocks, split_prose
from http_client import RemoteError
from task_state import TaskCancelled, read_json
from translation_checks import concerns, validate_protected, numbers


CFG = translate.ProviderConfig('custom', 'https://example.invalid/v1', 'KEY', 'dummy-secret', 'model')


def echo_source(config, messages, timeout=None):
    value = messages[-1]['content']
    if value.startswith('['):
        return value
    return re.search(r'<SOURCE>\n(.*?)\n</SOURCE>', value, re.S)[1]


class StructureTests(unittest.TestCase):
    def test_translated_dates_compare_by_calendar_value(self):
        for english, chinese in [('11 Jul 2017', '2017年7月11日'), ('25 May 1996', '1996年5月25日'), ('August 12, 2017', '2017年8月12日'), ('16 Dec 91', '91年12月16日')]:
            self.assertEqual(numbers(english), numbers(chinese))
        self.assertNotEqual(numbers('11 Jul 2017'), numbers('2017年7月12日'))
        self.assertNotEqual(numbers('11 Jul 2017'), numbers('2018年7月11日'))
        self.assertNotEqual(numbers('May increase by 5'), numbers('可能增加6'))
        self.assertNotEqual(numbers('16 Dec 91'), numbers('1991年12月16日'))

    def test_equivalent_scientific_notation_and_written_counts(self):
        examples = [('1920x1080 px; 0 % and 100 %', '1920×1080像素；0%和100%'),
                    ('3D coordinates', '三维坐标'), ('Thirteen athletes finished; 88.26 m in 4th place.', '13名运动员完赛；第四名为88.26米。'),
                    ('7 World Championships', '七届世界锦标赛')]
        for original, result in examples:
            self.assertNotIn('数字可能发生变化', concerns(original, result))
        self.assertIn('数字可能发生变化', concerns('Thirteen athletes, 88.26 m in 4th place.', '14名运动员，第四名为88.26米。'))
        self.assertIn('数字可能发生变化', concerns('1920x1080 px', '1920×1070像素'))

    def test_numbers_adjacent_to_chinese_are_not_false_alarms(self):
        self.assertEqual(concerns('Table 1: 45 seconds and 2 minutes.', '表1：45秒和2分钟。'), [])
        self.assertIn('数字可能发生变化', concerns('Table 1: 45 seconds.', '表1：46秒。'))

    def test_reference_section_ends_at_next_peer_heading(self):
        source = '# Chapter 1\n\nBody.\n\n## References\n\n[1] Smith.\n\n# Chapter 2\n\nNext.\n\n## References\n\n[2] Jones.\n\n## Appendix\n\nAppendix body.'
        parts = blocks(source)
        refs = [p.text for p in parts if p.kind == 'reference']
        self.assertEqual(len(refs), 2)
        self.assertTrue(all('Chapter 2' not in ref and 'Appendix' not in ref for ref in refs))
        body, _ = translate.split_references(source)
        self.assertIn('Appendix body.', body)

    def test_long_table_and_formula_are_not_cut(self):
        table = '<table><tr><td>' + 'scientific text ' * 2000 + '</td></tr></table>'
        formula = '$$\n' + 'x + ' * 3000 + 'y\n$$'
        chunks = translate.split_body_into_segments('# Results\n\n' + table + '\n\n' + formula)
        self.assertEqual(sum(table in chunk for chunk in chunks), 1)
        self.assertEqual(sum(formula in chunk for chunk in chunks), 1)

    def test_inline_formula_adjacent_to_words_is_atomic(self):
        source = 'word$x + y$ word ' * 100
        chunks = split_prose(source, 80)
        self.assertEqual(sum(chunk.count('$x + y$') for chunk in chunks), 100)

    def test_lost_duplicate_reordered_placeholders_are_rejected(self):
        original, _ = translate._protect_segment('Text $x=1$ and ![](images/a.png).\n\nSecond paragraph.')
        tokens = re.findall(r'\[\[\[TP_.*?\]\]\]', original)
        outputs = [original.replace(tokens[1], ''), original + tokens[1], original.replace(tokens[1], 'TEMP').replace(tokens[2], tokens[1]).replace('TEMP', tokens[2])]
        for output in outputs:
            with self.assertRaises(ValueError):
                validate_protected(original, output)

    def test_added_commentary_and_empty_blocks_rejected(self):
        source, _ = translate._protect_segment('Text.\n\nOther.')
        with self.assertRaises(ValueError):
            validate_protected(source, 'Here is your translation.\n' + source)
        with self.assertRaises(ValueError):
            validate_protected(source, source.replace('Other.', ''))


class TranslationTests(unittest.TestCase):
    def test_bad_table_cell_does_not_retry_accepted_neighbors(self):
        prompts = []
        def fake(config, messages):
            values = json.loads(messages[-1]['content'])
            prompts.append(values)
            return json.dumps(['已合格', '时间为46秒'] if len(values) == 2 else ['时间为45秒'])
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=fake):
            result = translate.translate_table_cells(['Good', 'Time 45 seconds'], CFG, Path(td))
        self.assertEqual(result, ['已合格', '时间为45秒'])
        self.assertEqual(prompts, [['Good', 'Time 45 seconds'], ['Time 45 seconds']])

    def test_only_suspicious_paragraph_is_sent_for_revision(self):
        prompts = []
        def fake(config, messages):
            text = echo_source(config, messages)
            prompts.append(text)
            if len(prompts) == 1:
                return text.replace('A valid paragraph.', '已合格的段落。').replace('The duration is 45 seconds.', '持续时间为46秒。')
            return text.replace('The duration is 45 seconds.', '持续时间为45秒。')
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=fake):
            result = translate.translate_segment('A valid paragraph.\n\nThe duration is 45 seconds.', '', 1, 1, CFG, Path(td))
        self.assertEqual(result, '已合格的段落。\n\n持续时间为45秒。')
        self.assertEqual(len(prompts), 2)
        self.assertNotIn('A valid paragraph', prompts[1])

    def test_missing_structures_never_cached_as_success(self):
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', return_value='中文。') as call:
            with self.assertRaises(ValueError):
                translate.translate_segment('Text $x=1$ ![](x.png)', '', 1, 1, CFG, Path(td))
            self.assertEqual(call.call_count, 3)
            self.assertEqual(list(Path(td).glob('seg_*.json')), [])

    def test_response_truncation_and_null_content_rejected(self):
        for reason, content in [('length', 'partial'), ('stop', None), ('stop', '')]:
            response = Mock()
            response.json.return_value = {'choices': [{'finish_reason': reason, 'message': {'content': content}}]}
            with patch('translate.http_request', return_value=response):
                with self.assertRaises(ValueError):
                    translate.call_chat_completion(CFG, [])

    def test_cache_corruption_and_temperature_change_invalidate(self):
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=echo_source) as call:
            root = Path(td)
            translate.translate_segment('A formula $x=1$ stays.', '', 1, 1, CFG, root)
            cache = next(root.glob('seg_*.json'))
            cache.write_text('{broken', encoding='utf-8')
            translate.translate_segment('A formula $x=1$ stays.', '', 1, 1, CFG, root)
            translate.translate_segment('A formula $x=1$ stays.', '', 1, 1, replace(CFG, temperature=0.2), root)
            self.assertEqual(call.call_count, 3)

    def test_short_neighbor_context_affects_cache_and_stays_readonly(self):
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=echo_source) as call:
            for context in ('previous-a', 'previous-b'):
                text = translate.translate_segment('Text.', context, 1, 1, CFG, Path(td), next_context='Next context', chapter_context='Chapter 2')
                self.assertNotIn(context, text)
            self.assertEqual(call.call_count, 2)
            self.assertIn('Next source excerpt (read-only)', call.call_args[0][1][-1]['content'])

    def test_failed_quality_retry_keeps_first_valid_candidate(self):
        calls = 0
        def fake(config, messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                return echo_source(config, messages)
            raise RemoteError('temporary network failure')
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=fake):
            warnings = []
            result = translate.translate_segment('These six ordinary words require complete translation.', '', 1, 1, CFG, Path(td), quality_warnings=warnings)
            self.assertIn('These six', result)
            self.assertTrue(warnings)

    def test_table_failure_is_retried_on_next_run(self):
        failed = True
        def fake(config, messages):
            if failed:
                raise ValueError('invalid response')
            return json.dumps(['中文'])
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=fake):
            memory, failures = {}, []
            result = translate.translate_table_cells(['Text'], CFG, Path(td), translation_memory=memory, failures=failures)
            self.assertEqual(result, ['Text'])
            self.assertTrue(failures)
            self.assertFalse(memory)
            failed = False
            self.assertEqual(translate.translate_table_cells(['Text'], CFG, Path(td), translation_memory=memory), ['中文'])

    def test_table_auth_failure_does_not_split_into_many_requests(self):
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=RemoteError('invalid key', 401)) as call:
            with self.assertRaises(RemoteError):
                translate.translate_table_cells(['Alpha', 'Beta', 'Gamma'], CFG, Path(td))
            self.assertEqual(call.call_count, 1)


class JobTests(unittest.TestCase):
    def test_plain_cover_uses_original_pdf_name(self):
        from translation_job import _document_title
        from task_state import write_json
        write_json(self.root.parent / 'parse_state.json', {'parts': [{'path': 'D:/books/Academic report.pdf'}]})
        self.assertEqual(_document_title(translate, 'Cover without heading\n\n## Methods', self.root, self.source), 'Academic report')

    def test_progress_identifies_table_and_body_before_completion(self):
        self.source.write_text('<table><tr><td>Text</td></tr></table>\n\nBody.', encoding='utf-8')
        events = []
        def fake(config, messages):
            self.assertTrue(any(e.get('current_segment') == 1 for e in events))
            return '["正文"]' if config.phase == 'table' else echo_source(config, messages)
        with patch('translate.call_chat_completion', side_effect=fake):
            translate.run(self.root, self.out, progress_callback=events.append, **self.kw)
        self.assertTrue(any('表格 1/1' in e['msg'] for e in events))
        self.assertTrue(any('正文' in e['msg'] for e in events))
        self.assertFalse(next(e for e in events if e['stage'] == 'prepare')['artifact_ready'])

    def test_waiting_status_updates_without_repeating_log_lines(self):
        import itertools
        release, events = threading.Event(), []
        self.source.write_text('Short text.', encoding='utf-8')
        def progress(event):
            events.append(event)
            if event.get('log') is False:
                release.set()
        def fake(config, messages):
            if not release.wait(5):
                raise AssertionError('No waiting progress while the request is running')
            return echo_source(config, messages)
        with patch('translate.call_chat_completion', side_effect=fake), patch('translation_job.time.monotonic', side_effect=itertools.count(0, 11).__next__):
            translate.run(self.root, self.out, progress_callback=progress, **self.kw)
        self.assertTrue(any('等待' in e['msg'] and e.get('log') is False for e in events))
        self.assertEqual(read_json(self.out / 'quality_report.json')['status'], 'completed')

    def test_cli_returns_failure_status_for_partial_output(self):
        import io
        data = io.BytesIO()
        with io.TextIOWrapper(data, encoding='cp1252') as output:
            with patch('translate.run', return_value=(Path('out/translated.md'), None)), patch('translate.read_json', return_value={'status': 'partial_failed'}), patch('translate.sys.stdout', output):
                self.assertEqual(translate.main(['input']), 1)
            output.flush()
            self.assertIn('质量报告', data.getvalue().decode('utf-8'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'input'
        self.root.mkdir()
        self.source = self.root / 'full.md'
        self.out = Path(self.temp.name) / 'output'
        self.kw = dict(provider='custom', base_url=CFG.base_url, api_key=CFG.api_key, model=CFG.model, max_workers=1)

    def test_nested_output_directory_is_rejected(self):
        self.source.write_text('Text.', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '输入目录内'):
            translate.run(self.root, self.root / 'output', **self.kw)

    def test_reference_positions_and_later_chapter_translation(self):
        self.source.write_text('# Chapter 1\n\nText one.\n\n## References\n\n[1] Smith.\n\n# Chapter 2\n\nText two.\n\n## Appendix\n\nText three.', encoding='utf-8')
        def fake(config, messages):
            return echo_source(config, messages).replace('Text one.', '第一段。').replace('Text two.', '第二段。').replace('Text three.', '第三段。')
        with patch('translate.call_chat_completion', side_effect=fake):
            md, _ = translate.run(self.root, self.out, **self.kw)
        result = md.read_text(encoding='utf-8')
        self.assertLess(result.index('[1] Smith.'), result.index('# Chapter 2'))
        self.assertIn('第二段。', result)
        self.assertIn('第三段。', result)

    def test_default_output_directory_is_resumable(self):
        self.source.write_text('# Title\n\nSimple text.', encoding='utf-8')
        with patch('translate.OUTPUT_ROOT', Path(self.temp.name) / 'default'), patch('translate.call_chat_completion', side_effect=echo_source) as call:
            first, _ = translate.run(self.root, **self.kw)
            count = call.call_count
            second, _ = translate.run(self.root, **self.kw)
        self.assertEqual(first, second)
        self.assertEqual(call.call_count, count)

    def test_new_checks_and_better_title_keep_existing_request_cache(self):
        from task_state import write_json
        self.source.write_text('Short text.', encoding='utf-8')
        default = Path(self.temp.name) / 'default'
        with patch('translate.OUTPUT_ROOT', default), patch('translate.call_chat_completion', side_effect=echo_source) as call:
            first, _ = translate.run(self.root, **self.kw)
            state = read_json(first.parent / 'task_state.json')
            state.pop('checks_version')
            write_json(first.parent / 'task_state.json', state)
            write_json(self.root.parent / 'parse_state.json', {'source_name': 'Actual report.pdf'})
            second, _ = translate.run(self.root, **self.kw)
        self.assertEqual(first, second)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(read_json(first.parent / 'task_state.json')['checks_version'], 2)

    def test_failed_segment_only_is_retried(self):
        parts = ['# First\n\nShort text.', '# Second\n\nOther text.']
        self.source.write_text('\n\n'.join(parts), encoding='utf-8')
        fail = True
        seen = []
        def fake(config, messages):
            source = echo_source(config, messages)
            seen.append(source)
            return '' if fail and '# Second' in source else source
        with patch('translate.split_body_into_segments', return_value=parts), patch('translate.call_chat_completion', side_effect=fake):
            translate.run(self.root, self.out, **self.kw)
            self.assertEqual(read_json(self.out / 'quality_report.json')['status'], 'partial_failed')
            count = len(seen)
            fail = False
            translate.run(self.root, self.out, **self.kw)
        self.assertTrue(all('# First' not in value for value in seen[count:]))
        self.assertFalse(read_json(self.out / 'quality_report.json')['failed_segments'])

    def test_stop_preserves_completed_request_for_resume(self):
        self.source.write_text('# Title\n\nSimple text.', encoding='utf-8')
        event = threading.Event()
        def fake(config, messages):
            event.set()
            return echo_source(config, messages)
        with patch('translate.call_chat_completion', side_effect=fake):
            with self.assertRaises(TaskCancelled):
                translate.run(self.root, self.out, cancel_event=event, **self.kw)
        self.assertEqual(read_json(self.out / 'task_state.json')['status'], 'stopped')
        event.clear()
        with patch('translate.call_chat_completion', side_effect=AssertionError('completed chunk should be reused')):
            translate.run(self.root, self.out, cancel_event=event, **self.kw)

    def test_source_replacement_invalidates_old_output(self):
        self.source.write_text('First text.', encoding='utf-8')
        with patch('translate.call_chat_completion', side_effect=echo_source) as call:
            translate.run(self.root, self.out, **self.kw)
            self.source.write_text('Different text.', encoding='utf-8')
            translate.run(self.root, self.out, **self.kw)
        self.assertEqual(call.call_count, 2)
        self.assertIn('Different text.', (self.out / 'translated.md').read_text())
        self.assertTrue(list((self.out / '_previous').glob('*/translated.md')))

    def test_corrupt_completed_chunk_uses_valid_request_cache(self):
        self.source.write_text('Text.', encoding='utf-8')
        with patch('translate.call_chat_completion', side_effect=echo_source) as call:
            translate.run(self.root, self.out, **self.kw)
            next((self.out / '_chunks').glob('*.md')).write_text('broken')
            translate.run(self.root, self.out, **self.kw)
        self.assertEqual(call.call_count, 1)
        self.assertIn('Text.', (self.out / 'translated.md').read_text())
