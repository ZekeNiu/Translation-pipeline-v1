"""Official MinerU v4 local-file upload, durable polling and selective resume."""
from __future__ import annotations
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid
import requests

from http_client import RemoteError, request
from mineru_merge import find_markdown, merge_parts, result_fingerprint, result_valid, safe_extract
from pdf_parts import PDF_PLAN_VERSION, prepare_parts
from task_state import check_cancel, file_hash, fingerprint, interruptible_wait, read_json, redact, task_lock, write_json


def official_base(url):
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("MinerU URL 应为 HTTP(S) 服务地址，不能包含 Key。")
    path = parsed.path.rstrip("/")
    if "/api/v4" in path:
        path = path.split("/api/v4", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _api(session, method, url, key, cancel_event=None, **kwargs):
    response = request(session, method, url, headers={"Authorization": f"Bearer {key}"}, cancel_event=cancel_event, **kwargs)
    try:
        data = response.json()
    except ValueError as exc:
        raise RemoteError("MinerU 返回了无效的 JSON 结果。") from exc
    finally:
        response.close()
    if not isinstance(data, dict) or data.get("code") != 0:
        message = data.get("msg", "未知接口错误") if isinstance(data, dict) else "响应类型错误"
        raise RemoteError("MinerU: " + redact(str(message), [key])[:240])
    if not isinstance(data.get("data"), dict):
        raise RemoteError("MinerU 响应缺少任务数据。")
    return data["data"]


def _download(session, url, directory, cancel_event, timeout):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RemoteError("MinerU 返回了无效的下载地址。")
    target = directory / ("result_" + uuid.uuid4().hex)
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "download.zip"
    response = request(session, "GET", url, stream=True, timeout=timeout, cancel_event=cancel_event)
    try:
        with archive.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                check_cancel(cancel_event)
                fh.write(chunk)
        safe_extract(archive, target / "files")
        find_markdown(target / "files")
    finally:
        response.close()
        archive.unlink(missing_ok=True)
    return target / "files"


def parse_official(input_path, base_url, api_key, output_root, *, timeout=180, poll_interval=3,
                   max_wait=1800, progress=None, cancel_event=None, model_version="vlm"):
    if not api_key:
        raise ValueError("请填写 MinerU 官网 Key。")
    source = Path(input_path).resolve()
    if not source.is_file():
        raise ValueError("请选择存在的文档文件。")
    base = official_base(base_url)
    source_hash = file_hash(source)
    identity = fingerprint(source_hash, source.suffix.lower(), base, model_version, PDF_PLAN_VERSION)
    stem = re.sub(r"[^\w.-]", "_", source.stem)[:60]
    directory = Path(output_root) / f"{stem}_{identity[:20]}"
    with task_lock(directory), requests.Session() as session:
        manifest = directory / "parse_state.json"
        state = read_json(manifest, {})
        if not isinstance(state, dict) or state.get("identity") != identity:
            state = {"version": 1, "identity": identity, "source_hash": source_hash, "parts": []}
        old_parts = {p["id"]: p for p in state["parts"]}
        prepared = bool(old_parts) and all(Path(p["path"]).is_file() and file_hash(Path(p["path"])) == p["sha256"] for p in old_parts.values())
        if not prepared:
            if progress:
                progress({"stage": "preflight", "msg": "检查 PDF 页数、体积与章节边界…"})
            parts, warnings, count = prepare_parts(source, directory / "inputs", cancel_event=cancel_event)
            state.update(parts=[p.record() for p in parts], warnings=warnings, page_count=count)
            for part in state["parts"]:
                old = old_parts.get(part["id"], {})
                if old.get("sha256") == part["sha256"]:
                    part.update({k: v for k, v in old.items() if k not in part})
        parts = state["parts"]
        for part in parts:
            if result_valid(part):
                part["status"] = "done"
            elif part.get("status") == "failed":
                part["status"] = "new"
                part.pop("batch_id", None)
            else:
                part["status"] = "pending" if part.get("batch_id") else "new"
        state.update(status="running", source_name=source.name)
        write_json(manifest, state)
        deadline = time.monotonic() + max_wait
        refreshed_uploads, refreshed_downloads = set(), set()
        last_progress = None
        while any(p["status"] not in {"done", "failed"} for p in parts):
            check_cancel(cancel_event)
            if time.monotonic() >= deadline:
                raise RemoteError("MinerU 仍在处理，等待已超时；任务编号已保存，继续任务会先查询已有结果。", transient=True)
            pending = [p for p in parts if p["status"] == "new"]
            for offset in range(0, len(pending), 50):
                group = pending[offset:offset + 50]
                payload = {"files": [{"name": p["id"] + source.suffix.lower(), "data_id": p["id"]} for p in group],
                           "model_version": model_version, "enable_formula": True, "enable_table": True}
                data = _api(session, "POST", base + "/api/v4/file-urls/batch", api_key, cancel_event, json=payload, timeout=timeout)
                urls = data.get("file_urls", [])
                if not data.get("batch_id") or len(urls) != len(group) or not all(isinstance(u, str) for u in urls):
                    raise RemoteError("MinerU 上传地址与分片数量不匹配。")
                for part, url in zip(group, urls):
                    part.update(batch_id=data["batch_id"], upload_url=url, allocated_at=time.time(), status="waiting-file")
                # Persist before PUT; a restart queries the batch before uploading again.
                write_json(manifest, state)
            batches = sorted({p["batch_id"] for p in parts if p["status"] not in {"done", "failed"}})
            for batch in batches:
                data = _api(session, "GET", base + f"/api/v4/extract-results/batch/{batch}", api_key, cancel_event, timeout=timeout)
                items = data.get("extract_result", [])
                if not isinstance(items, list):
                    raise RemoteError("MinerU 任务查询响应格式错误。")
                by_id = {item.get("data_id") or Path(item.get("file_name", "")).stem: item for item in items if isinstance(item, dict)}
                for part in (p for p in parts if p.get("batch_id") == batch and p["status"] not in {"done", "failed"}):
                    check_cancel(cancel_event)
                    item = by_id.get(part["id"])
                    if item is None:
                        continue
                    status = item.get("state")
                    if status == "done":
                        url = item.get("full_zip_url")
                        if not url:
                            raise RemoteError("MinerU 完成任务缺少下载地址。")
                        try:
                            result = _download(session, url, directory / "raw" / part["id"], cancel_event, timeout)
                        except RemoteError as exc:
                            if exc.status in {401, 403, 404} and part["id"] not in refreshed_downloads:
                                # Requery the completed task once for a fresh signed link.
                                refreshed_downloads.add(part["id"])
                                continue
                            raise
                        part.update(result_dir=str(result.resolve()), result_hashes=result_fingerprint(result), status="done")
                        part.pop("upload_url", None)
                    elif status == "failed":
                        part.update(status="failed", error=redact(item.get("err_msg", "解析失败"), [api_key])[:240])
                        part.pop("upload_url", None)
                    elif status == "waiting-file":
                        if time.time() - part.get("allocated_at", 0) > 23 * 3600 or not part.get("upload_url"):
                            part["status"] = "new"
                            part.pop("batch_id", None)
                        else:
                            if time.time() - part.get("uploaded_at", 0) < 60:
                                continue
                            try:
                                with Path(part["path"]).open("rb") as fh:
                                    # Signed object storage requests intentionally carry no API headers.
                                    response = request(session, "PUT", part["upload_url"], data=fh, timeout=timeout, cancel_event=cancel_event)
                                    response.close()
                                part.update(status="uploaded", uploaded_at=time.time())
                            except RemoteError as exc:
                                if exc.status not in {401, 403, 404} or part["id"] in refreshed_uploads:
                                    raise
                                # The API confirmed waiting-file, so no parsed work is discarded.
                                refreshed_uploads.add(part["id"])
                                part["status"] = "new"
                                part.pop("batch_id", None)
                                part.pop("upload_url", None)
                                part.pop("uploaded_at", None)
                    elif status in {"pending", "running", "converting"}:
                        part["status"] = status
                    else:
                        raise RemoteError(f"MinerU 返回未知任务状态：{str(status)[:40]}")
                    write_json(manifest, state)
                if progress:
                    done = sum(p["status"] == "done" for p in parts)
                    running = sum(p["status"] in {"running", "converting"} for p in parts)
                    message = f"文档解析：已完成 {done}/{len(parts)} 个部分" + (f"；{running} 个正在解析" if running else "")
                    progress({"stage": "parse", "msg": message, "completed": done, "total": len(parts), "log": message != last_progress})
                    last_progress = message
            if any(p["status"] not in {"done", "failed", "new"} for p in parts):
                interruptible_wait(poll_interval, cancel_event)
        failed = [p for p in parts if p["kind"] == "main" and p["status"] != "done"]
        if failed:
            state["status"] = "partial_failed"
            write_json(manifest, state)
            details = "; ".join(f"{p['id']}: {p.get('error', '解析未完成')}" for p in failed)
            raise RemoteError("部分解析失败，已完成部分已保存；继续任务可重试。 " + details)
        check_cancel(cancel_event)
        output = merge_parts(parts, directory / "merged", state.get("warnings", []))
        if state.get("page_count") is not None and max(p["end"] for p in parts if p["kind"] == "main") != state["page_count"]:
            raise ValueError("原 PDF 页面未完整覆盖，停止翻译。")
        state["status"] = "completed"
        write_json(manifest, state)
        return output
