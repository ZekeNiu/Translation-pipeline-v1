"""Bilingual text review with explicit candidate adoption and export-only retry."""
from task_paths import artifact, translation_lock, public_markdown
from pathlib import Path
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from glossary import GlossaryMatcher, GlossarySnapshot
from review import (all_units, effective_text, load_edits, save_edit, reexport, propose_translation,
                    extract_original_page, validate_edit, recover_existing, current_glossary)
from review import confirm_unit, propose_omission, adopt_omission, omission_unit
from quality_report import active_issues, suggestion
from task_state import read_json, fingerprint, redact
from task_state import write_json


class ReviewWindow:
    def __init__(self, parent, output, config_for, readonly=lambda: False, busy=lambda _: None):
        self.out, self.config_for, self.readonly, self.busy_callback = Path(output), config_for, readonly, busy
        self.window = tk.Toplevel(parent)
        self.window.title('原文 — 译文复核')
        self.window.geometry('1150x720')
        self.window.minsize(920, 620)
        self.events, self.working = queue.Queue(), False
        self.document, self.edits, self.units = None, None, []
        self.current = None
        self.displayed_text = ''
        self.controls = []
        top = ttk.Frame(self.window, padding=10)
        top.pack(fill='x')
        self.filter = tk.BooleanVar()
        ttk.Checkbutton(top, text='仅显示待检查项', variable=self.filter, command=self.refresh_list).pack(side='left')
        ttk.Button(top, text='刷新', command=self.load).pack(side='left', padx=6)
        self.status = ttk.Label(top, text='')
        self.status.pack(side='left', padx=12)
        settings = ttk.Frame(self.window, padding=(10, 0))
        settings.pack(fill='x')
        ttk.Label(settings, text='页眉页脚').pack(side='left')
        from export_layout import MODES
        self.layout_mode = tk.StringVar(value=MODES.get(read_json(artifact(self.out, 'export_options.json'), {}).get('header_mode', 'original'), '保留原样式'))
        ttk.Combobox(settings, values=list(MODES.values()), textvariable=self.layout_mode, state='readonly', width=14).pack(side='left', padx=5)
        for text, callback in [('预览样式', self.preview), ('应用样式并导出', self.apply_layout), ('打开质量报告', self.open_report)]:
            button = ttk.Button(settings, text=text, command=callback)
            button.pack(side='left', padx=5)
            if text != '打开质量报告':
                self.controls.append(button)
        self.query = tk.StringVar()
        search = ttk.Entry(settings, textvariable=self.query, width=24)
        search.pack(side='right')
        ttk.Label(settings, text='搜索编号 / 原译文 ').pack(side='right')
        self.query.trace_add('write', lambda *_: self.refresh_list())
        listing = ttk.Frame(self.window)
        listing.pack(fill='x', padx=10)
        self.tree = ttk.Treeview(listing, columns=('section', 'page', 'kind', 'issue'), show='headings', height=8, selectmode='browse')
        for key, label, width in (('section', '章节 / 内容', 330), ('page', '原页码', 90), ('kind', '类型', 90), ('issue', '检查项', 540)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        scrollbar = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='x', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        panels = ttk.Panedwindow(self.window, orient='horizontal')
        panels.pack(fill='both', expand=True, padx=10, pady=10)
        left, right = ttk.LabelFrame(panels, text='原文'), ttk.LabelFrame(panels, text='译文 / 重译候选（点击保存后采用）')
        panels.add(left, weight=1)
        panels.add(right, weight=1)
        self.source = scrolledtext.ScrolledText(left, wrap='word', font=('Microsoft YaHei UI', 11), height=10, width=40, state='disabled')
        self.target = scrolledtext.ScrolledText(right, wrap='word', font=('Microsoft YaHei UI', 11), height=10, width=40)
        self.source.pack(fill='both', expand=True)
        self.target.pack(fill='both', expand=True)
        self.reason = ttk.Label(self.window, text='', wraplength=1100)
        self.reason.pack(fill='x', padx=10)
        bar = ttk.Frame(self.window, padding=10)
        bar.pack(fill='x')
        for index, (label, callback) in enumerate([('保存并重新导出', self.save), ('恢复上一版', self.restore), ('只重译选中项', self.retranslate),
                                ('重新导出', lambda: self.action('export', lambda: reexport(self.out))),
                                ('离线恢复对应记录', self.recover), ('确认保留', self.confirm), ('恢复漏段', self.retranslate)]):
            button = ttk.Button(bar, text=label, command=callback)
            button.grid(row=index // 4, column=index % 4, padx=4, pady=3, sticky='ew')
            self.controls.append(button)
        ttk.Button(bar, text='查看原 PDF 页', command=self.open_page).grid(row=1, column=3, padx=4)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.load()
        self.timer = self.window.after(100, self.poll)

    def load(self):
        if self.working:
            return
        self.document = read_json(artifact(self.out, 'review_document.json'), {})
        if not self.document or self.document.get('version') != 1:
            self.status.configure(text='旧任务缺少对应记录；可点击“离线恢复对应记录”。')
            return
        self.edits = load_edits(self.out, self.document['identity'])
        self.revision = fingerprint(self.document, self.edits)
        self.units = [u for u in all_units(self.document) if u['kind'] != 'table' or not u.get('cells')]
        recovered = {u.get('omission_id') for u in self.units}
        self.units.extend({**i, 'kind': 'omission', 'chapter': '解析遗漏检查', 'translated': '',
                           'source_hash': fingerprint(i['source']), 'issues': [i['reason']]}
                          for i in self.document.get('omissions', []) if i['id'] not in recovered)
        self.refresh_list()
        self.status.configure(text=f"共 {len(self.units)} 项；人工修订 {len(self.edits['edits'])} 项。")

    def issues(self, unit):
        if unit['kind'] == 'omission':
            return unit['issues']
        return active_issues(unit, self.edits)

    def refresh_list(self):
        if self.working:
            return
        self.tree.delete(*self.tree.get_children())
        labels = {'text': '正文', 'heading': '标题', 'caption': '图表标题', 'cell': '表格单元格',
                  'reference': '参考文献', 'formula': '公式', 'image': '图片', 'code': '代码', 'unmapped': '未对应', 'table': '表格'}
        for unit in self.units:
            query = self.query.get().strip().casefold()
            if query and query not in (' '.join((unit['id'], unit['source'], effective_text(unit, self.edits)))).casefold():
                continue
            issues = self.issues(unit)
            if self.filter.get() and not issues:
                continue
            self.tree.insert('', 'end', iid=unit['id'], values=(unit['chapter'] + ' · ' + unit['source'][:60],
                '、'.join(map(str, unit['pages'])) if unit.get('pages') else unit.get('page') or '未定位', labels.get(unit['kind'], unit['kind']), '；'.join(issues) or ''))
        if self.current and self.tree.exists(self.current):
            self.tree.selection_set(self.current)

    def select(self, _event=None):
        selection = self.tree.selection()
        if not selection or self.working:
            return
        if self.current and self.current != selection[0] and self.target.get('1.0', 'end-1c') != self.displayed_text:
            if not messagebox.askyesno('尚未保存', '当前译文有未保存的修改。放弃修改并切换？', parent=self.window):
                self.tree.selection_set(self.current)
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
        self.displayed_text = effective_text(unit, self.edits)
        issues = self.issues(unit)
        self.reason.configure(text=f"编号 {unit['id']} · " + ('；'.join(issues) + '\n建议：' + suggestion(issues[0]) if issues else '自动检查未发现问题；仍需结合原文判断语义。'))

    def confirm(self):
        try:
            unit, revision = self.selected(), self.revision
            if unit['kind'] == 'omission':
                raise ValueError('漏段请先核对原页，不能以确认保留代替恢复。')
            self.action('saved', lambda: confirm_unit(self.out, unit['id'], expected_revision=revision))
        except Exception as exc:
            messagebox.showerror('不能确认', str(exc), parent=self.window)

    def layout_options(self):
        from export_layout import MODES
        return {'header_mode': next(k for k, v in MODES.items() if v == self.layout_mode.get())}

    def preview(self):
        from export_layout import preview_layout
        options = self.layout_options()
        def work():
            with translation_lock(self.out):
                return preview_layout(self.out, self.document, options)
        self.action('preview', work)

    def apply_layout(self):
        options = self.layout_options()
        self.action('export', lambda: reexport(self.out, export_options=options))

    def open_report(self):
        path = self.out / 'quality_report.html'
        if path.exists():
            os.startfile(path)
        else:
            self.status.configure(text='请先重新导出以生成可定位报告。')

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
            checked = omission_unit(self.document, unit['id']) if unit['kind'] == 'omission' else unit
            issues = validate_edit(checked, text) + matcher.issues(unit['source'], text)
            if issues and not messagebox.askyesno('仍有待核对项', '\n'.join(issues) + '\n\n仍然保留此修订？', parent=self.window):
                return
            revision = self.revision
            if unit['kind'] == 'omission':
                self.action('saved', lambda: adopt_omission(self.out, unit['id'], text, expected_revision=revision))
            else:
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
                    return (propose_omission if unit['kind'] == 'omission' else propose_translation)(self.out, unit['id'], config)
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
            elif kind == 'preview':
                os.startfile(value)
                self.status.configure(text='样式预览已打开；应用样式并导出后更新成品。')
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
        if self.current and self.target.get('1.0', 'end-1c') != self.displayed_text:
            if not messagebox.askyesno('尚未保存', '当前译文有未保存的修改。放弃修改并关闭？', parent=self.window):
                return
        self.window.after_cancel(self.timer)
        self.window.destroy()
