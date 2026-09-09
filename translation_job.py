"""Durable orchestration around the existing translation functions."""
from __future__ import annotations
import concurrent.futures
from dataclasses import asdict, replace
import os
from pathlib import Path
import re
import shutil
import time

from document_blocks import blocks
from mineru_sidecar import load_mineru_sidecar, strip_excluded_lines, write_sidecar_summary
from task_state import TaskCancelled, atomic_write, check_cancel, file_hash, fingerprint, read_json, redact, task_lock, write_json
from http_client import RemoteError


def _source_hash(root, markdown, sidecar):
    paths = {markdown, *(root / name for name in sidecar.source_files)}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"}:
            paths.add(path)
    return fingerprint({str(p.relative_to(root)): file_hash(p) for p in sorted(paths)})


def _excerpt(segment):
    return "\n".join(b.text for b in blocks(segment) if b.kind in {"heading", "text", "caption"})


def _copy_images(text, source, output):
    missing = []
    for link in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        if link.startswith(("http://", "https://", "data:")):
            missing.append({"path": link, "reason": "远程图片未自动下载"})
            continue
        relative = Path(link.removeprefix("./"))
        original = (source / relative).resolve()
        target = (output / relative).resolve()
        if not original.is_relative_to(source.resolve()) or not target.is_relative_to(output.resolve()) or not original.is_file():
            missing.append({"path": link, "reason": "图片不存在或路径超出文档目录"})
            continue
        if target != original:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    return missing


def run_job(engine, config, input_folder, output_dir=None, *, progress_callback=None, speed_mode=None,
            max_workers=None, cancel_event=None, parse_seconds=0):
    check_cancel(cancel_event)
    root = Path(input_folder).resolve()
    files = sorted(root.glob("*.md"))
    if not files:
        raise FileNotFoundError("输入文件夹中没有 MinerU Markdown 文件。")
    source = next((p for p in files if p.name.lower() == "full.md"), files[0])
    sidecar = load_mineru_sidecar(root)
    raw = engine.normalize_abstract_heading(engine.normalize_source_text(source.read_text(encoding="utf-8")))
    raw, excluded = strip_excluded_lines(raw, sidecar)
    if not raw.strip():
        raise ValueError("解析正文为空，无法翻译。")
    document_hash = _source_hash(root, source, sidecar)
    config_hash = fingerprint("pipeline-v5", config.provider_name, config.base_url, config.model, config.temperature,
                              engine.SYSPROMPT, engine.TABLE_SYSPROMPT)
    identity = fingerprint(document_hash, config_hash)
    title = engine.extract_title(raw)
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:40]
    out = Path(output_dir).resolve() if output_dir else engine.OUTPUT_ROOT / f"{safe_title}_{identity[:20]}"
    if out.resolve().is_relative_to(root):
        raise ValueError("译文目录不能位于解析输入目录内，请选择另一个目录。")
    def progress(message, **info):
        if progress_callback:
            progress_callback({"msg": redact(message, [config.api_key]), **info})
    with task_lock(out):
        state_path = out / "task_state.json"
        state = read_json(state_path, {})
        if not isinstance(state, dict):
            state = {}
        if state.get("identity") != identity:
            # Preserve prior explicit-directory artifacts before adopting a different source/configuration.
            if state.get("identity"):
                previous = out / "_previous" / state["identity"][:20]
                previous.mkdir(parents=True, exist_ok=True)
                for name in ("translated.md", "translated.docx", "quality_report.json", "quality_report.md", "task_state.json"):
                    if (out / name).is_file():
                        shutil.copy2(out / name, previous / name)
            state = {"version": 3, "identity": identity, "source_hash": document_hash, "config_hash": config_hash, "segments": {}}
        state.update(status="running", source=str(source), provider=config.provider_name, model=config.model)
        write_json(state_path, state)
        progress(f"输出目录：{out}", stage="prepare", output_dir=str(out))
        try:
            return _execute(engine, config, raw, root, out, source, sidecar, excluded, state, state_path,
                            progress, speed_mode, max_workers, cancel_event, parse_seconds)
        except TaskCancelled:
            state["status"] = "stopped"
            write_json(state_path, state)
            raise
        except Exception as exc:
            state.update(status="partial_failed", error=redact(exc, [config.api_key]))
            write_json(state_path, state)
            raise


def _execute(engine, config, raw, root, out, source, sidecar, excluded, state, state_path,
             progress, speed_mode, max_workers, cancel_event, parse_seconds):
    metrics = []
    config = replace(config, cancel_event=cancel_event, metrics=metrics)
    cache_dir = out / "_cache"
    cache_dir.mkdir(exist_ok=True)
    references_original, references_normalized, reference_reports, prepared = [], [], [], []
    original_blocks = blocks(raw)
    for block in original_blocks:
        text = block.text
        if block.kind == "reference":
            references_original.append(text)
            text, report = engine.normalize_references(text)
            references_normalized.append(text)
            reference_reports.append(report)
        prepared.append(text)
    if references_original:
        atomic_write(out / "references_original.md", "\n\n".join(references_original))
        atomic_write(out / "references_normalized.md", "\n\n".join(references_normalized))
    else:
        for name in ("references_original.md", "references_normalized.md"):
            (out / name).unlink(missing_ok=True)
    if sidecar.has_data:
        write_sidecar_summary(sidecar, out / "mineru_structure_summary.json")
    segments = engine.split_body_into_segments("\n\n".join(prepared))
    excerpts = [_excerpt(s) for s in segments]
    chapters, chapter = [], ""
    for text in segments:
        headings = re.findall(r"(?m)^#{1,3}\s+.+", text)
        chapters.append(headings[0] if headings else chapter)
        if headings:
            chapter = headings[-1]
    guide = engine.build_consistency_guide(raw)
    workers = max_workers or engine._env_int("AI_MAX_WORKERS", 0) or {"safe": 1, "balanced": 2, "fast": 4}.get(speed_mode or os.environ.get("AI_SPEED_MODE", "balanced"), 2)
    workers = max(1, min(4, int(workers)))
    ids = [f"s{i:05d}_{fingerprint(s)[:12]}" for i, s in enumerate(segments)]
    state["blocks"] = [{"id": b.id, "kind": b.kind, "source_hash": fingerprint(b.text)} for b in original_blocks]
    for i, seg_id in enumerate(ids):
        record = state["segments"].setdefault(seg_id, {})
        record.update(source_hash=fingerprint(segments[i]), index=i, context_hash=fingerprint(chapters[i], excerpts[i - 1][-300:] if i else "", excerpts[i + 1][:300] if i + 1 < len(ids) else ""))
    write_json(state_path, state)
    translations = list(segments)  # Failed material remains visible in the partial artifact.
    table_reports, warnings, pending = [], [], []
    for i, seg_id in enumerate(ids):
        record = state["segments"][seg_id]
        path = out / "_chunks" / f"{seg_id}.md"
        if record.get("status") in {"completed", "needs_review"} and path.is_file() and file_hash(path) == record.get("output_hash"):
            translations[i] = path.read_text(encoding="utf-8")
            warnings.extend(record.get("warnings", []))
            table_reports.extend(record.get("tables", []))
        else:
            record["status"] = "pending"
            pending.append(i)
    completed = len(ids) - len(pending)
    progress(f"翻译：{completed}/{len(ids)} 段已完成，可复用已校验结果", stage="translate", completed=completed, total=len(ids))
    # Each worker has its own memory; persisted request caches handle the next run.
    def worker(index):
        check_cancel(cancel_event)
        local_tables, local_warnings, memory = [], [], {}
        started = time.monotonic()
        pieces = []
        for block in blocks(segments[index]):
            check_cancel(cancel_event)
            if block.kind == "table":
                pieces.append(engine.translate_tables_in_text(block.text, replace(config, phase="table"), cache_dir, lambda *_a, **_kw: None,
                                                              consistency_guide=guide, translation_memory=memory, table_reports=local_tables))
            else:
                pieces.append(block.text)
        table_seconds = time.monotonic() - started
        started = time.monotonic()
        translated = engine.translate_segment("\n\n".join(pieces), excerpts[index - 1][-300:] if index else "", index + 1, len(ids),
                                               config=config, cache_dir=cache_dir, consistency_guide=guide, translation_memory=memory,
                                               quality_warnings=local_warnings, next_context=excerpts[index + 1][:300] if index + 1 < len(ids) else "",
                                               chapter_context=chapters[index])
        for report in local_tables:
            report["segment"] = index + 1
        return translated, local_tables, local_warnings, table_seconds, time.monotonic() - started

    started, table_seconds, body_seconds = time.monotonic(), 0, 0
    fatal = None
    pending_iter = iter(pending)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        def submit_next():
            index = next(pending_iter, None)
            if index is not None:
                check_cancel(cancel_event)
                futures[executor.submit(worker, index)] = index
        for _ in range(workers):
            submit_next()
        while futures:
            done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                index = futures.pop(future)
                record = state["segments"][ids[index]]
                try:
                    translated, tables, issues, ts, bs = future.result()
                    translations[index] = translated
                    table_seconds += ts
                    body_seconds += bs
                    table_reports.extend(tables)
                    warnings.extend(issues)
                    failures = any(r["failures"] for r in tables)
                    review = bool(issues) or any(r["residual_english"] or r["inline_artifacts"] for r in tables)
                    status = "failed" if failures else "needs_review" if review else "completed"
                    path = out / "_chunks" / f"{ids[index]}.md"
                    atomic_write(path, translated)
                    record.update(status=status, output_hash=file_hash(path), warnings=issues, tables=tables, seconds=ts + bs)
                    if not failures:
                        completed += 1
                except TaskCancelled:
                    record["status"] = "pending"
                    write_json(state_path, state)
                    raise
                except Exception as exc:
                    record.update(status="failed", error=redact(exc, [config.api_key]))
                    if isinstance(exc, RemoteError):
                        fatal = exc  # Do not fan out provider/auth/network failures to every paragraph.
                write_json(state_path, state)
                progress(f"翻译：{completed}/{len(ids)} 段已完成", stage="translate", completed=completed, total=len(ids))
                if not fatal:
                    submit_next()
    check_cancel(cancel_event)
    elapsed = time.monotonic() - started
    result = "\n\n".join(translations).strip() + "\n"
    missing_images = _copy_images(result, root, out)
    parse_report = read_json(root / "parse_report.json", {})
    parse_warnings = parse_report.get("warnings", [])
    failed = [i + 1 for i, key in enumerate(ids) if state["segments"][key].get("status") not in {"completed", "needs_review"}]
    artifacts = engine.find_latex_artifacts(result)
    status = "partial_failed" if failed else "needs_review" if warnings or parse_warnings or missing_images or artifacts or any(r["residual_english"] or r["inline_artifacts"] for r in table_reports) else "completed"
    progress("正在保存译文与质量报告…", stage="export")
    atomic_write(out / "translated.md", result)
    export_started = time.monotonic()
    docx, export_error, export_warnings = out / "translated.docx", None, []
    try:
        from make_docx import make_docx
        temporary_docx = out / "translated.docx.tmp"
        export_warnings = make_docx(result, temporary_docx, out) or []
        os.replace(temporary_docx, docx)
        if export_warnings and status == "completed":
            status = "needs_review"
    except Exception as exc:
        export_error = redact(exc, [config.api_key])
        if docx.exists():
            os.replace(docx, out / "previous_translated.docx")
        docx = None
        status = "partial_failed"
    report = {"status": status, "failed_segments": failed, "warnings": warnings, "tables": table_reports,
              "parse_warnings": parse_warnings, "missing_images": missing_images, "formula_artifacts": artifacts,
              "references": reference_reports, "export_error": export_error, "export_warnings": export_warnings, "error": str(fatal) if fatal else None,
              "timings": {"parse": parse_seconds, "translation_wall": elapsed, "table_workers": table_seconds,
                          "body_workers": body_seconds, "retry_requests": sum(m["seconds"] for m in metrics if m.get("retry")),
                          "export": time.monotonic() - export_started},
              "request_count": len(metrics), "requests": metrics, "workers": workers}
    write_json(out / "quality_report.json", report)
    lines = ["# 翻译质量报告", "", {"completed": "完成：自动检查未发现需要处理的问题。", "needs_review": "完成，但有待检查项。", "partial_failed": "部分失败；失败段落保留原文，可继续任务重试。"}[status],
             "", f"共 {len(ids)} 段；本次模型请求 {len(metrics)} 次。", "", "自动检查用于发现结构问题与疑点，不等同于人工语义审校。"]
    if failed:
        lines.extend(["", "未完成段落：" + ", ".join(map(str, failed))])
        for i in failed:
            record = state["segments"][ids[i - 1]]
            lines.append(f"- 第 {i} 段：{record.get('error', '表格部分单元格未完成或任务尚未执行')}")
    for issue in warnings:
        lines.append(f"- 第 {issue['segment']} 段：" + "；".join(issue["phrases"]))
    for issue in parse_warnings:
        lines.append(f"- 原文页码 {issue.get('pages')}：{issue.get('reason')}")
    for issue in missing_images:
        lines.append(f"- 图片 {issue['path']}：{issue['reason']}")
    for table in table_reports:
        if table["failures"] or table["residual_english"] or table["inline_artifacts"]:
            lines.append(f"- 第 {table['segment']} 段表格：请核对未翻译单元格、残留英文或格式疑点。")
    if artifacts:
        lines.append("- 公式存在未处理标记，请核对：" + ", ".join(artifacts[:10]))
    if export_error:
        lines.append("- Word 导出失败：" + export_error)
    for warning in export_warnings:
        lines.append("- " + warning)
    atomic_write(out / "quality_report.md", "\n".join(lines) + "\n")
    atomic_write(out / "translation.log", f"Source: {source}\nProvider: {config.provider_name}\nModel: {config.model}\n"
                 f"Header/footer/page lines removed: {excluded}\nReference normalization: {reference_reports}\n" +
                 redact(str(report), [config.api_key]))
    state.update(status=status, quality_report="quality_report.json")
    write_json(state_path, state)
    progress({"completed": "完成", "needs_review": "完成，但有待检查项", "partial_failed": "部分失败，已保留进度"}[status],
             stage="done", status=status, output_dir=str(out), completed=completed, total=len(ids))
    return out / "translated.md", docx
