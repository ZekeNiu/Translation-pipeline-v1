"""Versioned local-parser fingerprints and observable, cancellable processes."""
from __future__ import annotations

import codecs
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from task_state import check_cancel, file_hash, fingerprint, read_json

_versions = {}


def _version(executable, env, detection):
    path = Path(executable).resolve()
    metadata = list((path.parent.parent / "Lib" / "site-packages").glob("mineru-*.dist-info/METADATA"))
    stamps = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in [path, *metadata] if p.is_file()]
    key = fingerprint(stamps, env.get("PATH"), env.get("PYTHONPATH"))
    if not stamps:
        return detection(executable)
    if key not in _versions:
        info = detection(executable)
        if info.found:
            _versions[key] = info
        return info
    return _versions[key]


def parser_signature(executable, backend, env, detection):
    """Inspect metadata once per job; never read model tensor contents."""
    info = _version(executable, env, detection)
    config_path = Path(env.get("MINERU_TOOLS_CONFIG_JSON", str(Path.home() / "mineru.json")))
    config = read_json(config_path, {})
    roots = config.get("models-dir", {}) if isinstance(config, dict) else {}
    manifest = {}
    complete = bool(info.found and info.version and env.get("MINERU_MODEL_SOURCE") == "local" and roots)
    if isinstance(roots, dict):
        for name, directory in sorted(roots.items()):
            if not isinstance(directory, str) or not Path(directory).is_dir():
                complete = False
                continue
            files = []
            for path in sorted(Path(directory).rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    files.append((path.relative_to(directory).as_posix(), stat.st_size, stat.st_mtime_ns))
            if not files or any(".incomplete" in row[0] for row in files):
                complete = False
            manifest[name] = {"root": str(Path(directory).resolve()), "files": files}
    else:
        complete = False
    settings = {k: v for k, v in env.items() if k.startswith("MINERU_")}
    # Persist hashes, not potentially private configuration values.
    signature = {"version": 2, "executable": str(Path(executable).resolve()),
                 "engine": info.version, "backend": backend or "auto",
                 "config_hash": file_hash(config_path) if config_path.is_file() else None,
                 "environment_hash": fingerprint(settings), "models_hash": fingerprint(manifest)}
    return signature, complete


class ParseProgress:
    def __init__(self, callback):
        self.callback = callback
        self.started = self.last_activity = time.monotonic()
        self.last_emit = 0
        self.label, self.detail = "启动解析", ""
        self.completed = self.total = None

    def feed(self, text):
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).strip()
        if not text:
            return
        self.last_activity = time.monotonic()
        self.detail = text[-350:]
        match = re.search(r"Processed\s+(\d+)/(\d+)\s+pages", text, re.I)
        if match:
            self.completed, self.total = map(int, match.groups())
            self.label = "页面解析"
        elif re.search(r"model|loading|initialize|turbomind", text, re.I):
            self.label = "加载模型"
        elif re.search(r"OCR|Processing pages|Submitting batch", text, re.I):
            self.label = "页面解析"
        elif re.search(r"output|markdown|visualiz|export", text, re.I):
            self.label = "导出解析结果"
        self.emit()

    def emit(self, force=False):
        now = time.monotonic()
        if not self.callback or (not force and now - self.last_emit < 0.5):
            return
        self.last_emit = now
        message = f"MinerU：{self.label}，已用 {int(now - self.started)} 秒，最近活动 {int(now - self.last_activity)} 秒前"
        event = {"msg": message, "stage": "parse", "log": False,
                 "elapsed": now - self.started, "last_activity_seconds": now - self.last_activity}
        if self.total and self.completed is not None:
            event.update(completed=self.completed, total=self.total)
        if self.detail:
            event["msg"] += "；" + self.detail
        self.callback(event)


def terminate_tree(child):
    if child.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=False)
    else:
        os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
        child.wait(timeout=10)


def run_observed(cmd, directory, env, timeout=None, progress=None, cancel_event=None):
    directory = Path(directory)
    status = ParseProgress(progress)
    event = cancel_event or threading.Event()
    env = {**env, "PYTHONUNBUFFERED": "1"}
    paths = [directory / "cli_stdout.txt", directory / "cli_stderr.txt"]
    status.emit(True)
    with paths[0].open("wb") as stdout, paths[1].open("wb") as stderr:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        child = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, env=env, **options)
        try:
            with paths[0].open("rb") as out_reader, paths[1].open("rb") as err_reader:
                decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in paths]
                buffers = ["", ""]
                while True:
                    for i, reader in enumerate((out_reader, err_reader)):
                        chunk = reader.read(65536)
                        if chunk:
                            buffers[i] += decoders[i].decode(chunk)
                            lines = re.split(r"[\r\n]", buffers[i])
                            buffers[i] = lines.pop()
                            for line in lines:
                                status.feed(line)
                            if len(buffers[i]) > 4096:
                                status.feed(buffers[i])
                                buffers[i] = ""
                    check_cancel(event)
                    if timeout is not None and time.monotonic() - status.started > timeout:
                        raise TimeoutError("本地 MinerU 解析超时，已保留文件。")
                    if child.poll() is not None:
                        for i, reader in enumerate((out_reader, err_reader)):
                            tail = buffers[i] + decoders[i].decode(reader.read(), final=True)
                            for line in re.split(r"[\r\n]", tail):
                                status.feed(line)
                        break
                    status.emit()
                    event.wait(0.1)
        except BaseException:
            terminate_tree(child)
            raise
    status.emit(True)
    def tail(path):
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - 65536))
            return fh.read().decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(cmd, child.returncode, tail(paths[0]), tail(paths[1]))
