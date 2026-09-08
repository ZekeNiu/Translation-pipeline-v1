"""Keep MinerU Markdown primary; repair only provably aligned seam content."""
from __future__ import annotations
from copy import deepcopy
import html
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

from task_state import atomic_write, file_hash, read_json, write_json


def safe_extract(archive, directory: Path):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or ":" in name or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("解析结果压缩包包含不安全路径。")
            target = (directory / name).resolve()
            if not target.is_relative_to(directory):
                raise ValueError("解析结果路径超出输出目录。")
        zf.extractall(directory)


def find_markdown(directory):
    files = sorted(Path(directory).rglob("*.md"))
    files.sort(key=lambda p: (p.name.lower() != "full.md", len(p.parts), str(p)))
    if not files:
        raise ValueError("解析结果没有 Markdown 文件。")
    path = files[0]
    if not path.read_text(encoding="utf-8").strip():
        raise ValueError("解析结果 Markdown 为空，不能作为完成结果。")
    return path


def result_fingerprint(directory):
    root = Path(directory)
    return {p.relative_to(root).as_posix(): file_hash(p) for p in sorted(root.rglob("*")) if p.is_file()}


def result_valid(part):
    directory = Path(part.get("result_dir", ""))
    hashes = part.get("result_hashes")
    if not directory.is_dir() or not hashes:
        return False
    try:
        return all((directory / name).is_file() and file_hash(directory / name) == expected for name, expected in hashes.items())
    except OSError:
        return False


def _copy_assets(markdown, source, output, part_id):
    replacements = {}
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg"}
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in image_exts:
            continue
        relative = path.relative_to(source).as_posix()
        dest = output / "assets" / part_id / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        replacements[relative] = dest.relative_to(output).as_posix()
    def convert(value):
        raw = value.strip().removeprefix("./").replace("\\", "/")
        return replacements.get(raw, value)
    markdown = re.sub(r"(!\[[^\]]*\]\()([^\n)]+)(\))", lambda m: m[1] + convert(m[2]) + m[3], markdown)
    markdown = re.sub(r'(\bsrc=[\"\'])([^\"\']+)([\"\'])', lambda m: m[1] + convert(m[2]) + m[3], markdown)
    return markdown, replacements


def _sidecar(source, offset, replacements):
    files = []
    for pattern in ("*_content_list.json", "content_list.json", "*_content_list_v2.json", "content_list_v2.json", "layout.json", "*_middle.json"):
        files.extend(sorted(source.glob(pattern)))
        if files:
            break
    if not files:
        return []
    value = deepcopy(read_json(files[0], []))
    def walk(node, page=None):
        if isinstance(node, dict):
            if isinstance(node.get("page_idx"), int):
                page = node["page_idx"]
            if page is not None:
                node["page_idx"] = page + offset
            for key, item in list(node.items()):
                if key in {"img_path", "image_path"} and isinstance(item, str):
                    node[key] = replacements.get(item.removeprefix("./"), item)
                elif isinstance(item, (dict, list)):
                    walk(item, page)
        elif isinstance(node, list):
            for item in node:
                walk(item, page)
    if isinstance(value, list) and value and all(isinstance(v, list) for v in value):
        for page, nodes in enumerate(value):
            walk(nodes, page)
    else:
        walk(value)
    return value


def _content_entries(data):
    if isinstance(data, list):
        for value in data:
            yield from _content_entries(value)
    elif isinstance(data, dict):
        # Only schema fields whose relation to Markdown is unambiguous.
        text = data.get("table_body") if data.get("type") == "table" else data.get("text")
        if isinstance(text, str) and isinstance(data.get("page_idx"), int):
            text = re.sub(r"</?(?:html|body)[^>]*>", "", text).strip()
            yield data["page_idx"], text
        else:
            for value in data.values():
                if isinstance(value, (dict, list)):
                    yield from _content_entries(value)


def _page_span(markdown, data, start, end):
    entries = [(page, text) for page, text in _content_entries(data) if start <= page < end and text]
    if not entries:
        return None
    spans = []
    for page, text in entries:
        if markdown.count(text) != 1:
            return None
        pos = markdown.index(text)
        line_start = markdown.rfind("\n", 0, pos) + 1
        if re.fullmatch(r"#{1,6}\s+", markdown[line_start:pos]):
            pos = line_start
        spans.append((pos, markdown.index(text) + len(text)))
    if spans != sorted(spans):
        return None
    return spans[0][0], spans[-1][1]


def _signature(text, root):
    # Preserve text, formula symbols and exact image bytes. Ignore layout-only syntax.
    def image_token(match):
        path = (root / match[1]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return match[0]
        return "IMAGE:" + file_hash(path)
    text = re.sub(r"!\[[^\]]*\]\(([^)]+)\)", image_token, text)
    text = re.sub(r"</?(?:table|thead|tbody|tr|td|th|html|body)\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    return re.sub(r"\s+", "", html.unescape(text))


def merge_parts(parts, output: Path, warnings=None):
    output.mkdir(parents=True, exist_ok=True)
    main = sorted((p for p in parts if p["kind"] == "main"), key=lambda p: p["start"])
    expected = 0
    texts, sidecars, all_warnings = [], [], list(warnings or [])
    for part in main:
        if part["start"] != expected:
            raise ValueError("PDF 页码覆盖不完整，停止合并。")
        expected = part["end"]
        if not result_valid(part):
            raise ValueError(f"解析分片 {part['id']} 缺失或已改变，停止合并。")
        md = find_markdown(part["result_dir"])
        text, replacements = _copy_assets(md.read_text(encoding="utf-8"), md.parent, output, part["id"])
        texts.append(text.strip())
        sidecars.append(_sidecar(md.parent, part["start"], replacements))
    merged = "\n\n".join(texts)
    seam_reports = []
    for part in (p for p in parts if p["kind"] == "seam"):
        report = {"pages": [part["start"] + 1, part["end"]], "status": "needs_review"}
        if not result_valid(part):
            report["reason"] = "接缝补充解析未完成；保留主分片原文。"
        else:
            md = find_markdown(part["result_dir"])
            bridge, replacements = _copy_assets(md.read_text(encoding="utf-8"), md.parent, output, part["id"])
            data = _sidecar(md.parent, part["start"], replacements)
            old_span = _page_span(merged, sidecars, part["start"], part["end"])
            new_span = _page_span(bridge, data, part["start"], part["end"])
            if old_span and new_span:
                old, new = merged[slice(*old_span)], bridge[slice(*new_span)]
                if _signature(old, output) == _signature(new, output):
                    merged = merged[:old_span[0]] + new + merged[old_span[1]:]
                    report.update(status="verified", reason="接缝页码和内容一致，已采用同窗口解析结构。")
                else:
                    report["reason"] = "接缝解析内容存在差异，未自动改写或去重。"
            else:
                report["reason"] = "无法唯一定位接缝内容，未自动改写或去重。"
        seam_reports.append(report)
        if report["status"] != "verified":
            all_warnings.append(report)
    atomic_write(output / "full.md", merged + "\n")
    write_json(output / "merged_content_list.json", sidecars)
    write_json(output / "parse_report.json", {"pages": expected, "parts": [{k: p[k] for k in ("id", "start", "end", "kind", "sha256")} for p in parts],
                                               "seams": seam_reports, "warnings": all_warnings})
    return output
