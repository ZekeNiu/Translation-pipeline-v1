from __future__ import annotations

from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urljoin, urlsplit
import zipfile
import uuid
from local_parse import parser_signature, run_observed
from task_state import check_cancel, TaskCancelled, file_hash, fingerprint, read_json, write_json, task_lock
from mineru_merge import safe_extract, result_fingerprint, result_valid


PROJECT_ROOT = Path(__file__).parent
DEFAULT_MINERU_OUTPUT_ROOT = PROJECT_ROOT / "mineru_outputs"
SUPPORTED_INPUT_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".docx",
    ".pptx",
    ".xlsx",
}


class MinerURunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class MinerUDetection:
    found: bool
    command: str = ""
    path: str = ""
    version: str = ""
    error: str = ""


def _timestamped_output_dir(input_path: Path, output_root: Path | None = None) -> Path:
    root = output_root or DEFAULT_MINERU_OUTPUT_ROOT
    safe_stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in input_path.stem)[:80] or "document"
    return root / f"{time.strftime('%Y-%m-%d_%H%M%S')}_{safe_stem}"


def _resolve_executable(executable: str | None = None) -> tuple[str | None, str]:
    if executable:
        executable = executable.strip().strip('"')
        candidate = Path(executable).expanduser()
        if candidate.is_file():
            return str(candidate), candidate.name.lower()
        found = shutil.which(executable)
        return found, Path(executable).name.lower()
    for command in ("mineru", "magic-pdf"):
        found = shutil.which(command)
        if found:
            return found, command
    # Prefix environments need not be active or installed below Anaconda/envs.
    registry = Path.home() / ".conda" / "environments.txt"
    try:
        prefixes = registry.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        prefixes = []
    for prefix in prefixes:
        if not prefix.strip():
            continue
        for relative in ("Scripts/mineru.exe", "bin/mineru", "Scripts/magic-pdf.exe", "bin/magic-pdf"):
            candidate = Path(prefix.strip()).expanduser() / relative
            if candidate.is_file():
                return str(candidate), candidate.stem.lower()
    return None, ""


def _cli_environment(executable: str) -> dict[str, str]:
    """Apply the selected prefix's saved settings without changing the GUI process."""
    env = os.environ.copy()
    prefix = Path(executable).resolve().parent.parent
    metadata = prefix / "conda-meta"
    if metadata.is_dir():
        state = read_json(metadata / "state", {})
        variables = state.get("env_vars", {}) if isinstance(state, dict) else {}
        if isinstance(variables, dict):
            env.update({k: v for k, v in variables.items() if isinstance(v, str)})
        paths = ([prefix, prefix / "Library" / "mingw-w64" / "bin", prefix / "Library" / "usr" / "bin",
                  prefix / "Library" / "bin", prefix / "Scripts", prefix / "bin"] if os.name == "nt" else [prefix / "bin"])
        env["PATH"] = os.pathsep.join([*(str(p) for p in paths), env.get("PATH", "")])
        env["CONDA_PREFIX"] = str(prefix)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def detect_mineru_cli(executable: str | None = None) -> MinerUDetection:
    path, command = _resolve_executable(executable)
    if not path:
        return MinerUDetection(False, command=command, error="MinerU CLI not found. 请在高级设置填写 mineru.exe 的完整路径；已检查 PATH 和已登记的 Conda 环境。")
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace", env=_cli_environment(path),
            timeout=60,
            check=False,
        )
        version = (proc.stdout or proc.stderr).strip()
        return MinerUDetection(proc.returncode == 0, command=command, path=path, version=version, error="" if proc.returncode == 0 else version)
    except Exception as exc:
        return MinerUDetection(False, command=command, path=path, error=str(exc))


def locate_mineru_output_folder(output_root: str | Path) -> Path:
    root = Path(output_root)
    if not root.exists():
        raise MinerURunnerError(f"MinerU output folder does not exist: {root}")
    if root.is_file():
        root = root.parent

    md_files = sorted(root.rglob("*.md"), key=lambda p: (p.name.lower() != "full.md", len(p.parts), str(p).lower()))
    if not md_files:
        raise MinerURunnerError(f"MinerU did not produce a Markdown file under: {root}")
    return md_files[0].parent


def parse_with_local_cli(
    input_path: str | Path,
    output_root: str | Path | None = None,
    executable: str | None = None,
    backend: str | None = None,
    timeout: int | None = None,
    progress=None,
    cancel_event=None,
) -> Path:
    check_cancel(cancel_event)
    source = Path(input_path)
    if not source.exists():
        raise FileNotFoundError(f"Input file does not exist: {source}")
    if source.is_file() and source.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
        raise MinerURunnerError(f"Unsupported MinerU input type: {source.suffix}")

    exe_path, command_name = _resolve_executable(executable)
    if not exe_path:
        raise MinerURunnerError("MinerU CLI was not found. Install MinerU or set the executable path.")

    root = Path(output_root) if output_root else DEFAULT_MINERU_OUTPUT_ROOT
    source_hash = file_hash(source) if source.is_file() else fingerprint([
        (p.relative_to(source).as_posix(), file_hash(p)) for p in sorted(source.rglob("*")) if p.is_file()])
    identity = fingerprint(source_hash, str(Path(exe_path).resolve()), backend or "auto")
    out_dir = root / f"local_v2_{identity[:20]}"
    with task_lock(out_dir):
        check_cancel(cancel_event)
        if progress:
            progress({"msg": f"检查 MinerU 版本与本地模型：{exe_path}", "stage": "parse"})
        env = _cli_environment(exe_path)
        signature, reusable = parser_signature(exe_path, backend, env, detect_mineru_cli)
        cache = read_json(out_dir / "local_state.json", {})
        if reusable and cache.get("signature") == signature and result_valid(cache):
            if progress:
                progress({"msg": "MinerU：版本、模型与结果校验通过，复用已完成解析。", "stage": "parse"})
            return locate_mineru_output_folder(cache["result_dir"])
        if progress:
            reason = "解析版本或配置已变化，重新解析。" if reusable else "无法完整验证本地模型版本，本次重新解析。"
            progress({"msg": "MinerU：" + reason, "stage": "parse"})
        attempt = out_dir / ("attempt_" + uuid.uuid4().hex[:12])
        result_dir = attempt / "result"
        result_dir.mkdir(parents=True)
        cmd = [exe_path, "-p", str(source.resolve()), "-o", str(result_dir.resolve())]
        if not Path(exe_path).name.lower().startswith("magic-pdf") and backend and backend != "auto":
            cmd.extend(["-b", backend])
        try:
            proc = run_observed(cmd, attempt, env, timeout, progress, cancel_event)
        except TimeoutError as exc:
            raise MinerURunnerError(str(exc)) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-6000:]
            raise MinerURunnerError(f"MinerU CLI failed with exit code {proc.returncode}: {detail}\n日志：{attempt}")
        check_cancel(cancel_event)
        folder = locate_mineru_output_folder(result_dir)
        write_json(out_dir / "local_state.json", {"version": 2, "signature": signature,
                   "source": str(source.resolve()), "source_hash": source_hash,
                   "result_dir": str(result_dir.resolve()), "result_hashes": result_fingerprint(result_dir)})
        write_json(folder / "source_document.json", {"version": 1, "path": str(source.resolve()), "hash": source_hash})
        # The source record is also included in subsequent integrity checks.
        cache = read_json(out_dir / "local_state.json")
        cache["result_hashes"] = result_fingerprint(result_dir)
        write_json(out_dir / "local_state.json", cache)
        if progress:
            progress({"msg": "MinerU：解析完成。", "stage": "parse"})
        return folder



def _auth_headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _extract_zip(content: bytes, out_dir: Path) -> None:
    safe_extract(io.BytesIO(content), out_dir)


def _json_get(data: Any, dotted_key: str) -> Any:
    cur = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first_json_value(data: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = _json_get(data, key)
        if value:
            return value
    return None


def _write_markdown_from_json(data: Any, out_dir: Path) -> bool:
    markdown = _first_json_value(
        data,
        (
            "full_md",
            "markdown",
            "md",
            "content",
            "data.full_md",
            "data.markdown",
            "data.md",
            "data.content",
            "result.full_md",
            "result.markdown",
            "result.md",
            "result.content",
        ),
    )
    if isinstance(markdown, str) and markdown.strip():
        (out_dir / "full.md").write_text(markdown, encoding="utf-8")
        return True
    return False


def _result_url_from_json(data: Any) -> str | None:
    value = _first_json_value(
        data,
        (
            "download_url",
            "full_zip_url",
            "output_url",
            "result_url",
            "url",
            "data.download_url",
            "data.full_zip_url",
            "data.output_url",
            "data.result_url",
            "data.url",
            "result.download_url",
            "result.output_url",
            "result.result_url",
            "result.url",
        ),
    )
    return value if isinstance(value, str) and value else None


def _task_id_from_json(data: Any) -> str | None:
    value = _first_json_value(data, ("task_id", "id", "data.task_id", "data.id", "result.task_id", "result.id"))
    return str(value) if value else None


def _task_status_from_json(data: Any) -> str:
    value = _first_json_value(data, ("status", "state", "data.status", "data.state", "result.status", "result.state"))
    return str(value).lower() if value else ""


def _handle_api_response(resp, out_dir: Path, session, base_url: str, api_key: str | None) -> None:
    content_type = resp.headers.get("content-type", "").lower()
    content = resp.content
    if "zip" in content_type or content.startswith(b"PK\x03\x04"):
        _extract_zip(content, out_dir)
        return
    try:
        data = resp.json()
    except ValueError:
        if "html" in content_type or resp.text.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise MinerURunnerError("MinerU API 返回了网页，需填写 API 服务地址。")
        if resp.text.strip():
            (out_dir / "full.md").write_text(resp.text, encoding="utf-8")
            return
        raise MinerURunnerError("MinerU API returned an empty non-JSON response.")

    if _write_markdown_from_json(data, out_dir):
        return
    result_url = _result_url_from_json(data)
    if result_url:
        download_url = result_url if result_url.startswith(("http://", "https://")) else urljoin(base_url.rstrip("/") + "/", result_url.lstrip("/"))
        same_origin = (urlsplit(download_url).scheme, urlsplit(download_url).netloc) == (urlsplit(base_url).scheme, urlsplit(base_url).netloc)
        download = session.get(download_url, headers=_auth_headers(api_key) if same_origin else {}, timeout=180)
        download.raise_for_status()
        _handle_api_response(download, out_dir, session, base_url, api_key)
        return
    (out_dir / "api_result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_with_api(
    input_path: str | Path,
    base_url: str,
    api_key: str | None = None,
    output_root: str | Path | None = None,
    mode: str = "auto",
    timeout: int = 180,
    poll_interval: int = 3,
    max_wait: int = 1800,
    progress=None,
    cancel_event=None,
) -> Path:
    check_cancel(cancel_event)
    source = Path(input_path)
    if not source.exists():
        raise FileNotFoundError(f"Input file does not exist: {source}")
    if not base_url.strip():
        raise MinerURunnerError("MinerU API Base URL is empty.")
    if mode == "official" or (mode == "auto" and urlsplit(base_url).hostname in {"mineru.net", "www.mineru.net"}):
        from mineru_cloud import parse_official
        return parse_official(input_path, base_url, api_key, output_root or DEFAULT_MINERU_OUTPUT_ROOT,
                              timeout=timeout, poll_interval=poll_interval, max_wait=max_wait,
                              progress=progress, cancel_event=cancel_event)

    import requests

    out_dir = _timestamped_output_dir(source, Path(output_root) if output_root else None)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    base = base_url.strip().rstrip("/")
    modes = ["file_parse", "tasks"] if mode == "auto" else [mode]
    last_error: Exception | None = None

    for selected in modes:
        try:
            with source.open("rb") as fh:
                files = {"file": (source.name, fh, "application/octet-stream")}
                if selected == "tasks":
                    endpoint = f"{base}/tasks"
                else:
                    endpoint = f"{base}/file_parse"
                if progress:
                    progress(f"☁️ MinerU API submit: {selected}")
                resp = session.post(endpoint, headers=_auth_headers(api_key), files=files, timeout=timeout)
            if mode == "auto" and resp.status_code in {404, 405}:
                last_error = MinerURunnerError(f"{selected} endpoint returned {resp.status_code}")
                continue
            resp.raise_for_status()

            if selected != "tasks":
                _handle_api_response(resp, out_dir, session, base, api_key)
                return locate_mineru_output_folder(out_dir)

            data = resp.json()
            task_id = _task_id_from_json(data)
            if not task_id:
                _handle_api_response(resp, out_dir, session, base, api_key)
                return locate_mineru_output_folder(out_dir)

            deadline = time.time() + max_wait
            while time.time() < deadline:
                check_cancel(cancel_event)
                status_resp = session.get(f"{base}/tasks/{task_id}", headers=_auth_headers(api_key), timeout=timeout)
                status_resp.raise_for_status()
                status_data = status_resp.json()
                status = _task_status_from_json(status_data)
                if progress:
                    progress(f"☁️ MinerU API task {task_id}: {status or 'polling'}")
                if status in {"success", "succeeded", "done", "completed", "finished"}:
                    _handle_api_response(status_resp, out_dir, session, base, api_key)
                    return locate_mineru_output_folder(out_dir)
                if status in {"failed", "error", "cancelled", "canceled"}:
                    raise MinerURunnerError(f"MinerU API task failed: {json.dumps(status_data, ensure_ascii=False)[:600]}")
                time.sleep(poll_interval)
            raise MinerURunnerError(f"MinerU API task timed out after {max_wait} seconds.")
        except TaskCancelled:
            raise
        except Exception as exc:
            last_error = exc
            break  # Only an explicit 404/405 above is evidence for another protocol.
    raise MinerURunnerError(f"MinerU API parse failed: {last_error}")
