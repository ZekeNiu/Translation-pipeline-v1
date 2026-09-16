"""Bilingual text review with explicit candidate adoption and export-only retry."""
from task_paths import artifact, translation_lock, public_markdown
from pathlib import Path
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk, simpledialog

from glossary import GlossaryMatcher, GlossarySnapshot
from review import (all_units, effective_text, load_edits, save_edit, reexport, propose_translation,
                    extract_original_page, validate_edit, recover_existing, current_glossary)
from review import confirm_unit, propose_omission, adopt_omission, omission_unit
from quality_report import active_issues, suggestion
from task_state import read_json, fingerprint, redact
from task_state import write_json
from review_index import ReviewIndex
from review_state import revision_state
from review import set_disposition, READONLY


class ReviewWindow:
    def __init__(self, parent, output, config_for, readonly=lambda: False, busy=lambda _: None):
        self.out, self.config_for, self.readonly, self.busy_callback = Path(output), config_for, readonly, busy
        self.window = tk.Toplevel(parent)
        self.window.title('原文 — 译文复核')
        self.window.geometry(f'1250x{min(900, self.window.winfo_screenheight() - 100)}')
        self.window.minsize(960, 680)
        self.events, self.working = queue.Queue(), False
        self.document, self.edits, self.units = None, None, []
        self.current = None
        self.displayed_text = ''
        self.controls = []
        self.action_buttons = {}
        self.index = None
        self.page_offset = 0
        self.search_timer = None
        self.preferences = read_json(artifact(self.out, 'review_view.json'), {})
        top = ttk.Frame(self.window, padding=10)
        top.pack(fill='x')
        self.filter = tk.BooleanVar(value=self.preferences.get('issues_only', True))
        ttk.Checkbutton(top, text='重点复核（关闭后按章节阅读）', variable=self.filter, command=self.refresh_list).pack(side='left')
        ttk.Button(top, text='刷新', command=self.load).pack(side='left', padx=6)
        ttk.Button(top, text='上一项', command=lambda: self.navigate(-1)).pack(side='left')
        ttk.Button(top, text='下一项', command=lambda: self.navigate(1)).pack(side='left')
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
        self.query = tk.StringVar(value=self.preferences.get('query', ''))
        search = ttk.Entry(settings, textvariable=self.query, width=24)
        search.pack(side='right')
        ttk.Label(settings, text='搜索编号 / 原译文 ').pack(side='right')
        self.query.trace_add('write', lambda *_: self.schedule_search())
        filters = ttk.Frame(self.window, padding=(10, 6))
        filters.pack(fill='x')
        self.chapter, self.category, self.decision = tk.StringVar(), tk.StringVar(), tk.StringVar(value='待处理')
        self.chapter_box = ttk.Combobox(filters, textvariable=self.chapter, state='readonly', width=34)
        self.chapter_box.pack(side='left', padx=3)
        self.category_box = ttk.Combobox(filters, textvariable=self.category, state='readonly', width=16,
            values=['全部问题', '完整性与结构', '数值与单位', '术语', '翻译疑点', '一般信息'])
        self.category_box.pack(side='left', padx=3)
        self.category.set('全部问题')
        decision_box = ttk.Combobox(filters, textvariable=self.decision, state='readonly', width=12,
            values=['待处理', '稍后处理', '已确认', '无需恢复', '全部状态'])
        decision_box.pack(side='left', padx=3)
        for box in (self.chapter_box, self.category_box, decision_box):
            box.bind('<<ComboboxSelected>>', lambda _: self.refresh_list())
        ttk.Button(filters, text='前一组', command=lambda: self.turn_page(-1)).pack(side='right')
        ttk.Button(filters, text='后一组', command=lambda: self.turn_page(1)).pack(side='right')
        listing = ttk.Frame(self.window)
        listing.pack(fill='x', padx=10)
        self.tree = ttk.Treeview(listing, columns=('section', 'page', 'kind', 'issue'), show='headings', height=6, selectmode='extended')
        for key, label, width in (('section', '章节 / 内容', 330), ('page', '原页码', 90), ('kind', '类型', 90), ('issue', '检查项', 540)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        scrollbar = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='x', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.notebook = ttk.Notebook(self.window)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=6)
        panels = ttk.Panedwindow(self.notebook, orient='horizontal')
        self.notebook.add(panels, text='原译对照与编辑')
        self.editor_tab = panels
        self.reader = scrolledtext.ScrolledText(self.notebook, wrap='word', font=('Microsoft YaHei UI', 11))
        self.notebook.add(self.reader, text='连续阅读（点击内容进入编辑）')
        self.table_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.table_frame, text='完整表格（点击单元格编辑）')
        self.table_canvas = tk.Canvas(self.table_frame, background='white', highlightthickness=0)
        vertical = ttk.Scrollbar(self.table_frame, orient='vertical', command=self.table_canvas.yview)
        horizontal = ttk.Scrollbar(self.table_frame, orient='horizontal', command=self.table_canvas.xview)
        vertical.pack(side='right', fill='y')
        horizontal.pack(side='bottom', fill='x')
        self.table_canvas.pack(fill='both', expand=True)
        self.table_canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table_grid = tk.Frame(self.table_canvas, background='white')
        self.table_canvas.create_window((0, 0), window=self.table_grid, anchor='nw')
        self.table_grid.bind('<Configure>', lambda _: self.table_canvas.configure(scrollregion=self.table_canvas.bbox('all')))
        left, right = ttk.LabelFrame(panels, text='原文'), ttk.LabelFrame(panels, text='当前译文（保存不会立即导出整书）')
        panels.add(left, weight=1)
        panels.add(right, weight=1)
        self.source = scrolledtext.ScrolledText(left, wrap='word', font=('Microsoft YaHei UI', 11), height=10, width=40, state='disabled')
        self.target = scrolledtext.ScrolledText(right, wrap='word', font=('Microsoft YaHei UI', 11), height=10, width=40)
        self.source.pack(fill='both', expand=True)
        self.target.pack(fill='both', expand=True)
        self.candidate_frame = ttk.LabelFrame(right, text='AI 候选（尚未采用）')
        self.candidate = scrolledtext.ScrolledText(self.candidate_frame, wrap='word', height=4, width=35)
        self.candidate.pack(fill='x')
        self.adopt_button = ttk.Button(self.candidate_frame, text='采用候选到编辑框', command=self.adopt_candidate)
        self.adopt_button.pack(anchor='e')
        self.controls.append(self.adopt_button)
        self.reason = ttk.Label(self.window, text='', wraplength=1100)
        self.reason.pack(fill='x', padx=10)
        bar = ttk.Frame(self.window, padding=10)
        bar.pack(fill='x')
        for index, (label, callback) in enumerate([('保存修订', self.save), ('撤销上次译文修改', self.restore), ('AI 改译 / 补译（产生请求）', self.retranslate),
                                ('更新成品', lambda: self.action('export', lambda: reexport(self.out))),
                                ('稍后处理', lambda: self.decide('deferred')), ('确认保留所选', self.confirm), ('无需恢复 / 重新打开', self.dismiss_or_reopen)]):
            button = ttk.Button(bar, text=label, command=callback)
            button.grid(row=index // 4, column=index % 4, padx=4, pady=3, sticky='ew')
            self.controls.append(button)
            self.action_buttons[index] = button
        ttk.Button(bar, text='查看原 PDF 页', command=self.open_page).grid(row=1, column=3, padx=4)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.load()
        self.timer = self.window.after(100, self.poll)

    def load(self):
        if self.working:
            return
        path = artifact(self.out, 'review_document.json')
        if path.is_file() and path.stat().st_size > 1_000_000:
            self.working = True
            self.status.configure(text='正在加载章节与复核索引…')
            previous = self.index
            def read():
                try:
                    document = read_json(path, {})
                    edits = load_edits(self.out, document['identity'])
                    index = ReviewIndex(document, edits, previous)
                    self.events.put(('loaded', (document, edits, index)))
                except Exception as exc:
                    self.events.put(('error', str(exc)))
            threading.Thread(target=read, daemon=True).start()
            return
        document = read_json(path, {})
        if not document or document.get('version') != 1:
            self.status.configure(text='旧任务缺少对应记录；可点击“离线恢复对应记录”。')
            ttk.Button(self.window, text='离线恢复对应记录', command=self.recover).pack()
            return
        edits = load_edits(self.out, document['identity'])
        self.loaded(document, edits, ReviewIndex(document, edits, self.index))

    def loaded(self, document, edits, index):
        self.document, self.edits, self.index = document, edits, index
        self.revision = fingerprint(self.document, self.edits)
        self.units = list(self.index.units.values())
        chapters = list(dict.fromkeys(row['chapter'] for row in self.index.rows.values()))
        self.chapter_box.configure(values=['全部章节', *chapters])
        if self.chapter.get() not in chapters:
            self.chapter.set(self.preferences.get('chapter') if self.preferences.get('chapter') in chapters else '全部章节')
        if not self.current:
            self.current = self.preferences.get('current')
        self.refresh_list()
        if self.current in self.index.units:
            self.show_unit(self.index.units[self.current])
        self.show_status()

    def show_status(self):
        state = revision_state(self.out)
        pending = state['revision'] != state['exported_revision'] or state.get('export_error')
        self.status.configure(text=f"待核对 {self.index.unresolved()} 项 · " + ('修订已保存，成品待更新' if pending else '成品已更新'))

    def schedule_search(self):
        if self.search_timer:
            self.window.after_cancel(self.search_timer)
        self.search_timer = self.window.after(200, self.refresh_list)

    def issues(self, unit):
        if unit['kind'] == 'omission':
            return unit['issues']
        return self.index.rows[unit['id']]['issues']

    def refresh_list(self, reset=True):
        self.search_timer = None
        if self.working or not self.index:
            return
        if reset:
            self.page_offset = 0
        self.tree.delete(*self.tree.get_children())
        labels = {'text': '正文', 'heading': '标题', 'caption': '图表标题', 'cell': '表格单元格',
                  'reference': '参考文献', 'formula': '公式', 'image': '图片', 'code': '代码', 'unmapped': '未对应', 'table': '表格', 'omission': '疑似漏段'}
        self.visible = self.index.select(query=self.query.get(), issues_only=self.filter.get(),
            chapter='' if self.chapter.get() == '全部章节' else self.chapter.get(),
            category='' if self.category.get() == '全部问题' else self.category.get(),
            status={'待处理': 'open', '稍后处理': 'deferred', '已确认': 'confirmed', '无需恢复': 'dismissed', '全部状态': 'all'}[self.decision.get()])
        for key in self.visible[self.page_offset:self.page_offset + 100]:
            unit = self.index.units[key]
            issues = self.issues(unit)
            if unit.get('cells'):
                count = sum(bool(self.issues(c)) for c in unit['cells'])
                issues = [f'整表 · {count} 个单元格有疑点'] if count else []
            self.tree.insert('', 'end', iid=unit['id'], values=(unit['chapter'] + ' · ' + unit['source'][:60],
                '、'.join(map(str, unit['pages'])) if unit.get('pages') else unit.get('page') or '未定位', labels.get(unit['kind'], unit['kind']), '；'.join(issues) or ''))
        if self.current and self.tree.exists(self.current):
            self.tree.selection_set(self.current)
        self.fill_reader()

    def turn_page(self, direction):
        self.page_offset = max(0, min(max(0, (len(self.visible) - 1) // 100 * 100), self.page_offset + direction * 100))
        self.refresh_list(reset=False)

    def navigate(self, direction):
        if not self.visible:
            return
        parent = self.index.parents.get(self.current, self.current)
        position = self.visible.index(parent) if parent in self.visible else (-1 if direction > 0 else len(self.visible))
        key = self.visible[max(0, min(len(self.visible) - 1, position + direction))]
        self.page_offset = self.visible.index(key) // 100 * 100
        self.refresh_list(reset=False)
        self.tree.selection_set(key)
        self.tree.see(key)
        self.select()

    def fill_reader(self):
        import re
        from PIL import Image, ImageTk
        self.reader_images = []
        self.reader.configure(state='normal')
        self.reader.delete('1.0', 'end')
        for key in self.visible[self.page_offset:self.page_offset + 100]:
            unit = self.index.units[key]
            tag = 'u_' + key
            self.reader.insert('end', f"{unit.get('chapter', '')} · 原页 {unit.get('page') or '未定位'}\n", tag)
            if unit['kind'] == 'image':
                match = re.search(r'!\[[^\]]*\]\(([^)]+)\)', unit['source'])
                shown = False
                if match:
                    for path in (self.out / match[1], self.out / '_internal' / match[1]):
                        if path.resolve().is_relative_to(self.out.resolve()) and path.is_file():
                            try:
                                with Image.open(path) as source:
                                    source.thumbnail((600, 300))
                                    photo = ImageTk.PhotoImage(source.copy(), master=self.reader)
                                self.reader_images.append(photo)
                                self.reader.image_create('end', image=photo)
                                self.reader.insert('end', '\n\n')
                                shown = True
                            except OSError:
                                pass
                            break
                if not shown:
                    self.reader.insert('end', '[图片资源不可用]\n\n', tag)
            elif unit.get('cells'):
                self.reader.insert('end', f"[表格：{len(unit['cells'])} 个单元格，点击查看整表]\n\n", tag)
            else:
                self.reader.insert('end', unit['source'] + '\n' + effective_text(unit, self.edits) + '\n\n', tag)
            self.reader.tag_bind(tag, '<Button-1>', lambda _, k=key: self.open_unit(k))
        self.reader.configure(state='disabled')

    def open_unit(self, key):
        if self.working:
            return
        if self.current and self.current != key and self.target.get('1.0', 'end-1c') != self.displayed_text:
            if not messagebox.askyesno('尚未保存', '当前修改尚未保存。放弃修改并切换？', parent=self.window):
                return
        self.current = key
        self.show_unit(self.index.units[key])

    def select(self, _event=None):
        selection = self.tree.selection()
        if not selection or self.working:
            return
        if self.current and self.current != selection[0] and self.target.get('1.0', 'end-1c') != self.displayed_text:
            if not messagebox.askyesno('尚未保存', '当前译文有未保存的修改。放弃修改并切换？', parent=self.window):
                self.tree.selection_set(self.current)
                return
        self.current = selection[0]
        self.show_unit(self.index.units[self.current])

    def show_unit(self, unit):
        self.candidate_frame.pack_forget()
        self.source.configure(state='normal')
        self.source.delete('1.0', 'end')
        self.source.insert('1.0', unit['source'])
        self.source.configure(state='disabled')
        self.target.configure(state='normal')
        self.target.delete('1.0', 'end')
        self.target.insert('1.0', effective_text(unit, self.edits))
        self.displayed_text = effective_text(unit, self.edits)
        issues = self.issues(unit)
        self.reason.configure(text=f"{unit.get('chapter', '')} · 原页 {unit.get('page') or '未定位'} · " + (self.index.rows[unit['id']]['detail'] + '\n建议：' + suggestion(issues[0]) if issues else '自动检查未发现问题；不代表语义已人工审校。'))
        if unit.get('cells'):
            self.show_table(unit)
        else:
            self.notebook.select(self.editor_tab)

    def show_table(self, unit, offset=0):
        for widget in self.table_grid.winfo_children():
            widget.destroy()
        cells = unit['cells']
        from table_utils import parse_html_tables, iter_cells
        original_cells = list(iter_cells(parse_html_tables(unit['source'])))
        max_row = max((c.get('row', 1) for c in cells), default=1)
        bar = tk.Frame(self.table_grid)
        bar.grid(row=0, column=0, columnspan=100, sticky='w')
        ttk.Label(bar, text=f"每格上方原文、下方现译；黄色为疑点。显示行 {offset + 1}—{min(offset + 40, max_row)} / {max_row}").pack(side='left')
        if offset:
            ttk.Button(bar, text='前40行', command=lambda: self.show_table(unit, max(0, offset - 40))).pack(side='left')
        if offset + 40 < max_row:
            ttk.Button(bar, text='后40行', command=lambda: self.show_table(unit, offset + 40)).pack(side='left')
        for index, cell in enumerate(cells):
            row, column = cell.get('row', 1), cell.get('column', 1)
            attrs = original_cells[index].attrs if index < len(original_cells) else {}
            rowspan, colspan = max(1, int(attrs.get('rowspan', 1))), max(1, int(attrs.get('colspan', 1)))
            if row > offset + 40 or row + rowspan - 1 <= offset:
                continue
            issues = self.issues(cell)
            label = tk.Label(self.table_grid, text=f"[{row},{column}] {cell['source']}\n{effective_text(cell, self.edits)}",
                background='#fff0c2' if issues else '#f4f7fb', justify='left', anchor='nw', wraplength=185,
                width=24, relief='solid', borderwidth=1, padx=6, pady=6, cursor='hand2')
            visible_start, visible_end = max(row, offset + 1), min(row + rowspan, offset + 41)
            label.grid(row=visible_start - offset, column=column - 1, rowspan=visible_end - visible_start, columnspan=colspan, sticky='nsew')
            label.bind('<Button-1>', lambda _, k=cell['id']: self.open_unit(k))
        self.notebook.select(self.table_frame)

    def adopt_candidate(self):
        self.target.configure(state='normal')
        self.target.delete('1.0', 'end')
        self.target.insert('1.0', self.candidate.get('1.0', 'end-1c'))
        self.status.configure(text='候选已放入编辑框；点击“保存修订”才会保存。')

    def confirm(self):
        self.decide('confirmed')

    def decide(self, status, note=''):
        try:
            selection = list(self.tree.selection())
            keys = selection if len(selection) > 1 else [self.selected()['id']]
            if any(self.index.units[k].get('cells') for k in keys):
                raise ValueError('请打开整表后选择具体单元格；不会整表忽略疑点。')
            if len(keys) > 1 and not messagebox.askyesno('确认所选范围', f'将处理明确选中的 {len(keys)} 项。继续？', parent=self.window):
                return
            revision = self.revision
            self.action('saved', lambda: set_disposition(self.out, keys, status, note=note, expected_revision=revision))
        except Exception as exc:
            messagebox.showerror('未处理', str(exc), parent=self.window)

    def dismiss_or_reopen(self):
        if not self.current:
            self.status.configure(text='请先选择需要处理的内容。')
            return
        unit = self.selected()
        if unit['kind'] == 'omission' and self.index.rows[unit['id']]['status'] != 'dismissed':
            note = simpledialog.askstring('无需恢复的依据', '核对原页后，请说明为什么无需恢复：', parent=self.window)
            if note:
                self.decide('dismissed', note)
        else:
            self.decide('open')

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
                self.action('saved', lambda: adopt_omission(self.out, unit['id'], text, expected_revision=revision, export=False))
            else:
                self.action('saved', lambda: save_edit(self.out, unit['id'], text, allow_warnings=True, expected_revision=revision, export=False))
        except Exception as exc:
            messagebox.showerror('未保存', str(exc), parent=self.window)

    def restore(self):
        try:
            unit, revision = self.selected(), self.revision
            self.action('saved', lambda: save_edit(self.out, unit['id'], '', restore=True, allow_warnings=True, expected_revision=revision, export=False))
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
            elif kind == 'loaded':
                self.loaded(*value)
            elif kind == 'candidate':
                self.candidate.delete('1.0', 'end')
                self.candidate.insert('1.0', value)
                self.candidate_frame.pack(fill='x', side='bottom')
                self.status.configure(text='AI 候选已生成，尚未采用；现译未改变。')
            elif kind == 'preview':
                os.startfile(value)
                self.status.configure(text='样式预览已打开；应用样式并导出后更新成品。')
            else:
                self.load()
                if kind == 'recovered':
                    self.status.configure(text='离线恢复结束；未调用模型。')
        locked = self.working or self.readonly()
        for button in self.controls:
            button.configure(state='disabled' if locked else 'normal')
        protected = self.current and self.index and self.index.units.get(self.current, {}).get('kind') in READONLY | {'table'}
        self.target.configure(state='disabled' if locked or protected else 'normal')
        if not locked:
            kind = self.index.units.get(self.current, {}).get('kind') if self.index else None
            for key in (0, 1, 2, 4, 5, 6):
                disabled = not kind or (key in (0, 1, 2) and bool(protected)) or (kind == 'omission' and key in (1, 5))
                self.action_buttons[key].configure(state='disabled' if disabled else 'normal')
        self.timer = self.window.after(100, self.poll)

    def close(self):
        if self.working:
            self.status.configure(text='请等待当前操作结束后关闭。')
            return
        if self.current and self.target.get('1.0', 'end-1c') != self.displayed_text:
            if not messagebox.askyesno('尚未保存', '当前译文有未保存的修改。放弃修改并关闭？', parent=self.window):
                return
        self.window.after_cancel(self.timer)
        if self.search_timer:
            self.window.after_cancel(self.search_timer)
        write_json(artifact(self.out, 'review_view.json'), {'version': 1, 'current': self.current,
            'query': self.query.get(), 'chapter': self.chapter.get(), 'issues_only': self.filter.get()})
        self.window.destroy()
