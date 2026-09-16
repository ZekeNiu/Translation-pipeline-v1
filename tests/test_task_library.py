from pathlib import Path
import tempfile
import unittest
from task_library import TaskLibrary, clean_temporary
from task_paths import artifact
from task_state import write_json, read_json, task_lock


class LibraryTests(unittest.TestCase):
    def test_pending_task_records_exclude_secrets_and_move_to_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            library = TaskLibrary(root / 'tasks.json')
            pending = library.start_intake({'input_path': 'book.pdf', 'source_mode': 'MinerU API', 'provider': 'custom',
                                           'api_key': 'private-key', 'mineru_key': 'private-parser-key'})
            text = artifact(pending, 'task_state.json').read_text(encoding='utf-8')
            self.assertNotIn('private', text)
            self.assertEqual(library.rows()[0]['title'], 'book')
            result = root / 'translated'
            library.finish_intake(pending, result, 'completed')
            self.assertEqual(library.paths(), [str(result.resolve())])

    def test_cleanup_preserves_manual_data_and_original_pages_and_rejects_active_export(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('review_edits.json', '_review_pages/page_1.pdf', '_review_export/stale.tmp', '_review_candidates/candidate.json'):
                path = artifact(root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', encoding='utf-8')
            with task_lock(artifact(root, '_export_lock')):
                with self.assertRaises(RuntimeError):
                    clean_temporary(root)
            clean_temporary(root)
            self.assertTrue(artifact(root, 'review_edits.json').is_file())
            self.assertTrue(artifact(root, '_review_pages/page_1.pdf').is_file())
            self.assertFalse(artifact(root, '_review_export/stale.tmp').exists())
