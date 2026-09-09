import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from settings_store import SettingsStore, SettingsError
from task_state import atomic_write


class SettingsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "DPAPI requires Windows")
    def test_round_trip_and_clear_keys(self):
        with tempfile.TemporaryDirectory() as td:
            store = SettingsStore(Path(td) / "settings.json")
            data = {"providers": {"custom": {"api_key": "test-private-key", "model": "model-a"}},
                    "mineru": {"url": "https://mineru.net", "api_key": "test-mineru-key"}}
            store.save(data)
            raw = store.path.read_text(encoding="utf-8")
            self.assertNotIn("test-private-key", raw)
            self.assertNotIn("test-mineru-key", raw)
            self.assertEqual(store.load()["mineru"], data["mineru"])
            data["mineru"]["api_key"] = ""
            store.save(data)
            self.assertEqual(store.load()["mineru"]["api_key"], "")

    def test_corrupt_settings_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            store = SettingsStore(Path(td) / "settings.json")
            store.path.write_text("{corrupt", encoding="utf-8")
            with self.assertRaises(SettingsError):
                store.load()
            self.assertEqual(store.path.read_text(), "{corrupt")

    def test_failed_atomic_save_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            atomic_write(target, "old")
            with patch("task_state.os.replace", side_effect=PermissionError):
                with self.assertRaises(OSError):
                    atomic_write(target, "new")
            self.assertEqual(target.read_text(), "old")
            self.assertEqual(list(Path(td).glob("*.tmp")), [])


class GuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from gui import App
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        def cleanup():
            self.root.after_cancel(self.app.queue_timer)
            if self.app.save_timer:
                self.root.after_cancel(self.app.save_timer)
            self.root.destroy()
        self.addCleanup(cleanup)
        with patch.dict(os.environ, {"AI_PROVIDER": "custom", "AI_API_KEY": "", "MINERU_API_KEY": ""}):
            self.app = App(self.root, SettingsStore(Path(self.tmp.name) / "settings.json"))

    @unittest.skipUnless(os.name == "nt", "DPAPI requires Windows")
    def test_reopened_gui_restores_api_mode_and_cleared_key(self):
        import tkinter as tk
        from gui import App, SOURCE_API, SOURCE_EXISTING_FOLDER
        self.app.vars['source_mode'].set(SOURCE_API)
        self.app.vars['mineru_url'].set('https://mineru.net')
        self.app.vars['mineru_key'].set('test-mineru-key')
        self.app._save_settings()
        self.app.vars['source_mode'].set(SOURCE_EXISTING_FOLDER)
        self.app._update_source_mode()
        self.app.vars['source_mode'].set(SOURCE_API)
        self.app.vars['mineru_key'].set('')
        self.app._save_settings()
        window = tk.Toplevel(self.root)
        window.withdraw()
        restored = App(window, self.app.store)
        try:
            self.assertEqual(restored.vars['source_mode'].get(), SOURCE_API)
            self.assertEqual(restored.vars['mineru_url'].get(), 'https://mineru.net')
            self.assertEqual(restored.vars['mineru_key'].get(), '')
        finally:
            window.after_cancel(restored.queue_timer)
            window.destroy()

    def test_provider_switch_restores_independent_settings(self):
        self.app.vars["base_url"].set("https://example.invalid/v1")
        self.app.vars["model"].set("custom-model")
        self.app.vars["provider"].set("deepseek")
        self.app._apply_provider_defaults()
        self.assertNotEqual(self.app.vars["model"].get(), "custom-model")
        self.app.vars["provider"].set("custom")
        self.app._apply_provider_defaults()
        self.assertEqual(self.app.vars["model"].get(), "custom-model")
        self.assertEqual(self.app.store.load()["providers"]["custom"]["base_url"], "https://example.invalid/v1")

    def test_snapshot_is_independent_of_later_edits(self):
        self.app.vars["model"].set("first")
        options = self.app._snapshot()
        self.app.vars["model"].set("second")
        self.assertEqual(options["model"], "first")

    def test_background_exception_message_survives_queue(self):
        try:
            raise ValueError("example error")
        except Exception as exc:
            self.app.events.put(("failed", (str(exc), "trace")))
        self.root.after_cancel(self.app.queue_timer)
        self.app._drain_queue()
        self.assertIn("example error", self.app.progress_label["text"])

    def test_save_error_visible_without_discarding_input(self):
        self.app.vars["model"].set("retained")
        with patch.object(self.app.store, "save", side_effect=SettingsError("保存失败")):
            self.app._save_settings()
        self.assertEqual(self.app.vars["model"].get(), "retained")
        self.assertIn("失败", self.app.save_label["text"])
