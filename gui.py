"""Translation Pipeline GUI with OpenAI-compatible provider selection."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

PIPELINE_DIR = Path(__file__).parent
sys.path.insert(0, str(PIPELINE_DIR))

from mineru_runner import DEFAULT_MINERU_OUTPUT_ROOT, detect_mineru_cli, parse_with_api, parse_with_local_cli
from translate import PROVIDER_PRESETS, list_available_models, resolve_provider_config


SOURCE_EXISTING_FOLDER = "已有 MinerU 文件夹"
SOURCE_LOCAL_CLI = "本地 MinerU CLI"
SOURCE_API = "MinerU API"
DOCUMENT_FILETYPES = [
    ("Supported documents", "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.docx *.pptx *.xlsx"),
    ("PDF", "*.pdf"),
    ("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
    ("Office", "*.docx *.pptx *.xlsx"),
    ("All files", "*.*"),
]


class App:
    def __init__(self, root):
        self.root = root
        root.title("翻译流水线 v2.1")
        root.geometry("900x820")
        root.minsize(760, 680)
        self.env_values = self._read_env_file()

        tk.Label(root, text="AI 厂商:").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 2))
        self.provider_var = tk.StringVar(value=os.environ.get("AI_PROVIDER", "deepseek"))
        self.provider_box = ttk.Combobox(
            root,
            textvariable=self.provider_var,
            values=list(PROVIDER_PRESETS.keys()),
            state="readonly",
            width=24,
        )
        self.provider_box.grid(row=0, column=1, sticky="w", padx=(12, 5), pady=(12, 2))
        self.provider_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_provider_defaults())

        tk.Label(root, text="Base URL:").grid(row=1, column=0, sticky="w", padx=12, pady=(4, 2))
        self.base_url_var = tk.StringVar()
        tk.Entry(root, textvariable=self.base_url_var, width=70).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(12, 5), pady=(4, 2)
        )

        tk.Label(root, text="模型:").grid(row=2, column=0, sticky="w", padx=12, pady=(4, 2))
        self.model_var = tk.StringVar()
        self.model_box = ttk.Combobox(root, textvariable=self.model_var, values=[], width=67)
        self.model_box.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=(4, 2))
        tk.Button(root, text="刷新模型", command=self._refresh_models).grid(row=2, column=2, padx=5, pady=(4, 2))

        tk.Label(root, text="API Key:").grid(row=3, column=0, sticky="w", padx=12, pady=(4, 2))
        self.api_key = tk.Entry(root, width=70, show="*")
        self.api_key.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(12, 5), pady=(4, 2))

        tk.Label(root, text="速度模式:").grid(row=4, column=0, sticky="w", padx=12, pady=(4, 2))
        self.speed_var = tk.StringVar(value="balanced")
        ttk.Combobox(
            root,
            textvariable=self.speed_var,
            values=["safe", "balanced", "fast"],
            state="readonly",
            width=24,
        ).grid(row=4, column=1, sticky="w", padx=(12, 5), pady=(4, 2))

        tk.Label(root, text="输入来源:").grid(row=5, column=0, sticky="w", padx=12, pady=(8, 2))
        self.source_mode_var = tk.StringVar(value=SOURCE_EXISTING_FOLDER)
        self.source_mode_box = ttk.Combobox(
            root,
            textvariable=self.source_mode_var,
            values=[SOURCE_EXISTING_FOLDER, SOURCE_LOCAL_CLI, SOURCE_API],
            state="readonly",
            width=24,
        )
        self.source_mode_box.grid(row=5, column=1, sticky="w", padx=(12, 5), pady=(8, 2))
        self.source_mode_box.bind("<<ComboboxSelected>>", lambda _e: self._update_source_mode())

        self.input_label = tk.Label(root, text="MinerU 文件夹:")
        self.input_label.grid(row=6, column=0, sticky="w", padx=12, pady=(4, 2))
        self.input_var = tk.StringVar()
        tk.Entry(root, textvariable=self.input_var, width=70).grid(
            row=6, column=1, sticky="ew", padx=(12, 0), pady=(4, 2)
        )
        tk.Button(root, text="浏览...", command=self._browse_input).grid(row=6, column=2, padx=5, pady=(4, 2))

        tk.Label(root, text="MinerU CLI:").grid(row=7, column=0, sticky="w", padx=12, pady=(4, 2))
        self.mineru_exe_var = tk.StringVar()
        self.mineru_exe_entry = tk.Entry(root, textvariable=self.mineru_exe_var, width=70)
        self.mineru_exe_entry.grid(row=7, column=1, sticky="ew", padx=(12, 0), pady=(4, 2))
        tk.Button(root, text="检测环境", command=self._detect_mineru).grid(row=7, column=2, padx=5, pady=(4, 2))

        tk.Label(root, text="MinerU 后端:").grid(row=8, column=0, sticky="w", padx=12, pady=(4, 2))
        self.mineru_backend_var = tk.StringVar(value="auto")
        self.mineru_backend_box = ttk.Combobox(
            root,
            textvariable=self.mineru_backend_var,
            values=["auto", "pipeline"],
            state="readonly",
            width=24,
        )
        self.mineru_backend_box.grid(row=8, column=1, sticky="w", padx=(12, 5), pady=(4, 2))

        tk.Label(root, text="MinerU 输出目录:").grid(row=9, column=0, sticky="w", padx=12, pady=(4, 2))
        self.mineru_out_var = tk.StringVar(value=str(DEFAULT_MINERU_OUTPUT_ROOT))
        self.mineru_out_entry = tk.Entry(root, textvariable=self.mineru_out_var, width=70)
        self.mineru_out_entry.grid(row=9, column=1, sticky="ew", padx=(12, 0), pady=(4, 2))
        tk.Button(root, text="选择...", command=self._browse_mineru_output).grid(row=9, column=2, padx=5, pady=(4, 2))

        tk.Label(root, text="MinerU API URL:").grid(row=10, column=0, sticky="w", padx=12, pady=(4, 2))
        self.mineru_api_url_var = tk.StringVar(value=self._env("MINERU_API_BASE_URL"))
        self.mineru_api_url_entry = tk.Entry(root, textvariable=self.mineru_api_url_var, width=70)
        self.mineru_api_url_entry.grid(row=10, column=1, columnspan=2, sticky="ew", padx=(12, 5), pady=(4, 2))

        tk.Label(root, text="MinerU API Key:").grid(row=11, column=0, sticky="w", padx=12, pady=(4, 2))
        self.mineru_api_key = tk.Entry(root, width=70, show="*")
        self.mineru_api_key.grid(row=11, column=1, columnspan=2, sticky="ew", padx=(12, 5), pady=(4, 2))
        mineru_api_key = self._env("MINERU_API_KEY")
        if mineru_api_key:
            self.mineru_api_key.insert(0, mineru_api_key)

        tk.Label(root, text="翻译输出目录(可选):").grid(row=12, column=0, sticky="w", padx=12, pady=(4, 2))
        self.out_var = tk.StringVar()
        tk.Entry(root, textvariable=self.out_var, width=70).grid(
            row=12, column=1, columnspan=2, sticky="ew", padx=(12, 5), pady=(4, 2)
        )

        self.run_btn = tk.Button(
            root,
            text="▶ 开始翻译",
            command=self._run,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 11, "bold"),
            height=2,
            width=14,
        )
        self.run_btn.grid(row=13, column=0, columnspan=3, pady=8)

        self.progress = ttk.Progressbar(root, mode="determinate", length=760)
        self.progress.grid(row=14, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 4))
        self.progress_label = tk.Label(root, text="", fg="gray")
        self.progress_label.grid(row=15, column=0, columnspan=3, sticky="w", padx=12)

        self.log = scrolledtext.ScrolledText(root, height=14, font=("Consolas", 9), wrap=tk.WORD)
        self.log.grid(row=16, column=0, columnspan=3, sticky="nsew", padx=12, pady=(4, 12))

        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(16, weight=1)

        self._apply_provider_defaults()
        self._update_source_mode()
        self._log("翻译流水线 v2.1 就绪 ✓")
        self._log("选择输入来源 → 检查 AI 配置 → 开始翻译")

    def _read_env_file(self):
        values = {}
        env = PIPELINE_DIR / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
        return values

    def _env(self, name, default=""):
        return os.environ.get(name) or self.env_values.get(name, default)

    def _apply_provider_defaults(self):
        provider = self.provider_var.get().strip() or "deepseek"
        preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])
        self.base_url_var.set(self._env("AI_BASE_URL", preset["base_url"]))

        model_env = self._env("AI_MODEL") or self._env(f"{provider.upper()}_MODEL")
        if provider == "deepseek":
            model_env = self._env("DEEPSEEK_MODEL") or model_env
        self.model_var.set(model_env or preset["model"])

        key_env = self._env("AI_API_KEY_ENV", preset["api_key_env"])
        key = self._env("AI_API_KEY") or self._env(key_env)
        self.api_key.delete(0, tk.END)
        if key:
            self.api_key.insert(0, key)

    def _log(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.root.update()

    def _update_source_mode(self):
        mode = self.source_mode_var.get()
        self.input_label.config(text="MinerU 文件夹:" if mode == SOURCE_EXISTING_FOLDER else "待解析文件:")
        cli_state = tk.NORMAL if mode == SOURCE_LOCAL_CLI else tk.DISABLED
        api_state = tk.NORMAL if mode == SOURCE_API else tk.DISABLED
        self.mineru_exe_entry.configure(state=cli_state)
        self.mineru_backend_box.configure(state="readonly" if mode == SOURCE_LOCAL_CLI else tk.DISABLED)
        self.mineru_api_url_entry.configure(state=api_state)
        self.mineru_api_key.configure(state=api_state)
        self.mineru_out_entry.configure(state=tk.NORMAL if mode != SOURCE_EXISTING_FOLDER else tk.DISABLED)

    def _browse_input(self):
        mode = self.source_mode_var.get()
        if mode == SOURCE_EXISTING_FOLDER:
            folder = filedialog.askdirectory(title="选择 MinerU 输出文件夹")
            if folder:
                self.input_var.set(folder)
            return
        file_path = filedialog.askopenfilename(title="选择要解析的文件", filetypes=DOCUMENT_FILETYPES)
        if file_path:
            self.input_var.set(file_path)

    def _browse_mineru_output(self):
        folder = filedialog.askdirectory(title="选择 MinerU 中间输出目录")
        if folder:
            self.mineru_out_var.set(folder)

    def _detect_mineru(self):
        info = detect_mineru_cli(self.mineru_exe_var.get().strip() or None)
        if info.found:
            self._log(f"✅ MinerU CLI: {info.path}")
            if info.version:
                self._log(f"   Version: {info.version}")
        else:
            self._log(f"⚠️ 未检测到 MinerU CLI: {info.error}")
            self._log('   可安装官方新版: pip install -U "mineru[all]"，或手动填写 mineru.exe 路径')

    def _prepare_mineru_folder(self, progress_cb) -> str:
        mode = self.source_mode_var.get()
        input_path = self.input_var.get().strip()
        if not input_path:
            raise ValueError("请选择 MinerU 文件夹或待解析文件")
        if mode == SOURCE_EXISTING_FOLDER:
            return input_path

        output_root = self.mineru_out_var.get().strip() or str(DEFAULT_MINERU_OUTPUT_ROOT)
        if mode == SOURCE_LOCAL_CLI:
            folder = parse_with_local_cli(
                input_path,
                output_root=output_root,
                executable=self.mineru_exe_var.get().strip() or None,
                backend=self.mineru_backend_var.get().strip() or "auto",
                progress=lambda msg: progress_cb({"msg": msg}),
            )
            return str(folder)
        if mode == SOURCE_API:
            folder = parse_with_api(
                input_path,
                base_url=self.mineru_api_url_var.get().strip(),
                api_key=self.mineru_api_key.get().strip() or None,
                output_root=output_root,
                mode="auto",
                progress=lambda msg: progress_cb({"msg": msg}),
            )
            return str(folder)
        raise ValueError(f"未知输入来源: {mode}")

    def _refresh_models(self):
        provider = self.provider_var.get().strip() or "custom"
        base_url = self.base_url_var.get().strip()
        key = self.api_key.get().strip()
        current_model = self.model_var.get().strip()
        if not base_url or not key:
            self._log("⚠️ 需要先填写 Base URL 和 API Key，才能刷新模型列表")
            return

        def task():
            try:
                cfg = resolve_provider_config(provider=provider, base_url=base_url, api_key=key, model=current_model or "model")
                models = list_available_models(cfg)
                self.root.after(0, lambda: self.model_box.configure(values=models))
                self.root.after(0, lambda: self._log(f"✅ 获取到 {len(models)} 个模型"))
            except Exception as e:
                self.root.after(0, lambda: self._log(f"⚠️ 刷新模型失败，仍可手动填写模型名: {e}"))

        threading.Thread(target=task, daemon=True).start()

    def _run(self):
        input_path = self.input_var.get().strip()
        if not input_path:
            self._log("❌ 请选择 MinerU 文件夹或待解析文件")
            return

        provider = self.provider_var.get().strip() or "custom"
        base_url = self.base_url_var.get().strip()
        model = self.model_var.get().strip()
        key = self.api_key.get().strip()
        out = self.out_var.get().strip() or None
        speed_mode = self.speed_var.get().strip() or "balanced"

        if not base_url:
            self._log("❌ 请填写 Base URL")
            return
        if not model:
            self._log("❌ 请填写模型名")
            return
        if not key:
            self._log("❌ 请填写 API Key")
            return

        self.run_btn.config(state=tk.DISABLED, text="⏳ 翻译中...")
        self.progress["value"] = 0
        self.progress_label.config(text="初始化...")
        self.log.delete(1.0, tk.END)

        def task():
            try:
                from translate import run as pipeline

                def progress_cb(info):
                    msg = info.get("msg", "")
                    seg = info.get("segment", 0)
                    total = info.get("total", 1)
                    if total:
                        pct = min(95, int(seg / total * 80) + 5)
                        self.root.after(0, lambda: self.progress.configure(value=pct))
                    self.root.after(0, lambda: self.progress_label.config(text=f"阶段: {msg[:60]}"))
                    self.root.after(0, lambda: self._log(msg))

                mineru_folder = self._prepare_mineru_folder(progress_cb)
                self.root.after(0, lambda: self._log(f"📁 使用 MinerU 文件夹: {mineru_folder}"))
                pipeline(
                    mineru_folder,
                    out,
                    model=model,
                    provider=provider,
                    base_url=base_url,
                    api_key=key,
                    speed_mode=speed_mode,
                    progress_callback=progress_cb,
                )
                self.root.after(0, lambda: self.progress.configure(value=100))
                self.root.after(0, lambda: self.progress_label.config(text="✅ 完成"))
                self.root.after(0, lambda: self._log("\n✅ 翻译完成！"))
            except Exception as e:
                self.root.after(0, lambda: self._log(f"\n❌ 错误: {e}"))
                import traceback

                self.root.after(0, lambda: self._log(traceback.format_exc()))
            finally:
                self.root.after(0, lambda: self.run_btn.config(state=tk.NORMAL, text="▶ 开始翻译"))

        threading.Thread(target=task, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
