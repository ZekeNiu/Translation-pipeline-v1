import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from glossary import GlossaryMatcher, GlossarySnapshot, GlossaryStore, book_identity, merge_entries, read_csv, write_csv, extract_candidates, local_candidates
import translate
from task_state import read_json


def entry(source, target, note=''):
    return dict(source=source, target=target, note=note)


def snapshot(*entries):
    return GlossarySnapshot(True, False, tuple(entries))


class GlossaryTests(unittest.TestCase):
    def test_local_candidates_stop_at_function_words(self):
        candidates = local_candidates('Oxygen concentration is denoted by O2. The recovery period was 2 min.')
        self.assertIn('Oxygen concentration', candidates)
        self.assertIn('recovery period', candidates)
        self.assertNotIn('Oxygen concentration is denoted by', candidates)

    def test_priority_boundaries_and_longest_phrase(self):
        matcher = GlossaryMatcher(GlossarySnapshot(True, True,
            (entry('force', '力'), entry('force plate', '测力台'), entry('Force', '力量')),
            (entry('force plate', '测力平台'),)))
        self.assertEqual(matcher.matches('FORCE plate, force; reinforcement.'),
                         [{'source': 'force', 'target': '力量'}, {'source': 'force plate', 'target': '测力平台'}])
        self.assertEqual(matcher.matches('reinforcement'), [])
        self.assertEqual(GlossaryMatcher().prompt('force'), '')

    def test_protected_material_and_notes_are_not_sent(self):
        matcher = GlossaryMatcher(snapshot(entry('force', '力', 'PRIVATE NOTE')))
        self.assertEqual(matcher.prompt('`force` $force$\n\n```\nforce\n```\n\n## References\n\nforce'), '')
        self.assertNotIn('PRIVATE NOTE', matcher.prompt('force'))

    def test_unrelated_large_glossary_has_same_prompt(self):
        first = GlossaryMatcher(snapshot(entry('force plate', '测力平台')))
        large = GlossaryMatcher(snapshot(entry('force plate', '测力平台'), *(entry(f'unrelated{i}', f'术语{i}') for i in range(10000))))
        text = 'The force plate measured the result.' * 100
        started = time.perf_counter()
        for _ in range(10):
            self.assertEqual(first.prompt(text), large.prompt(text))
        self.assertLess(time.perf_counter() - started, 3)

    def test_csv_conflict_resolution_and_default_off(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = GlossaryStore(root / 'store')
            source = root / 'book.pdf'
            source.write_bytes(b'content')
            book = book_identity(source)
            self.assertFalse(store.snapshot(book).global_enabled)
            data = store.load(book)
            data.update(global_enabled=True, book_enabled=True, entries=[entry('force', '力')])
            store.save(data, book)
            self.assertTrue(store.snapshot(book).book_enabled)
            csv_path = root / 'terms.csv'
            write_csv(csv_path, data['entries'])
            self.assertEqual(read_csv(csv_path), data['entries'])
            with self.assertRaises(ValueError):
                merge_entries(data['entries'], [entry('FORCE', '力量')])
            self.assertEqual(merge_entries(data['entries'], [entry('FORCE', '力量')], lambda *_: False), data['entries'])
            moved = root / 'renamed.pdf'
            source.rename(moved)
            self.assertEqual(book_identity(moved), book)

    def test_explicit_extraction_is_bounded_and_rejects_invented_terms(self):
        config = translate.resolve_provider_config(provider='custom', api_key='test', model='test', base_url='https://example.invalid')
        with patch('translate.call_chat_completion', return_value=json.dumps([entry('force plate', '测力台'), entry('invented', '不存在')] )) as call:
            terms, _ = extract_candidates('The force plate measures ground reaction force. ' * 1000, config)
        self.assertLessEqual(len(json.loads(call.call_args.args[1][-1]['content'])), 100)
        self.assertTrue(all(e['source'] != 'invented' for e in terms))


class GlossaryTranslationTests(unittest.TestCase):
    def test_only_affected_segments_retranslate_and_off_reuses_original(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'input'
            source.mkdir()
            parts = ['Force plate.', 'Other text.']
            (source / 'full.md').write_text('\n\n'.join(parts), encoding='utf-8')
            def fake(config, messages):
                text = re.search(r'<SOURCE>\n(.*?)\n</SOURCE>', messages[-1]['content'], re.S)[1]
                return text.replace('Force plate.', '测力平台。' if '测力平台' in messages[0]['content'] else '测力台。').replace('Other text.', '其他文字。')
            kwargs = dict(provider='custom', api_key='test', model='test', base_url='https://example.invalid', max_workers=1)
            with patch('translate.split_body_into_segments', return_value=parts), patch('translate.call_chat_completion', side_effect=fake) as call:
                translate.run(source, root / 'out', **kwargs)
                baseline = call.call_count
                translate.run(source, root / 'out', glossary=snapshot(entry('irrelevant', '无关')), **kwargs)
                self.assertEqual(call.call_count, baseline)
                translate.run(source, root / 'out', glossary=snapshot(entry('force plate', '测力平台')), **kwargs)
                self.assertEqual(call.call_count, baseline + 1)
                translate.run(source, root / 'out', glossary=snapshot(entry('force plate', '测力平台'), entry('irrelevant', '无关')), **kwargs)
                self.assertEqual(call.call_count, baseline + 1)
                translate.run(source, root / 'out', **kwargs)
                self.assertEqual(call.call_count, baseline + 1)

    def test_table_cell_cache_uses_only_its_matched_terms(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as td:
            config = translate.resolve_provider_config(provider='custom', api_key='test', model='test', base_url='https://example.invalid')
            config = replace(config, glossary=GlossaryMatcher(snapshot(entry('force plate', '测力平台'))))
            def fake(cfg, messages):
                values = json.loads(messages[-1]['content'])
                return json.dumps([v.replace('Force plate', '测力平台').replace('Other', '其他') for v in values], ensure_ascii=False)
            with patch('translate.call_chat_completion', side_effect=fake) as call:
                result = translate.translate_table_cells(['Force plate', 'Other'], config, Path(td))
                count = call.call_count
                self.assertEqual(result, ['测力平台', '其他'])
                config = replace(config, glossary=GlossaryMatcher(snapshot(entry('force plate', '测力平台'), entry('unknown', '未知'))))
                translate.translate_table_cells(['Force plate', 'Other'], config, Path(td))
                self.assertEqual(call.call_count, count)
