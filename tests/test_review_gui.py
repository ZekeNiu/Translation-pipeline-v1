from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from gui import App
from glossary import GlossaryStore
from glossary_gui import GlossaryWindow
from review_gui import ReviewWindow
from settings_store import SettingsStore
from task_state import write_json
import translate


class ReviewGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        import gc
        self.addCleanup(gc.collect)
        self.path = Path(self.temp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_controls_visible_and_active_task_is_readonly(self):
        unit = {'id': 's:b0', 'source': 'Force 3.', 'translated': '力为 3。', 'kind': 'text', 'chapter': 'Methods',
                'segment': 1, 'page': None, 'bbox': None, 'source_hash': 'hash', 'issues': [], 'glossary_terms': []}
        write_json(self.path / 'review_document.json', {'version': 1, 'identity': 'test', 'segments': [{'units': [unit]}]})
        locked = False
        window = ReviewWindow(self.root, self.path, lambda _: None, readonly=lambda: locked)
        try:
            self.root.update()
            window.tree.selection_set('s:b0')
            window.select()
            self.assertIn('力为', window.target.get('1.0', 'end'))
            for button in window.controls:
                self.assertLess(button.winfo_rooty() + button.winfo_height(), window.window.winfo_rooty() + window.window.winfo_height() + 1)
            locked = True
            window.window.after_cancel(window.timer)
            window.poll()
            self.assertEqual(str(window.target['state']), 'disabled')
            self.assertTrue(all(str(b['state']) == 'disabled' for b in window.controls))
        finally:
            window.window.after_cancel(window.timer)
            window.window.destroy()

    def test_glossary_toggle_and_editor_save_survive_reopen(self):
        store = GlossaryStore(self.path / 'glossaries')
        book = 'a' * 64
        window = GlossaryWindow(self.root, store, book, lambda: '', translate.resolve_provider_config())
        try:
            window.book_enabled.set(True)
            editor = window.editors[1]
            editor['entries']['source'].set('Force')
            editor['entries']['target'].set('力')
            window.add(editor)
            self.assertTrue(store.snapshot(book).book_enabled)
            self.assertEqual(store.snapshot(book).book_entries[0]['target'], '力')
            self.assertFalse(store.snapshot(book).global_enabled)
        finally:
            window.close()

