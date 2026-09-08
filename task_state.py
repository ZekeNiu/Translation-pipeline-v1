"""Small, durable task records shared by parsing, translation and the GUI."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


class TaskCancelled(RuntimeError):
    pass


def check_cancel(event=None):
    if event is not None and event.is_set():
        raise TaskCancelled("已停止，完成的部分已保存，可以继续任务。")


def interruptible_wait(seconds, event=None):
    if event is not None:
        if event.wait(seconds):
            check_cancel(event)
    else:
        time.sleep(seconds)


def fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    with Path(path).open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def atomic_write(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as fh:
            temp_path = Path(fh.name)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_json(path: Path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return default


def redact(text, secrets=()) -> str:
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[已隐藏]", text)
    # Signed result/upload URLs are credentials too. Keep only origin/path.
    return re.sub(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", r"\1?[已隐藏]", text)


@contextmanager
def task_lock(directory: Path):
    """OS lock is released on process exit, including a crash; no stale PID logic."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a+b") as fh:
        fh.seek(0, 2)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("此任务已在另一个窗口运行。") from exc
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
