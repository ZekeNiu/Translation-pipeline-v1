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

from translate import PROVIDER_PRESETS, list_available_models, resolve_provider_config


class App:
    def __init__(self, root):
        self.root = root
        root.title("翻译流水线 v2.1")
        root.geometry("840x700")
        root.minsize(680, 560)
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

        tk.Label(root, text="MinerU 文件夹:").grid(row=5, column=0, sticky="w", padx=12, pady=(8, 2))
        self.folder_var = tk.StringVar()
        tk.Entry(root, textvariable=self.folder_var, width=70).grid(
            row=5, column=1, sticky="ew", padx=(12, 0), pady=(8, 2)
        )
        tk.Button(root, text="浏览...", command=self._browse).grid(row=5, column=2, padx=5, pady=(8, 2))

        tk.Label(root, text="输出目录(可选):").grid(row=6, column=0, sticky="w", padx=12, pady=(4, 2))
        self.out_var = tk.StringVar()
        tk.Entry(root, textvariable=self.out_var, width=70).grid(
            row=6, column=1, columnspan=2, sticky="ew", padx=(12, 5), pady=(4, 2)
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
        self.run_btn.grid(row=7, column=0, columnspan=3, pady=8)

        self.progress = ttk.Progressbar(root, mode="determinate", length=760)
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 4))
        self.progress_label = tk.Label(root, text="", fg="gray")
        self.progress_label.grid(row=9, column=0, columnspan=3, sticky="w", padx=12)

        self.log = scrolledtext.ScrolledText(root, height=17, font=("Consolas", 9), wrap=tk.WORD)
        self.log.grid(row=10, column=0, columnspan=3, sticky="nsew", padx=12, pady=(4, 12))

        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(10, weight=1)

        self._apply_provider_defaults()
        self._log("翻译流水线 v2.1 就绪 ✓")
        self._log("选择厂商 → 检查 Base URL/模型/API Key → 选文件夹 → 开始翻译")

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

    def _browse(self):
        folder = filedialog.askdirectory(title="选择 MinerU 输出文件夹")
        if folder:
            self.folder_var.set(folder)

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
        folder = self.folder_var.get().strip()
        if not folder:
            self._log("❌ 请选择 MinerU 文件夹")
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

                pipeline(
                    folder,
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
