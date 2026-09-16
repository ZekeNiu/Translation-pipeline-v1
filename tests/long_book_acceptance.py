"""Offline 10k-unit performance check; --gui also measures actual Tk operations."""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_paths import artifact
from task_state import write_json, fingerprint
from review_index import ReviewIndex
from review import save_edit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gui', action='store_true')
    args = parser.parse_args()
    out = Path(__file__).parent / '.artifacts' / 'long-book-performance'
    out.mkdir(parents=True, exist_ok=True)
    units = [dict(id=f'u{i}', kind='text', source=f'Force {i}.', translated=f'力为 {i}。',
                  source_hash=fingerprint(f'Force {i}.'), chapter=f'Chapter {i // 100}',
                  segment=1, page=i // 10 + 1, issues=[], glossary_terms=[]) for i in range(10000)]
    document = {'version': 1, 'identity': 'synthetic-10000', 'segments': [{'units': units}]}
    write_json(artifact(out, 'review_document.json'), document)
    start = time.perf_counter()
    index = ReviewIndex(document, {'edits': {}})
    results = {'units': len(units), 'index_seconds': time.perf_counter() - start}
    start = time.perf_counter()
    index.select(query='999', issues_only=False)
    results['search_seconds'] = time.perf_counter() - start
    start = time.perf_counter()
    with patch('translate.call_chat_completion', side_effect=AssertionError('No model')), patch('review._export_review', side_effect=AssertionError('No export')):
        save_edit(out, 'u999', '测得力为 999。', export=False)
    results['save_seconds'] = time.perf_counter() - start
    if args.gui:
        import tkinter as tk
        from review_gui import ReviewWindow
        root = tk.Tk()
        root.withdraw()
        start = time.perf_counter()
        window = ReviewWindow(root, out, lambda _: None)
        while window.working:
            root.update()
            time.sleep(.01)
        root.update()
        results['gui_load_seconds'] = time.perf_counter() - start
        window.filter.set(False)
        window.refresh_list()
        start = time.perf_counter()
        window.query.set('999')
        # Include the intended debounce, not just raw Python filtering.
        while window.search_timer:
            root.update()
            time.sleep(.005)
        results['gui_search_seconds'] = time.perf_counter() - start
        start = time.perf_counter()
        key = window.tree.get_children()[0]
        window.tree.selection_set(key)
        window.select()
        root.update()
        results['gui_switch_seconds'] = time.perf_counter() - start
        window.window.after_cancel(window.timer)
        window.window.destroy()
        root.destroy()
    write_json(out / 'acceptance.json', results)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    assert results['save_seconds'] < 1
    assert results.get('gui_search_seconds', results['search_seconds']) < .5
    assert results.get('gui_switch_seconds', 0) < .5


if __name__ == '__main__':
    main()
