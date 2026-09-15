"""Bilingual text review with explicit candidate adoption and export-only retry."""
from pathlib import Path
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from glossary import GlossaryMatcher, GlossarySnapshot
from review import (all_units, effective_text, load_edits, save_edit, reexport, propose_translation,
                    extract_original_page, validate_edit, recover_existing, current_glossary)
from task_state import read_json, fingerprint, redact


class ReviewWindow:
    def __init__(self, parent, output, config_for, readonly=lambda: False, busy=lambda _: None):
        self.out, self.config_for, self.readonly, self.busy_callback = Path(output), config_for, readonly, busy
        self.window = tk.Toplevel(parent)
        self.window.title('原文 — 译文复核')
        self.window.geometry('1150x720')
        self.events, self.working = queue.Queue(), False
        self.document, self.edits, self.units = None, None, []
        self.current = None
        self.controls = []
        top = ttk.Frame(self.window, padding=10)
        top.pack(fill='x')
        self.filter = tk.BooleanVar()
        ttk.Checkbutton(top, text='仅显示待检查项', variable=self.filter, command=self.refresh_list).pack(side='left')
        ttk.Button(top, text='刷新', command=self.load).pack(side='left', padx=6)
        self.status = ttk.Label(top, text='')
        self.status.pack(side='left', padx=12)
        self.tree = ttk.Treeview(self.window, columns=('section', 'page', 'kind', 'issue'), show='headings', height=9, selectmode='browse')
        for key, label, width in (('section', '章节 / 内容', 330), ('page', '原页码', 90), ('kind', '类型', 90), ('issue', '检查项', 540)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.pack(fill='x', padx=10)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        panels = ttk.Panedwindow(self.window, orient='horizontal')
        panels.pack(fill='both', expand=True, padx=10, pady=10)
        left, right = ttk.LabelFrame(panels, text='原文'), ttk.LabelFrame(panels, text='译文 / 重译候选（点击保存后采用）')
        panels.add(left, weight=1)
        panels.add(right, weight=1)
        self.source = scrolledtext.ScrolledText(left, wrap='word', font=('Microsoft YaHei UI', 11), state='disabled')
        self.target = scrolledtext.ScrolledText(right, wrap='word', font=('Microsoft YaHei UI', 11))
        self.source.pack(fill='both', expand=True)
        self.target.pack(fill='both', expand=True)
        self.reason = ttk.Label(self.window, text='', wraplength=1100)
        self.reason.pack(fill='x', padx=10)
        bar = ttk.Frame(self.window, padding=10)
        bar.pack(fill='x')
        for label, callback in [('保存并重新导出', self.save), ('恢复上一版', self.restore), ('只重译选中项', self.retranslate),
                                ('重新导出', lambda: self.action('export', lambda: reexport(self.out))),
                                ('离线恢复对应记录', self.recover)]:
            button = ttk.Button(bar, text=label, command=callback)
            button.pack(side='left', padx=4)
            self.controls.append(button)
        ttk.Button(bar, text='查看原 PDF 页', command=self.open_page).pack(side='right')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.load()
        self.timer = self.window.after(100, self.poll)

    def load(self):
        if self.working:
            return
        self.document = read_json(self.out / 'review_document.json', {})
        if not self.document or self.document.get('version') != 1:
            self.status.configure(text='旧任务缺少对应记录；可点击“离线恢复对应记录”。')
            return
        self.edits = load_edits(self.out, self.document['identity'])
        self.revision = fingerprint(self.document, self.edits)
        self.units = [u for u in all_units(self.document) if u['kind'] != 'table' or not u.get('cells')]
        self.refresh_list()
        self.status.configure(text=f"共 {len(self.units)} 项；人工修订 {len(self.edits['edits'])} 项。")

    def issues(self, unit):
        record = self.edits['edits'].get(unit['id'], {})
        if record.get('history') and record.get('source_hash') == unit['source_hash']:
            last = record['history'][-1]
            issues = list(last.get('issues', []))
            if last.get('glossary_terms', []) != unit.get('glossary_terms', []):
                issues.append('术语已变化，人工译文已保留')
            return issues
        return unit.get('issues', [])

    def refresh_list(self):
        if self.working:
            return
        self.tree.delete(*self.tree.get_children())
        labels = {'text': '正文', 'heading': '标题', 'caption': '图表标题', 'cell': '表格单元格',
                  'reference': '参考文献', 'formula': '公式', 'image': '图片', 'code': '代码', 'unmapped': '未对应', 'table': '表格'}
        for unit in self.units:
            issues = self.issues(unit)
            if self.filter.get() and not issues:
                continue
            self.tree.insert('', 'end', iid=unit['id'], values=(unit['chapter'] + ' · ' + unit['source'][:60],
                unit.get('page') or '未定位', labels.get(unit['kind'], unit['kind']), '；'.join(issues) or ''))
        if self.current and self.tree.exists(self.current):
            self.tree.selection_set(self.current)

    def select(self, _event=None):
        selection = self.tree.selection()
        if not selection or self.working:
            return
        self.current = selection[0]
        unit = next(u for u in self.units if u['id'] == self.current)
        self.source.configure(state='normal')
        self.source.delete('1.0', 'end')
        self.source.insert('1.0', unit['source'])
        self.source.configure(state='disabled')
        self.target.configure(state='normal')
        self.target.delete('1.0', 'end')
        self.target.insert('1.0', effective_text(unit, self.edits))
        self.reason.configure(text='；'.join(self.issues(unit)) or '自动检查未发现问题；仍需结合原文判断语义。')

    def selected(self):
        if not self.current:
            raise ValueError('请先选择一个段落或单元格。')
        return next(u for u in self.units if u['id'] == self.current)

    def action(self, kind, callback):
        if self.working or self.readonly():
            self.status.configure(text='当前有任务运行，复核暂为只读。')
            return
        self.working = True
        self.busy_callback(True)
        self.status.configure(text='正在处理；已有译文和修订会保留。')
        def task():
            try:
                self.events.put((kind, callback()))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=task, daemon=True).start()

    def save(self):
        try:
            unit = self.selected()
            text = self.target.get('1.0', 'end-1c')
            matcher = GlossaryMatcher(current_glossary(self.out))
            issues = validate_edit(unit, text) + matcher.issues(unit['source'], text)
            if issues and not messagebox.askyesno('仍有待核对项', '\n'.join(issues) + '\n\n仍然保留此修订？', parent=self.window):
                return
            revision = self.revision
            self.action('saved', lambda: save_edit(self.out, unit['id'], text, allow_warnings=True, expected_revision=revision))
        except Exception as exc:
            messagebox.showerror('未保存', str(exc), parent=self.window)

    def restore(self):
        try:
            unit, revision = self.selected(), self.revision
            self.action('saved', lambda: save_edit(self.out, unit['id'], '', restore=True, allow_warnings=True, expected_revision=revision))
        except Exception as exc:
            messagebox.showerror('无法恢复', str(exc), parent=self.window)

    def retranslate(self):
        try:
            unit = self.selected()
            config = self.config_for(self.document)
            def propose():
                try:
                    return propose_translation(self.out, unit['id'], config)
                except Exception as exc:
                    raise ValueError(redact(exc, [config.api_key])) from None
            self.action('candidate', propose)
        except Exception as exc:
            messagebox.showerror('不能重译', str(exc), parent=self.window)

    def recover(self):
        try:
            config = self.config_for(self.document or {})
            self.action('recovered', lambda: recover_existing(self.out, config))
        except Exception as exc:
            messagebox.showerror('不能恢复', str(exc), parent=self.window)

    def open_page(self):
        try:
            path = extract_original_page(self.out, self.selected()['id'])
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showinfo('原页不可用', str(exc), parent=self.window)

    def poll(self):
        try:
            kind, value = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.working = False
            self.busy_callback(False)
            if kind == 'error':
                self.status.configure(text='操作未完成；已保存的人工修订可点击“重新导出”。')
                messagebox.showerror('操作未完成', value, parent=self.window)
            elif kind == 'candidate':
                self.target.configure(state='normal')
                self.target.delete('1.0', 'end')
                self.target.insert('1.0', value)
                self.status.configure(text='重译候选已生成；点击“保存并重新导出”后采用。')
            else:
                self.load()
                self.status.configure(text='已保存；导出文件及质量报告已更新。' if kind != 'recovered' else '离线恢复结束；未调用模型。')
        locked = self.working or self.readonly()
        for button in self.controls:
            button.configure(state='disabled' if locked else 'normal')
        self.target.configure(state='disabled' if locked else 'normal')
        self.timer = self.window.after(100, self.poll)

    def close(self):
        if self.working:
            self.status.configure(text='请等待当前操作结束后关闭。')
            return
        self.window.after_cancel(self.timer)
        self.window.destroy()
