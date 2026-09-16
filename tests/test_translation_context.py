from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import translate
from translation_context import neighbor, table_contexts, stable_content_ids
from table_utils import parse_html_tables


class ContextTests(unittest.TestCase):
    def test_equivalent_numbers_and_changed_sign(self):
        from translation_checks import numbers
        self.assertEqual(numbers('1,000 and 1.00'), numbers('1000 and 1'))
        self.assertEqual(numbers('−2 and 1e-3'), numbers('-2 and 0.001'))
        self.assertNotEqual(numbers('-2'), numbers('2'))
        self.assertEqual(numbers('10–12'), numbers('10-12'))
    def test_neighbors_do_not_cut_sentences_or_include_tables(self):
        self.assertEqual(neighbor('First sentence.\n\nSecond sentence.', tail=True, limit=20), 'Second sentence.')
        self.assertEqual(neighbor('<table><tr><td>Do not include</td></tr></table>'), '')
        self.assertEqual(neighbor('x' * 100, limit=20), '')

    def test_cell_context_respects_spanning_header(self):
        tables = parse_html_tables('<table><tr><th rowspan="2">Group</th><th colspan="2">Duration</th></tr><tr><th>Mean</th><th>SD</th></tr><tr><td>Control</td><td>45</td><td>3</td></tr></table>')
        context = table_contexts(tables, 'Table 1')[5]
        self.assertEqual(context['column_headers'], ['Duration', 'Mean'])
        self.assertEqual(context['row_header'], 'Control')
        self.assertEqual(context['id'], 'r3c2')

    def test_same_word_different_context_does_not_reuse_translation(self):
        config = replace(translate.resolve_provider_config(provider='custom', base_url='https://example.invalid', api_key='fixture', model='test'), use_context=True)
        contexts = [{'id': 'r1c1', 'table': 'Clinical research'}, {'id': 'r1c1', 'table': 'Javelin attempts'}]
        prompts = []
        def fake(config, messages):
            prompts.append(messages[0]['content'])
            return json.dumps(['临床试验' if 'Clinical' in prompts[-1] else '试投'])
        with tempfile.TemporaryDirectory() as td, patch('translate.call_chat_completion', side_effect=fake):
            for ctx, expected in zip(contexts, ['临床试验', '试投']):
                result = translate.translate_table_cells(['Trial'], config, Path(td), contexts=[ctx])
                self.assertEqual(result, [expected])
            translate.translate_table_cells(['Trial'], config, Path(td), contexts=[contexts[0]])
        self.assertEqual(len(prompts), 2)

    def test_stable_ids_survive_repartition_and_keep_duplicates_separate(self):
        units = [{'kind': 'text', 'source': 'Same', 'id': 'old1'}, {'kind': 'text', 'source': 'Same', 'id': 'old2'}]
        old = {'segments': [{'units': [dict(u) for u in units]}]}
        new = {'segments': [{'units': [dict(units[0])]}, {'units': [dict(units[1])]}]}
        stable_content_ids(new, old)
        values = [s['units'][0] for s in new['segments']]
        self.assertEqual([u['id'] for u in values], ['old1', 'old2'])
        self.assertNotEqual(values[0]['content_id'], values[1]['content_id'])

    def test_over_budget_does_not_send_request(self):
        config = replace(translate.resolve_provider_config(api_key='fixture'), use_context=True, context_budget=4096)
        with patch('translate.http_request', side_effect=AssertionError('network')):
            with self.assertRaisesRegex(ValueError, '预算'):
                translate.call_chat_completion(config, [{'role': 'user', 'content': 'x' * 5000}])
