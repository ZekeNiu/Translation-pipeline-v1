"""Compact desktop workflow. Workers only communicate through a queue."""
from __future__ import annotations
import os
from pathlib import Path
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk, messagebox

from mineru_runner import DEFAULT_MINERU_OUTPUT_ROOT, detect_mineru_cli, parse_with_api, parse_with_local_cli
from settings_store import SettingsError, SettingsStore
from task_state import TaskCancelled, check_cancel, read_json, redact
from translate import PROVIDER_PRESETS, list_available_models, resolve_provider_config

SOURCE_EXISTING_FOLDER = "已有 MinerU 文件夹"
SOURCE_LOCAL_CLI = "本地 MinerU CLI"
SOURCE_API = "MinerU API"


class App:
    def __init__(self, root, settings_store=None):
        self.root, self.store = root, settings_store or SettingsStore()
        root.title("学术文档翻译")
        root.geometry("850x660")
        root.minsize(760, 600)
        self.save_blocked, load_error = False, ""
        try:
            self.settings = self.store.load()
        except SettingsError as exc:
            self.settings, self.save_blocked, load_error = {}, True, str(exc)
        self.providers = self.settings.get("providers", {})
        self.current_provider = ""
        self.events, self.cancel_event = queue.Queue(), threading.Event()
        self.running = self.closing = self.auxiliary_busy = False
        self.glossary_window = self.review_window = None
        self.save_timer = None
        self.active_secrets, self.stage = [], ""
        self.result_dir = self.settings.get("result_dir", "")
        self.vars = {}
        self.page = page = ttk.Frame(root, padding=18)
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        ttk.Label(page, text="学术文档翻译", font=("Microsoft YaHei UI", 17, "bold")).grid(row=0, sticky="w")
        ttk.Label(page, text="选择材料，确认服务；中断后可继续已保存的任务。").grid(row=1, sticky="w", pady=(4, 14))
        source = self._frame(page, "翻译材料", 2)
        self._field(source, "输入方式", "source_mode", SOURCE_EXISTING_FOLDER, 0,
                    [SOURCE_EXISTING_FOLDER, SOURCE_API, SOURCE_LOCAL_CLI]).bind("<<ComboboxSelected>>", lambda e: self._update_source_mode())
        self._field(source, "文件 / 文件夹", "input_path", "", 1)
        ttk.Button(source, text="浏览", command=self._browse_input).grid(row=1, column=2, padx=8)
        self.api_frame = self._frame(source, "MinerU 官网或自建服务", 2)
        self.api_frame.grid(columnspan=3)
        mineru = self.settings.get("mineru", {})
        self._field(self.api_frame, "MinerU URL", "mineru_url", mineru.get("url", os.environ.get("MINERU_API_BASE_URL", "https://mineru.net")), 0)
        self._field(self.api_frame, "MinerU Key", "mineru_key", mineru.get("api_key", os.environ.get("MINERU_API_KEY", "")), 1, secret=True)
        service = self._frame(page, "翻译服务", 3)
        self._field(service, "厂商", "provider", os.environ.get("AI_PROVIDER", "deepseek"), 0, list(PROVIDER_PRESETS)).bind("<<ComboboxSelected>>", lambda e: self._apply_provider_defaults())
        self.model_box = self._field(service, "模型", "model", "", 1, [], editable=True)
        self.refresh_btn = ttk.Button(service, text="刷新模型", command=self._refresh_models)
        self.refresh_btn.grid(row=1, column=2, padx=8)
        self._field(service, "服务 URL", "base_url", "", 2)
        self._field(service, "API Key", "api_key", "", 3, secret=True)
        toggles = ttk.Frame(page)
        toggles.grid(row=4, sticky="ew", pady=4)
        self.advanced_var, self.logs_var = tk.BooleanVar(), tk.BooleanVar()
        ttk.Checkbutton(toggles, text="高级设置", variable=self.advanced_var, command=self._toggle_panels).pack(side="left")
        ttk.Checkbutton(toggles, text="详细日志", variable=self.logs_var, command=self._toggle_panels).pack(side="left", padx=14)
        ttk.Button(toggles, text="专业术语表", command=self._open_glossary).pack(side="left", padx=8)
        self.save_label = ttk.Label(toggles, text="设置自动保存到当前账户")
        self.save_label.pack(side="right")
        self.advanced_frame = self._frame(page, "高级设置", 5)
        self._field(self.advanced_frame, "并发模式", "speed_mode", "balanced", 0, ["safe", "balanced", "fast"])
        self._field(self.advanced_frame, "译文目录（可选）", "output_dir", "", 1)
        self._field(self.advanced_frame, "解析文件目录", "mineru_output", mineru.get("output", str(DEFAULT_MINERU_OUTPUT_ROOT)), 2)
        self.cli_frame = self._frame(self.advanced_frame, "本地 MinerU", 3)
        self.cli_frame.grid(columnspan=3)
        self._field(self.cli_frame, "程序路径", "mineru_exe", mineru.get("executable", ""), 0)
        ttk.Button(self.cli_frame, text="检测环境", command=self._detect_mineru).grid(row=0, column=2, padx=8)
        self._field(self.cli_frame, "解析后端", "mineru_backend", mineru.get("backend", "auto"), 1, ["auto", "pipeline"])
        actions = ttk.Frame(page)
        actions.grid(row=6, sticky="ew", pady=12)
        self.run_btn = ttk.Button(actions, text="开始 / 继续任务", command=self._run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止并保留进度", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=10)
        self.open_btn = ttk.Button(actions, text="打开结果", command=self._open_result, state="normal" if self.result_dir else "disabled")
        self.open_btn.pack(side="right")
        ttk.Button(actions, text="复核译文", command=self._open_review).pack(side="right", padx=8)
        self.progress_label = ttk.Label(page, text=load_error or "就绪", wraplength=750)
        self.progress_label.grid(row=7, sticky="w")
        self.progress = ttk.Progressbar(page)
        self.progress.grid(row=8, sticky="ew", pady=8)
        self.log = scrolledtext.ScrolledText(page, height=8, font=("Consolas", 9), wrap="word", state="disabled")
        self.log.grid(row=9, sticky="nsew")
        page.rowconfigure(9, weight=1)
        self._apply_provider_defaults(save=False)
        self._update_source_mode(save=False)
        self._toggle_panels()
        if load_error:
            self.save_label.configure(text="设置读取失败")
            self._log(load_error)
        root.bind_all("<FocusOut>", self._schedule_save, add="+")
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.queue_timer = root.after(100, self._drain_queue)

    def _frame(self, parent, title, row):
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)
        return frame

    def _field(self, parent, label, name, default, row, choices=None, editable=False, secret=False):
        variable = self.vars[name] = tk.StringVar(value=self.settings.get(name, default))
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        widget = (ttk.Combobox(parent, textvariable=variable, values=choices, state="normal" if editable else "readonly")
                  if choices is not None else ttk.Entry(parent, textvariable=variable, show="*" if secret else ""))
        widget.grid(row=row, column=1, sticky="ew", pady=4)
        return widget

    def _snapshot(self):
        return {name: var.get().strip() for name, var in self.vars.items()}

    def _capture_provider(self):
        if self.current_provider:
            self.providers[self.current_provider] = {name: self.vars[name].get().strip() for name in ("base_url", "model", "api_key")}

    def _apply_provider_defaults(self, save=True):
        self._capture_provider()
        name = self.vars["provider"].get()
        preset = PROVIDER_PRESETS.get(name, PROVIDER_PRESETS["custom"])
        defaults = {"base_url": preset["base_url"], "model": os.environ.get(f"{name.upper()}_MODEL", preset["model"]),
                    "api_key": os.environ.get(preset["api_key_env"], "")}
        if name == os.environ.get("AI_PROVIDER", "deepseek"):
            for field, env in (("base_url", "AI_BASE_URL"), ("model", "AI_MODEL"), ("api_key", "AI_API_KEY")):
                if env in os.environ:
                    defaults[field] = os.environ[env]
            if "AI_API_KEY_ENV" in os.environ and "AI_API_KEY" not in os.environ:
                defaults["api_key"] = os.environ.get(os.environ["AI_API_KEY_ENV"], defaults["api_key"])
        for field, value in {**defaults, **self.providers.get(name, {})}.items():
            self.vars[field].set(value)
        self.model_box.configure(values=[])
        self.current_provider = name
        if save:
            self._save_settings()

    def _schedule_save(self, _event=None):
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_timer = self.root.after(350, self._save_settings)

    def _save_settings(self):
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_timer = None
        if self.save_blocked:
            return
        self._capture_provider()
        values = self._snapshot()
        # Never persist top-level plaintext fields from the worker snapshot.
        self.settings = {k: values[k] for k in ("provider", "source_mode", "input_path", "speed_mode", "output_dir")}
        self.settings.update(providers=self.providers, result_dir=self.result_dir,
                             mineru={"url": values["mineru_url"], "api_key": values["mineru_key"], "output": values["mineru_output"],
                                     "executable": values["mineru_exe"], "backend": values["mineru_backend"]})
        try:
            self.store.save(self.settings)
            self.save_label.configure(text="设置已保存")
        except SettingsError as exc:
            self.save_label.configure(text="设置保存失败")
            self.progress_label.configure(text=str(exc))
            self._log(str(exc))

    def _update_source_mode(self, save=True):
        mode = self.vars["source_mode"].get()
        self.api_frame.grid() if mode == SOURCE_API else self.api_frame.grid_remove()
        self.cli_frame.grid() if mode == SOURCE_LOCAL_CLI else self.cli_frame.grid_remove()
        if save:
            self._save_settings()
            self._toggle_panels()

    def _toggle_panels(self):
        self.advanced_frame.grid() if self.advanced_var.get() else self.advanced_frame.grid_remove()
        self.log.grid() if self.logs_var.get() else self.log.grid_remove()
        self.root.update_idletasks()
        self.root.geometry(f"{max(850, self.root.winfo_width())}x{max(660, self.root.winfo_reqheight())}")

    def _browse_input(self):
        value = (filedialog.askdirectory(title="选择 MinerU 输出文件夹") if self.vars["source_mode"].get() == SOURCE_EXISTING_FOLDER else
                 filedialog.askopenfilename(title="选择要翻译的文档", filetypes=[("文档", "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.docx *.pptx *.xlsx"), ("所有文件", "*.*")]))
        if value:
            self.vars["input_path"].set(value)
            self._save_settings()

    def _log(self, message):
        secrets = self.active_secrets + [self.vars["api_key"].get(), self.vars["mineru_key"].get()]
        secrets.extend(p.get("api_key", "") for p in self.providers.values())
        self.log.configure(state="normal")
        self.log.insert("end", redact(message, secrets) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_models(self):
        opts = self._snapshot()
        if not opts["base_url"] or not opts["api_key"]:
            self.progress_label.configure(text="请先填写翻译服务 URL 和 Key；模型也可以直接输入。")
            return
        self.refresh_btn.configure(state="disabled")
        def task():
            try:
                cfg = resolve_provider_config(**{k: opts[k] for k in ("provider", "base_url", "api_key")}, model=opts["model"] or "model")
                self.events.put(("models", (opts["provider"], opts["base_url"], list_available_models(cfg))))
            except Exception as exc:
                self.events.put(("model_error", redact(str(exc), [opts["api_key"]])))
        threading.Thread(target=task, daemon=True).start()

    def _detect_mineru(self):
        path = self.vars["mineru_exe"].get().strip() or None
        def task():
            info = detect_mineru_cli(path)
            self.events.put(("notice", f"MinerU: {info.path} {info.version}" if info.found else f"MinerU 检测失败：{info.path or path or ''}\n{info.error}"))
        threading.Thread(target=task, daemon=True).start()

    def _prepare_mineru_folder(self, opts, progress_cb):
        if opts["source_mode"] == SOURCE_EXISTING_FOLDER:
            return opts["input_path"]
        emit = lambda info: progress_cb(info if isinstance(info, dict) else {"msg": info, "stage": "parse"})
        common = {"output_root": opts["mineru_output"] or None, "progress": emit, "cancel_event": self.cancel_event}
        if opts["source_mode"] == SOURCE_LOCAL_CLI:
            return str(parse_with_local_cli(opts["input_path"], executable=opts["mineru_exe"] or None, backend=opts["mineru_backend"], **common))
        folder = parse_with_api(opts["input_path"], base_url=opts["mineru_url"], api_key=opts["mineru_key"] or None, **common)
        from task_state import write_json, file_hash
        origin = Path(folder) / 'source_document.json'
        if not origin.exists():
            write_json(origin, {'version': 1, 'path': str(Path(opts['input_path']).resolve()), 'hash': file_hash(Path(opts['input_path']))})
        return str(folder)

    def _run(self):
        if self.running or self.auxiliary_busy:
            return
        opts = self._snapshot()
        path = Path(opts["input_path"])
        error = ""
        if not opts["input_path"] or not path.exists():
            error = "请选择存在的文档或 MinerU 文件夹。"
        elif opts["source_mode"] == SOURCE_EXISTING_FOLDER and not path.is_dir():
            error = "已有 MinerU 文件夹模式需要选择文件夹。"
        elif opts["source_mode"] != SOURCE_EXISTING_FOLDER and not path.is_file():
            error = "请选择需要解析的文件。"
        elif any(not opts[k] for k in ("base_url", "model", "api_key")):
            error = "请填写翻译服务的 URL、模型和 Key。"
        elif opts["source_mode"] == SOURCE_API and not opts["mineru_url"]:
            error = "请填写 MinerU URL。"
        if error:
            self.progress_label.configure(text=error)
            return
        from glossary import GlossaryStore, book_identity
        from task_state import file_hash
        try:
            glossary = GlossaryStore().snapshot(book_identity(opts["input_path"]))
        except Exception as exc:
            self.progress_label.configure(text=str(exc))
            return
        source_info = {"version": 1, "path": str(path.resolve()), "hash": file_hash(path)} if path.is_file() else None
        self._save_settings()
        self.running = True
        secrets = self.active_secrets = [opts["api_key"], opts["mineru_key"]]
        self.cancel_event.clear()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0)
        self.progress_label.configure(text="正在检查任务…")
        self.stage = ""
        def task():
            try:
                from translate import run
                progress_cb = lambda info: self.events.put(("progress", info))
                started = time.monotonic()
                folder = self._prepare_mineru_folder(opts, progress_cb)
                elapsed = time.monotonic() - started
                check_cancel(self.cancel_event)
                md, _docx = run(folder, opts["output_dir"] or None, **{k: opts[k] for k in ("provider", "base_url", "api_key", "model", "speed_mode")},
                                progress_callback=progress_cb, cancel_event=self.cancel_event, parse_seconds=elapsed, glossary=glossary, source_info=source_info)
                summary = read_json(Path(md).parent / "quality_report.json", {})
                self.events.put(("done", (str(Path(md).parent), summary.get("status", "completed"))))
            except TaskCancelled as exc:
                self.events.put(("stopped", str(exc)))
            except Exception as exc:
                self.events.put(("failed", (redact(str(exc), secrets), redact(traceback.format_exc(), secrets))))
        threading.Thread(target=task, daemon=True).start()

    def _stop(self):
        self.cancel_event.set()
        self.stop_btn.configure(state="disabled")
        self.progress_label.configure(text="正在停止；当前请求结束后保存进度。")

    def _drain_queue(self):
        for _ in range(100):
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                stage = value.get("stage", self.stage)
                if stage != self.stage:
                    self.stage = stage
                    self.progress.configure(value=0)
                if value.get("total"):
                    self.progress.configure(value=100 * value.get("completed", value.get("segment", 0)) / value["total"])
                if not self.cancel_event.is_set():
                    self.progress_label.configure(text=redact(value.get("msg", ""), self.active_secrets)[:240])
                if value.get("log", True):
                    self._log(value.get("msg", ""))
                if value.get("output_dir"):
                    self.result_dir = value["output_dir"]
                    self.open_btn.configure(state="normal" if value.get("artifact_ready", True) else "disabled")
            elif event in {"done", "stopped", "failed"}:
                self.running = False
                self.run_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
                if event == "done":
                    self.result_dir, status = value
                    message = {"completed": "完成", "needs_review": "完成，但有待检查项：请查看结果目录中的质量报告。",
                               "partial_failed": "部分失败：已保留译文，继续任务可重试失败部分。"}.get(status, status)
                    self.open_btn.configure(state="normal")
                    if status != "partial_failed":
                        self.progress.configure(value=100)
                elif event == "failed":
                    message = "任务未完成：" + value[0]
                    self._log(value[1])
                else:
                    message = value
                self.progress_label.configure(text=message)
                self._log(message)
                self._save_settings()
            elif event == "models":
                provider, base, models = value
                if provider == self.vars["provider"].get() and base == self.vars["base_url"].get().strip():
                    self.model_box.configure(values=models)
                self.refresh_btn.configure(state="normal")
            elif event == "model_error":
                self.refresh_btn.configure(state="normal")
                self.progress_label.configure(text="刷新失败，仍可手动输入模型名。")
                self._log(value)
            elif event == "notice":
                self.progress_label.configure(text=value)
                self._log(value)
        if self.closing and not self.running and not self.auxiliary_busy:
            self.root.destroy()
            return
        self.queue_timer = self.root.after(100, self._drain_queue)

    def _open_glossary(self):
        from glossary import GlossaryStore, book_identity
        from glossary_gui import GlossaryWindow
        if self.running or self.auxiliary_busy:
            self.progress_label.configure(text="请等待当前任务结束后管理术语。")
            return
        if self.glossary_window and self.glossary_window.window.winfo_exists():
            self.glossary_window.window.lift()
            return
        opts = self._snapshot()
        try:
            book_id = book_identity(opts["input_path"])
            config = resolve_provider_config(**{k: opts[k] for k in ("provider", "base_url", "api_key", "model")})
            def prepare():
                folder = Path(self._prepare_mineru_folder(opts, lambda info: self.events.put(("progress", info))))
                files = sorted(folder.glob("*.md"))
                source = next((p for p in files if p.name == "full.md"), files[0])
                return source.read_text(encoding="utf-8")
            def busy(value):
                self.auxiliary_busy = value
                self.run_btn.configure(state="disabled" if value else "normal")
                if value:
                    self.cancel_event.clear()
            from dataclasses import replace
            config = replace(config, cancel_event=self.cancel_event)
            self.glossary_window = GlossaryWindow(self.root, GlossaryStore(), book_id, prepare, config, busy)
        except Exception as exc:
            self.progress_label.configure(text=str(exc))

    def _open_review(self):
        from review_gui import ReviewWindow
        if not self.result_dir or not (Path(self.result_dir) / "task_state.json").is_file():
            self.progress_label.configure(text="请先完成或继续一个翻译任务，再打开复核。")
            return
        if self.review_window and self.review_window.window.winfo_exists():
            self.review_window.window.lift()
            return
        def config_for(document):
            opts = self._snapshot()
            original = document.get("config", {})
            if not original:
                return resolve_provider_config(**{k: opts[k] for k in ("provider", "base_url", "api_key", "model")})
            provider = original['provider']
            if opts['provider'] == provider and opts['base_url'].rstrip('/') == original['base_url'].rstrip('/'):
                key = opts['api_key']
            else:
                saved = self.providers.get(provider, {})
                if saved.get('base_url', '').rstrip('/') != original['base_url'].rstrip('/'):
                    raise ValueError("请先在主窗口选择原任务的翻译服务并填写 Key，再重译选中项。")
                key = saved.get('api_key', '')
            if not key:
                raise ValueError("原任务翻译服务缺少 Key；手动编辑与重新导出不需要 Key。")
            from dataclasses import replace
            return replace(resolve_provider_config(**original, api_key=key), cancel_event=self.cancel_event)
        def busy(value):
            self.auxiliary_busy = value
            self.run_btn.configure(state='disabled' if value else 'normal')
            if value:
                self.cancel_event.clear()
        try:
            self.review_window = ReviewWindow(self.root, self.result_dir, config_for,
                                             lambda: self.running or self.auxiliary_busy, busy)
        except Exception as exc:
            self.progress_label.configure(text=str(exc))

    def _open_result(self):
        if self.result_dir and Path(self.result_dir).is_dir():
            os.startfile(self.result_dir)

    def _close(self):
        self._save_settings()
        if self.running or self.auxiliary_busy:
            self.closing = True
            self._stop()
        else:
            self.root.after_cancel(self.queue_timer)
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
