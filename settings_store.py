"""Per-user GUI settings. Secrets are protected by the Windows user account."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

from task_state import write_json


def settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    return root / "TranslationPipeline" / "settings.json"


class SettingsError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise SettingsError("加密保存 Key 需要 Windows；其他系统请使用环境变量。")
    buffer = ctypes.create_string_buffer(value)
    source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise SettingsError("无法使用当前 Windows 账户加密或读取 Key。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else settings_path()

    def _transform(self, value, decrypt=False):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "api_key":
                    if not item:
                        out[key] = ""  # An explicit clear must override environment defaults.
                    elif decrypt:
                        if not isinstance(item, dict) or item.get("protection") != "dpapi":
                            raise SettingsError("Key 的保存格式无法识别。")
                        out[key] = _crypt(base64.b64decode(item["value"], validate=True), True).decode("utf-8")
                    else:
                        out[key] = {"protection": "dpapi", "value": base64.b64encode(_crypt(item.encode("utf-8"))).decode("ascii")}
                else:
                    out[key] = self._transform(item, decrypt)
            return out
        return value

    def load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("settings version")
            if not isinstance(data.get("providers", {}), dict) or not isinstance(data.get("mineru", {}), dict):
                raise ValueError("settings groups")
            if any(not isinstance(p, dict) for p in data.get("providers", {}).values()):
                raise ValueError("provider settings")
            return self._transform(data, True)
        except (OSError, ValueError, KeyError, TypeError, SettingsError) as exc:
            raise SettingsError("设置读取失败，原文件已保留。请检查当前账户及设置文件；本次编辑暂不自动覆盖它。") from exc

    def save(self, values):
        try:
            write_json(self.path, self._transform({**values, "version": 1}))
        except (OSError, ValueError, TypeError, SettingsError) as exc:
            raise SettingsError("设置保存失败，请检查当前用户目录是否可写；当前输入仍可用于本次任务。") from exc
