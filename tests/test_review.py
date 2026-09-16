from task_paths import artifact
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import translate
from glossary import GlossarySnapshot
from mineru_sidecar import load_mineru_sidecar
from review import all_units, save_edit, reexport, propose_translation, recover_existing, extract_original_page, load_edits
from task_state import read_json, write_json, task_lock, fingerprint, file_hash
from pdf_helpers import make_pdf


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.md = self.source / 'full.md'
        self.out = self.root / 'out'
        self.kwargs = dict(provider='custom', base_url='https://example.invalid', api_key='test', model='model', max_workers=1)
        self.config = translate.resolve_provider_config(**{k: v for k, v in self.kwargs.items() if k != 'max_workers'})

    def fake(self, config, messages):
        value = messages[-1]['content']
        if value.startswith('['):
            return json.dumps([text.replace('Force', '力').replace('Other', '其他') for text in json.loads(value)], ensure_ascii=False)
        return re.search(r'<SOURCE>\n(.*?)\n</SOURCE>', value, re.S)[1].replace('Force', '力').replace('Other', '其他')

    def run_job(self, **kwargs):
        with patch('translate.call_chat_completion', side_effect=self.fake):
            return translate.run(self.source, self.out, **self.kwargs, **kwargs)

    def units(self):
        return list(all_units(read_json(artifact(self.out, 'review_document.json'))))

    def test_manual_edit_survives_resume_and_restore(self):
        self.md.write_text('Force 3.\n\nOther 4.', encoding='utf-8')
        self.run_job()
        unit = self.units()[0]
        save_edit(self.out, unit['id'], '测得力为 3。')
        with patch('translate.call_chat_completion', side_effect=AssertionError('No model call on resume')):
            translate.run(self.source, self.out, **self.kwargs)
        self.assertIn('测得力为 3。', (self.out / 'translated.md').read_text(encoding='utf-8'))
        save_edit(self.out, unit['id'], '', restore=True, allow_warnings=True)
        self.assertIn(unit['translated'], (self.out / 'translated.md').read_text(encoding='utf-8'))
        self.assertTrue(list((artifact(self.out, '_review_history')).glob('*/translated.docx')))

    def test_formula_structure_cannot_be_overwritten(self):
        self.md.write_text('Force $x^2$ is 3.', encoding='utf-8')
        self.run_job()
        unit = self.units()[0]
        with self.assertRaisesRegex(ValueError, '结构|公式'):
            save_edit(self.out, unit['id'], '力 $y^2$ 为 3。', allow_warnings=True)
        self.assertFalse((artifact(self.out, 'review_edits.json')).exists())

    def test_numeric_warning_requires_explicit_acceptance(self):
        self.md.write_text('Force 3.', encoding='utf-8')
        self.run_job()
        unit = self.units()[0]
        with self.assertRaisesRegex(ValueError, '数字'):
            save_edit(self.out, unit['id'], '力为 4。')
        save_edit(self.out, unit['id'], '力为 4。', allow_warnings=True)
        self.assertEqual(read_json(artifact(self.out, 'quality_report.json'))['status'], 'needs_review')

    def test_export_failure_retains_edit_and_can_retry_without_model(self):
        self.md.write_text('Force 3.', encoding='utf-8')
        self.run_job()
        unit = self.units()[0]
        previous = (self.out / 'translated.docx').read_bytes()
        with patch('make_docx.make_docx', side_effect=OSError('Word file is open')):
            with self.assertRaises(OSError):
                save_edit(self.out, unit['id'], '测得力为 3。')
        self.assertEqual((self.out / 'translated.docx').read_bytes(), previous)
        self.assertTrue(read_json(artifact(self.out, 'quality_report.json'))['review_export_pending'])
        with patch('translate.call_chat_completion', side_effect=AssertionError('No model on export')):
            reexport(self.out)
        self.assertFalse(read_json(artifact(self.out, 'quality_report.json'))['review_export_pending'])
        self.assertIn('测得力为 3。', (self.out / 'translated.md').read_text(encoding='utf-8'))

    def test_duplicate_paragraphs_are_separate_and_table_cell_edit_preserves_spans(self):
        self.md.write_text('Force.\n\nForce.\n\n<table><tr><td rowspan="2">Force</td><td>3</td></tr><tr><td>4</td></tr></table>', encoding='utf-8')
        self.run_job()
        units = self.units()
        self.assertNotEqual(units[0]['id'], units[1]['id'])
        save_edit(self.out, units[1]['id'], '测得力。')
        cell = next(u for u in units if u['kind'] == 'cell' and u['source'] == 'Force')
        save_edit(self.out, cell['id'], '力量')
        result = (self.out / 'translated.md').read_text(encoding='utf-8')
        self.assertTrue(result.startswith('力.\n\n测得力。'))
        self.assertIn('rowspan="2">力量', result)
        self.assertIn('<td>4</td>', result)

    def test_selected_retranslation_is_candidate_only(self):
        self.md.write_text('Force 3.\n\nOther 4.', encoding='utf-8')
        self.run_job()
        unit = self.units()[0]
        original = (self.out / 'translated.md').read_text(encoding='utf-8')
        with patch('translate.call_chat_completion', side_effect=self.fake) as call:
            candidate = propose_translation(self.out, unit['id'], self.config)
        self.assertIn('力', candidate)
        source_payload = re.search(r'<SOURCE>\n(.*?)\n</SOURCE>', call.call_args.args[1][-1]['content'], re.S)[1]
        self.assertNotIn('Other', source_payload)
        self.assertEqual((self.out / 'translated.md').read_text(encoding='utf-8'), original)

    def test_glossary_change_preserves_manual_edit_and_marks_it(self):
        self.md.write_text('Force 3.', encoding='utf-8')
        self.run_job()
        unit = self.units()[0]
        save_edit(self.out, unit['id'], '测得力为 3。')
        terms = GlossarySnapshot(True, False, ({'source': 'Force', 'target': '力量', 'note': ''},))
        self.run_job(glossary=terms)
        self.assertIn('测得力为 3。', (self.out / 'translated.md').read_text(encoding='utf-8'))
        self.assertTrue(read_json(artifact(self.out, 'quality_report.json'))['manual_review'])

    def test_offline_recovery_uses_validated_caches(self):
        self.md.write_text('Force 3.', encoding='utf-8')
        self.run_job()
        state = read_json(artifact(self.out, 'task_state.json'))
        for record in state['segments'].values():
            record.pop('alignment')
        write_json(artifact(self.out, 'task_state.json'), state)
        (artifact(self.out, 'review_document.json')).unlink()
        with patch('translate.http_request', side_effect=AssertionError('No network during recovery')):
            document = recover_existing(self.out, self.config)
        self.assertEqual(next(all_units(document))['kind'], 'text')

    def test_page_mapping_and_source_replacement_guard(self):
        self.md.write_text('Force 3.\n\nOther 4.', encoding='utf-8')
        write_json(self.source / 'sample_content_list.json', [
            {'type': 'text', 'text': 'Force 3.', 'page_idx': 1, 'bbox': [0, 0, 100, 100]},
            {'type': 'text', 'text': 'Other 4.', 'page_idx': 2}])
        pdf = make_pdf(self.root / 'source.pdf', 3)
        self.run_job(source_info={'path': str(pdf), 'hash': file_hash(pdf)})
        first = self.units()[0]
        self.assertEqual(first['page'], 2)
        page = extract_original_page(self.out, first['id'])
        from pypdf import PdfReader
        self.assertEqual(len(PdfReader(page).pages), 1)
        pdf.write_bytes(b'replaced')
        with self.assertRaisesRegex(ValueError, '替换'):
            extract_original_page(self.out, first['id'])

    def test_missing_and_ambiguous_pages_stay_unlocated(self):
        self.md.write_text('Force.', encoding='utf-8')
        write_json(self.source / 'sample_content_list.json', [
            {'type': 'text', 'text': 'Force.', 'page_idx': 0}, {'type': 'text', 'text': 'Force.', 'page_idx': 1}])
        self.run_job()
        self.assertIsNone(self.units()[0]['page'])

    def test_lock_and_stale_revision_prevent_overwrite(self):
        self.md.write_text('Force 3.', encoding='utf-8')
        self.run_job()
        unit = self.units()[0]
        document = read_json(artifact(self.out, 'review_document.json'))
        revision = fingerprint(document, load_edits(self.out, document['identity']))
        with task_lock(self.out / "_internal"):
            with self.assertRaises(RuntimeError):
                save_edit(self.out, unit['id'], '力为 3。')
        save_edit(self.out, unit['id'], '力为 3。', expected_revision=revision)
        with self.assertRaisesRegex(ValueError, '刷新'):
            save_edit(self.out, unit['id'], '作用力为 3。', expected_revision=revision)
