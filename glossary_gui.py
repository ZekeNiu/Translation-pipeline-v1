"""Small desktop editor for global and document-specific glossaries."""
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from glossary import merge_entries, read_csv, write_csv, extract_candidates, term_key
from task_state import redact


class GlossaryWindow:
    def __init__(self, parent, store, book_id, prepare, config, busy=None):
        self.store, self.book_id, self.prepare, self.config = store, book_id, prepare, config
        self.busy_callback = busy or (lambda value: None)
        self.window = tk.Toplevel(parent)
        self.window.title('专业术语表')
        self.window.geometry('900x600')
        self.book, self.common = store.load(book_id), store.load()
        self.events, self.working = queue.Queue(), False
        self.global_enabled = tk.BooleanVar(value=self.book.get('global_enabled', False))
        self.book_enabled = tk.BooleanVar(value=self.book.get('book_enabled', False))
        bar = ttk.Frame(self.window, padding=10)
        bar.pack(fill='x')
        ttk.Checkbutton(bar, text='本书启用全局术语表', variable=self.global_enabled, command=self.save).pack(side='left')
        ttk.Checkbutton(bar, text='本书启用专用术语表', variable=self.book_enabled, command=self.save).pack(side='left', padx=15)
        ttk.Label(self.window, text='本书译法优先；仅发送当前文字命中的术语。修改从下一次任务生效。').pack(anchor='w', padx=12)
        self.tabs = ttk.Notebook(self.window)
        self.tabs.pack(fill='both', expand=True, padx=10, pady=10)
        self.editors = []
        for title, data in (('全局通用', self.common), ('本书专用', self.book)):
            frame = ttk.Frame(self.tabs, padding=8)
            self.tabs.add(frame, text=title)
            search = tk.StringVar()
            ttk.Label(frame, text='搜索').pack(anchor='w')
            ttk.Entry(frame, textvariable=search).pack(fill='x')
            tree = ttk.Treeview(frame, columns=('source', 'target', 'note', 'override'), show='headings', selectmode='browse')
            for key, label, width in (('source', '原词', 190), ('target', '指定译法', 190), ('note', '备注（不发送）', 220), ('override', '优先级', 110)):
                tree.heading(key, text=label)
                tree.column(key, width=width)
            scroll = ttk.Scrollbar(frame, command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')
            tree.pack(fill='both', expand=True)
            entries = {}
            row = ttk.Frame(frame)
            row.pack(fill='x', pady=8)
            for key, label in (('source', '原词'), ('target', '译法'), ('note', '备注')):
                ttk.Label(row, text=label).pack(side='left')
                variable = entries[key] = tk.StringVar()
                ttk.Entry(row, textvariable=variable, width=20).pack(side='left', padx=4)
            editor = {'data': data, 'tree': tree, 'search': search, 'entries': entries}
            self.editors.append(editor)
            controls = ttk.Frame(frame)
            controls.pack(fill='x')
            for label, command in [('清空 / 新建', lambda e=editor: self.clear(e)), ('保存条目', lambda e=editor: self.add(e)), ('删除选中', lambda e=editor: self.delete(e)),
                                   ('导入 CSV', lambda e=editor: self.import_file(e)), ('导出 CSV', lambda e=editor: self.export_file(e))]:
                ttk.Button(controls, text=label, command=command).pack(side='left', padx=3)
            search.trace_add('write', lambda *_args, e=editor: self.refresh(e))
            tree.bind('<<TreeviewSelect>>', lambda _event, e=editor: self.select(e))
        bottom = ttk.Frame(self.window, padding=10)
        bottom.pack(fill='x')
        self.extract_button = ttk.Button(bottom, text='提取本书术语建议（调用翻译服务）', command=self.extract)
        self.extract_button.pack(side='left')
        self.status = ttk.Label(bottom, text='导入或编辑不会调用模型。')
        self.status.pack(side='left', padx=10)
        for editor in self.editors:
            self.refresh(editor)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.timer = self.window.after(100, self.poll)

    def conflict(self, previous, new):
        return messagebox.askyesno('同层术语冲突', f"{new['source']}\n现有：{previous['target']}\n导入：{new['target']}\n\n使用新译法？选择“否”保留现有译法。", parent=self.window)

    def save(self):
        try:
            self.book.update(global_enabled=self.global_enabled.get(), book_enabled=self.book_enabled.get())
            self.store.save(self.common)
            self.store.save(self.book, self.book_id)
            for editor in getattr(self, 'editors', []):
                self.refresh(editor)
        except Exception as exc:
            messagebox.showerror('术语保存失败', str(exc), parent=self.window)

    def refresh(self, editor):
        tree = editor['tree']
        tree.delete(*tree.get_children())
        query = editor['search'].get().casefold()
        global_keys = {term_key(e['source']) for e in self.common['entries']}
        for i, entry in enumerate(editor['data']['entries']):
            if query and query not in ' '.join(entry.values()).casefold():
                continue
            override = '覆盖全局' if editor['data'] is self.book and term_key(entry['source']) in global_keys else ''
            tree.insert('', 'end', iid=str(i), values=(entry['source'], entry['target'], entry['note'], override))

    def select(self, editor):
        selection = editor['tree'].selection()
        if selection:
            entry = editor['data']['entries'][int(selection[0])]
            for key, variable in editor['entries'].items():
                variable.set(entry[key])

    def add(self, editor):
        try:
            entry = {k: v.get() for k, v in editor['entries'].items()}
            previous = list(editor['data']['entries'])
            selection = editor['tree'].selection()
            if selection:
                previous.pop(int(selection[0]))
            editor['data']['entries'] = merge_entries(previous, [entry], self.conflict)
            self.save()
        except ValueError as exc:
            messagebox.showerror('术语未保存', str(exc), parent=self.window)

    def clear(self, editor):
        editor['tree'].selection_remove(*editor['tree'].selection())
        for variable in editor['entries'].values():
            variable.set('')

    def delete(self, editor):
        selection = editor['tree'].selection()
        if selection:
            editor['data']['entries'].pop(int(selection[0]))
            self.save()

    def import_file(self, editor):
        path = filedialog.askopenfilename(parent=self.window, filetypes=[('术语 CSV', '*.csv')])
        if path:
            try:
                editor['data']['entries'] = merge_entries(editor['data']['entries'], read_csv(path), self.conflict)
                self.save()
            except Exception as exc:
                messagebox.showerror('导入失败', str(exc), parent=self.window)

    def export_file(self, editor):
        path = filedialog.asksaveasfilename(parent=self.window, defaultextension='.csv', filetypes=[('术语 CSV', '*.csv')])
        if path:
            try:
                write_csv(path, editor['data']['entries'])
            except Exception as exc:
                messagebox.showerror('导出失败', str(exc), parent=self.window)

    def extract(self):
        if self.working:
            return
        self.working = True
        self.busy_callback(True)
        self.extract_button.configure(state='disabled')
        self.status.configure(text='准备原文并提取候选词…')
        def task():
            try:
                source = self.prepare()
                proposed, metrics = extract_candidates(source, self.config)
                self.events.put(('suggestions', (proposed, metrics)))
            except Exception as exc:
                self.events.put(('error', redact(exc, [self.config.api_key])))
        threading.Thread(target=task, daemon=True).start()

    def poll(self):
        try:
            kind, value = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.working = False
            self.busy_callback(False)
            self.extract_button.configure(state='normal')
            if kind == 'error':
                self.status.configure(text='提取失败，词库未改变。')
                messagebox.showerror('术语提取失败', value, parent=self.window)
            else:
                proposed, metrics = value
                self.book.setdefault('extraction_requests', []).extend(metrics)
                self.save()
                self.status.configure(text=f'获得 {len(proposed)} 条候选；请选择要采用的条目。')
                self.show_candidates(proposed)
        self.timer = self.window.after(100, self.poll)

    def show_candidates(self, proposed):
        popup = tk.Toplevel(self.window)
        popup.title('选择本书术语建议')
        popup.geometry('700x420')
        tree = ttk.Treeview(popup, columns=('source', 'target'), show='headings', selectmode='extended')
        tree.heading('source', text='原词')
        tree.heading('target', text='建议译法')
        tree.pack(fill='both', expand=True, padx=10, pady=10)
        for i, item in enumerate(proposed):
            tree.insert('', 'end', iid=str(i), values=(item['source'], item['target']))
        def accept():
            self.book['entries'] = merge_entries(self.book['entries'], [proposed[int(i)] for i in tree.selection()], self.conflict)
            self.save()
            popup.destroy()
        ttk.Button(popup, text='采用选中条目到本书词库', command=accept).pack(pady=8)

    def close(self):
        if self.working:
            self.status.configure(text='正在处理，请等待当前提取结束后关闭。')
            return
        self.window.after_cancel(self.timer)
        self.window.destroy()
