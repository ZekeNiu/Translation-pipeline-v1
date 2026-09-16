import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from task_paths import artifact, translation_lock
from task_state import write_json, fingerprint, read_json
from review import save_edit, set_disposition, load_edits, reexport
from review_index import ReviewIndex
from review_state import revision_state, publish, OUTPUTS


def unit(key='a', **kwargs):
    return dict(id=key, source='Force 3.', translated='力为 3。', source_hash=fingerprint('Force 3.'),
                kind='text', chapter='Methods', segment=1, page=1, issues=[], glossary_terms=[], **kwargs)


class LongReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.doc = {'version': 1, 'identity': 'test', 'segments': [{'units': [unit()]}]}
        write_json(self.out / 'review_document.json', self.doc)

    def test_save_and_decision_do_not_export_or_call_model(self):
        with patch('review._export_review', side_effect=AssertionError('export')), patch('translate.call_chat_completion', side_effect=AssertionError('model')):
            save_edit(self.out, 'a', '测得力为 3。', export=False)
            set_disposition(self.out, ['a'], 'deferred')
        state = revision_state(self.out)
        self.assertEqual((state['revision'], state['exported_revision']), (2, 0))
        self.assertTrue((artifact(self.out, '_schema_backups') / 'review-v1' / 'review_document.json').is_file())
        self.assertFalse(artifact(self.out, '_review_history').exists())

    def test_decision_invalidated_by_changed_text_and_can_reopen(self):
        set_disposition(self.out, ['a'], 'deferred')
        edits = load_edits(self.out, 'test')
        self.assertEqual(ReviewIndex(self.doc, edits).rows['a']['status'], 'deferred')
        save_edit(self.out, 'a', '测得力为 3。', export=False)
        self.assertEqual(ReviewIndex(self.doc, load_edits(self.out, 'test')).rows['a']['status'], 'open')
        set_disposition(self.out, ['a'], 'open')

    def test_group_table_and_filter_without_rechecking(self):
        table = unit('table')
        table.update(kind='table', cells=[{**unit('cell'), 'kind': 'cell', 'translated': '力为 4。'}])
        self.doc['segments'][0]['units'].append(table)
        index = ReviewIndex(self.doc, {'edits': {}})
        with patch('review_index.active_issues', side_effect=AssertionError('recheck')):
            self.assertEqual(index.select(), ['table'])
            self.assertEqual(index.select(query='4'), ['table'])
        self.assertIn('3', index.rows['cell']['detail'])
        self.assertIn('4', index.rows['cell']['detail'])

    def test_publication_failure_restores_all_artifacts(self):
        with translation_lock(self.out):
            stage = artifact(self.out, '_review_export') / 'test'
            for name in OUTPUTS:
                path = artifact(self.out, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('old-' + name, encoding='utf-8')
                path = artifact(stage, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('new-' + name, encoding='utf-8')
            from review_state import copy_atomic
            calls = 0
            def fail_once(source, target):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError('simulated interrupted replace')
                copy_atomic(source, target)
            with patch('review_state.copy_atomic', side_effect=fail_once):
                with self.assertRaises(OSError):
                    publish(self.out, stage, 1)
            for name in OUTPUTS:
                self.assertEqual(artifact(self.out, name).read_text(encoding='utf-8'), 'old-' + name)

    def test_omission_dismissal_requires_reason_and_is_reversible(self):
        self.doc['omissions'] = [{'id': 'om', 'source': 'Event logo', 'reason': '疑似遗漏', 'page': 1}]
        write_json(self.out / 'review_document.json', self.doc)
        with self.assertRaises(ValueError):
            set_disposition(self.out, ['om'], 'dismissed')
        set_disposition(self.out, ['om'], 'dismissed', note='核对原页：图中已包含')
        index = ReviewIndex(self.doc, load_edits(self.out, 'test'))
        self.assertNotIn('om', index.select())
        set_disposition(self.out, ['om'], 'open')
        self.assertIn('om', ReviewIndex(self.doc, load_edits(self.out, 'test')).select())
