from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urljoin
import zipfile
from task_state import check_cancel


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
        candidate = Path(executable).expanduser()
        if candidate.exists():
            return str(candidate), candidate.name.lower()
        found = shutil.which(executable)
        return found, Path(executable).name.lower()
    for command in ("mineru", "magic-pdf"):
        found = shutil.which(command)
        if found:
            return found, command
    return None, ""


def detect_mineru_cli(executable: str | None = None) -> MinerUDetection:
    path, command = _resolve_executable(executable)
    if not path:
        return MinerUDetection(False, command=command, error="MinerU CLI not found in PATH.")
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
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

    out_dir = _timestamped_output_dir(source, Path(output_root) if output_root else None)
    out_dir.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"🔎 MinerU local parse output: {out_dir}")

    if command_name == "magic-pdf" or Path(exe_path).name.lower().startswith("magic-pdf"):
        cmd = [exe_path, "-p", str(source), "-o", str(out_dir)]
    else:
        cmd = [exe_path, "-p", str(source), "-o", str(out_dir)]
        if backend and backend != "auto":
            cmd.extend(["-b", backend])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MinerURunnerError(f"MinerU CLI failed with exit code {proc.returncode}: {detail}")
    return locate_mineru_output_folder(out_dir)


def _auth_headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _extract_zip(content: bytes, out_dir: Path) -> None:
    zip_path = out_dir / "mineru_result.zip"
    zip_path.write_bytes(content)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    try:
        zip_path.unlink()
    except OSError:
        pass


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
            "output_url",
            "result_url",
            "url",
            "data.download_url",
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
        if resp.text.strip():
            (out_dir / "full.md").write_text(resp.text, encoding="utf-8")
            return
        raise MinerURunnerError("MinerU API returned an empty non-JSON response.")

    if _write_markdown_from_json(data, out_dir):
        return
    result_url = _result_url_from_json(data)
    if result_url:
        download_url = result_url if result_url.startswith(("http://", "https://")) else urljoin(base_url.rstrip("/") + "/", result_url.lstrip("/"))
        download = session.get(download_url, headers=_auth_headers(api_key), timeout=180)
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
        except Exception as exc:
            last_error = exc
            if mode != "auto":
                break
    raise MinerURunnerError(f"MinerU API parse failed: {last_error}")
