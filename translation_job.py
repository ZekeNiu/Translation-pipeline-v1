"""Durable orchestration around the existing translation functions."""
from __future__ import annotations
import concurrent.futures
from dataclasses import asdict, replace
import os
from pathlib import Path
import re
import shutil
import time
import threading

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


def _document_title(engine, raw, root, source):
    title = engine.extract_title(raw)
    if title != "translated":
        return title
    # MinerU may leave the cover as plain text instead of a level-one heading.
    manifest = read_json(root.parent / "parse_state.json", {})
    if isinstance(manifest, dict):
        original = manifest.get("source_name")
        if not original:
            parts = manifest.get("parts", [])
            if parts and isinstance(parts[0], dict):
                original = parts[0].get("path")
        if original:
            return Path(original).stem
    return source.stem if source.stem != "full" else root.name


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
            max_workers=None, cancel_event=None, parse_seconds=0, glossary=None, source_info=None):
    from glossary import GlossaryMatcher, GlossarySnapshot
    snapshot = glossary or GlossarySnapshot()
    config = replace(config, glossary=GlossaryMatcher(snapshot))
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
    title = _document_title(engine, raw, root, source)
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:40]
    out = Path(output_dir).resolve() if output_dir else engine.OUTPUT_ROOT / f"{safe_title}_{identity[:20]}"
    if not output_dir and not out.exists():
        # A better display name must not abandon an existing task's request cache.
        for candidate in sorted(engine.OUTPUT_ROOT.glob(f"*_{identity[:20]}")):
            saved = read_json(candidate / "task_state.json", {})
            if isinstance(saved, dict) and saved.get("identity") == identity:
                out = candidate
                break
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
                for name in ("translated.md", "translated.docx", "quality_report.json", "quality_report.md", "task_state.json", "review_document.json", "review_edits.json", "glossary_snapshot.json", "source_document.json"):
                    if (out / name).is_file():
                        shutil.copy2(out / name, previous / name)
            state = {"version": 3, "identity": identity, "source_hash": document_hash, "config_hash": config_hash, "segments": {}}
        if state.get("checks_version") != 2:
            # Revalidate raw cached responses when rules change; no need to retranslate them.
            state["segments"] = {}
            state["checks_version"] = 2
        state.update(status="running", source=str(source), provider=config.provider_name, model=config.model)
        write_json(out / "glossary_snapshot.json", snapshot.to_dict())
        if source_info:
            write_json(out / "source_document.json", source_info)
        elif (root / 'source_document.json').is_file():
            write_json(out / 'source_document.json', read_json(root / 'source_document.json', {}))
        write_json(state_path, state)
        progress(f"译文完成后保存到：{out}", stage="prepare", output_dir=str(out), artifact_ready=False)
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
        effective_terms = config.glossary.matches(segments[i])
        if record.get("glossary_terms", []) != effective_terms:
            record["status"] = "pending"
        record["glossary_terms"] = effective_terms
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
    write_json(state_path, state)
    progress(f"翻译：{completed}/{len(ids)} 段已完成，可复用已校验结果", stage="translate", completed=completed, total=len(ids))
    active, activity_lock = {}, threading.Lock()
    def activity(index, label, retry=False):
        display = label + ("，校验重试" if retry else "")
        with activity_lock:
            changed = active.get(index, (None,))[0] != display
            active[index] = (display, time.monotonic())
        if changed:
            progress(f"第 {index + 1}/{len(ids)} 段：{label}" + ("，正在修订检查项" if retry else ""),
                     stage="translate", completed=completed, total=len(ids), current_segment=index + 1)

    def waiting_progress():
        now = time.monotonic()
        with activity_lock:
            details = "；".join(f"第 {i + 1} 段{label}，等待 {int(now - since)} 秒" for i, (label, since) in sorted(active.items()))
        progress(f"翻译：{completed}/{len(ids)} 段完成；{details}", stage="translate", completed=completed, total=len(ids), log=False)
    # Each worker has its own memory; persisted request caches handle the next run.
    def worker(index):
        check_cancel(cancel_event)
        local_tables, local_warnings, memory, alignments = [], [], {}, []
        started = time.monotonic()
        pieces = []
        table_index, table_total = 0, sum(b.kind == "table" for b in blocks(segments[index]))
        for block in blocks(segments[index]):
            check_cancel(cancel_event)
            if block.kind == "table":
                table_index += 1
                label = f"表格 {table_index}/{table_total}"
                activity(index, label)
                table_config = replace(config, phase="table", request_progress=lambda retry, label=label: activity(index, label, retry))
                pieces.append(engine.translate_tables_in_text(block.text, table_config, cache_dir, lambda *_a, **_kw: None,
                                                              consistency_guide=guide, translation_memory=memory, table_reports=local_tables))
            else:
                pieces.append(block.text)
        table_seconds = time.monotonic() - started
        started = time.monotonic()
        activity(index, "正文")
        body_config = replace(config, alignments=alignments, request_progress=lambda retry: activity(index, "正文", retry))
        translated = engine.translate_segment("\n\n".join(pieces), excerpts[index - 1][-300:] if index else "", index + 1, len(ids),
                                               config=body_config, cache_dir=cache_dir, consistency_guide=guide, translation_memory=memory,
                                               quality_warnings=local_warnings, next_context=excerpts[index + 1][:300] if index + 1 < len(ids) else "",
                                               chapter_context=chapters[index])
        for report in local_tables:
            report["segment"] = index + 1
        source_tables = iter(b.text for b in blocks(segments[index]) if b.kind == "table")
        for unit in alignments:
            if unit['kind'] == 'table':
                unit['source'] = next(source_tables)
        return translated, local_tables, local_warnings, table_seconds, time.monotonic() - started, alignments

    started, table_seconds, body_seconds = time.monotonic(), 0, 0
    last_heartbeat = started
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
            done, _ = concurrent.futures.wait(futures, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)
            if time.monotonic() - last_heartbeat >= 10:
                waiting_progress()
                last_heartbeat = time.monotonic()
            for future in done:
                index = futures.pop(future)
                with activity_lock:
                    active.pop(index, None)
                record = state["segments"][ids[index]]
                try:
                    translated, tables, issues, ts, bs, alignments = future.result()
                    translations[index] = translated
                    table_seconds += ts
                    body_seconds += bs
                    table_reports.extend(tables)
                    warnings.extend(issues)
                    failures = any(r["failures"] for r in tables)
                    review = bool(issues) or any(r["residual_english"] or r["inline_artifacts"] or r.get("glossary_warnings") for r in tables)
                    status = "failed" if failures else "needs_review" if review else "completed"
                    path = out / "_chunks" / f"{ids[index]}.md"
                    atomic_write(path, translated)
                    record.update(status=status, output_hash=file_hash(path), warnings=issues, tables=tables, seconds=ts + bs, alignment=alignments)
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
    from review import make_document, render_document, load_edits, edit_issues, recover_alignment
    for i, key in enumerate(ids):
        record = state['segments'][key]
        if not record.get('alignment') and record.get('status') in {'completed', 'needs_review'}:
            try:
                record['alignment'] = recover_alignment(engine, config, segments[i], translations[i], cache_dir, guide,
                    excerpts[i - 1][-300:] if i else '', excerpts[i + 1][:300] if i + 1 < len(ids) else '', chapters[i], i + 1, len(ids))
            except (engine.OfflineCacheMiss, ValueError, TypeError):
                pass  # Older tasks remain readable even when exact alignment cannot be recovered offline.
    document = make_document(state, segments, translations, chapters, sidecar, read_json(out / 'source_document.json', {}), config)
    write_json(out / 'review_document.json', document)
    edits = load_edits(out, state['identity'])
    manual_issues = edit_issues(document, edits)
    result = render_document(document, edits)
    missing_images = _copy_images(result, root, out)
    parse_report = read_json(root / "parse_report.json", {})
    parse_warnings = parse_report.get("warnings", [])
    failed = [i + 1 for i, key in enumerate(ids) if state["segments"][key].get("status") not in {"completed", "needs_review"}]
    # Valid LaTeX in Markdown is expected. Report commands that remain after Word conversion.
    artifacts = engine.find_latex_artifacts(engine.normalize_inline_output(result))
    status = "partial_failed" if failed else "needs_review" if warnings or parse_warnings or missing_images or artifacts or any(r["residual_english"] or r["inline_artifacts"] or r.get("glossary_warnings") for r in table_reports) else "completed"
    if manual_issues and status == 'completed':
        status = 'needs_review'
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
              "manual_review": manual_issues, "manual_edit_count": len(edits['edits']),
              "parse_warnings": parse_warnings, "missing_images": missing_images, "formula_artifacts": artifacts,
              "references": reference_reports, "export_error": export_error, "export_warnings": export_warnings, "error": str(fatal) if fatal else None,
              "timings": {"parse": parse_seconds, "translation_wall": elapsed, "table_workers": table_seconds,
                          "body_workers": body_seconds, "retry_requests": sum(m["seconds"] for m in metrics if m.get("retry")),
                          "export": time.monotonic() - export_started},
              "request_count": len(metrics), "requests": metrics, "workers": workers,
              "glossary": {"matched_terms": sum(len(state["segments"][key].get("glossary_terms", [])) for key in ids),
                           "prompt_chars": sum(m.get("glossary_chars", 0) for m in metrics),
                           "retry_requests": sum(bool(m.get("retry")) for m in metrics),
                           "book_extraction_requests": list(config.glossary.snapshot.extraction_requests)}}
    write_json(out / "quality_report.json", report)
    lines = ["# 翻译质量报告", "", {"completed": "完成：自动检查未发现需要处理的问题。", "needs_review": "完成，但有待检查项。", "partial_failed": "部分失败；失败段落保留原文，可继续任务重试。"}[status],
             "", f"共 {len(ids)} 段；本次模型请求 {len(metrics)} 次。", "", "自动检查用于发现结构问题与疑点，不等同于人工语义审校。"]
    lines.extend(["", f"术语命中（按分段累计）：{report['glossary']['matched_terms']}；本次请求附加术语字符：{report['glossary']['prompt_chars']}；本次修订请求：{report['glossary']['retry_requests']}。"] )
    lines.append(f"本书累计术语提取请求：{len(config.glossary.snapshot.extraction_requests)}。服务返回的逐次用量见 JSON 报告。")
    for item in manual_issues:
        lines.append(f"- 人工修订 {item['unit']}：" + '；'.join(item['issues']))
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
    table_numbers = {}
    for table in table_reports:
        table_numbers[table['segment']] = table_numbers.get(table['segment'], 0) + 1
        if table["failures"] or table["residual_english"] or table["inline_artifacts"] or table.get("glossary_warnings"):
            details = list(table.get("glossary_warnings", []))
            if table['failures']:
                details.append(f"{len(table['failures'])} 个单元格未完成")
            if table['residual_english']:
                details.append('英文候选（可能是专名）：' + '、'.join(table['residual_english'][:3]))
            if table['inline_artifacts']:
                details.append('格式标记需核对')
            lines.append(f"- 第 {table['segment']} 段，表格 {table_numbers[table['segment']]}：" + '；'.join(details))
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
