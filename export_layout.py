"""Local PDF furniture extraction and task-specific export preferences."""
from collections import Counter
from pathlib import Path
import html
import threading

from task_paths import artifact
from task_state import read_json, write_json, fingerprint, file_hash, atomic_write

PDF_RENDER_LOCK = threading.Lock()  # PDFium must not be called concurrently.
MODES = {'original': '保留原样式', 'simple': '简洁样式', 'off': '关闭'}


def prepare_layout(out, document, options=None):
    saved = read_json(artifact(out, 'export_options.json'), {})
    options = {**saved, **(options or {})}
    mode = options.get('header_mode', 'original')
    if mode not in MODES:
        raise ValueError('未知页眉页脚样式。')
    if options == saved:
        write_json(artifact(out, 'export_options.json'), {'version': 1, 'header_mode': mode})
    title = options.get('title') or Path(document.get('source', {}).get('path', '')).stem or '译文'
    result = {'header_mode': mode, 'title': title[:80], 'furniture': {}, 'warnings': []}
    if mode != 'original':
        return result
    origin = document.get('source', {})
    source = Path(origin.get('path', ''))
    if not source.is_file() or source.suffix.lower() != '.pdf':
        result['header_mode'] = 'simple'
        result['warnings'].append('缺少原 PDF，页眉页脚使用简洁样式。')
        return result
    if origin.get('hash') and file_hash(source) != origin['hash']:
        result['header_mode'] = 'simple'
        result['warnings'].append('原 PDF 已变化，页眉页脚使用简洁样式。')
        return result
    from mineru_sidecar import load_mineru_sidecar
    state = read_json(artifact(out, 'task_state.json'), {})
    raw = Path(state.get('source', ''))
    sidecar = load_mineru_sidecar(raw.parent) if raw.is_file() else None
    if not sidecar or not sidecar.provenance:
        result['header_mode'] = 'simple'
        result['warnings'].append('缺少可靠版面坐标，页眉页脚使用简洁样式。')
        return result
    folder = artifact(out, 'page_furniture')
    folder.mkdir(parents=True, exist_ok=True)
    signature = fingerprint('furniture-v1', origin, source.stat().st_size, source.stat().st_mtime_ns, sidecar.furniture, sidecar.provenance)
    cache = read_json(folder / 'manifest.json', {})
    if cache.get('signature') == signature and all((folder / p).exists() for p in cache.get('images', {}).values()):
        result['furniture'] = {k: str(folder / v) for k, v in cache.get('images', {}).items()}
    else:
        try:
            with PDF_RENDER_LOCK:
                images = extract_furniture(source, sidecar, folder)
            write_json(folder / 'manifest.json', {'signature': signature, 'images': images})
            result['furniture'] = {k: str(folder / v) for k, v in images.items()}
        except Exception as exc:
            result['warnings'].append(f'原页边装饰提取失败，使用简洁样式：{exc}')
    if not result['furniture']:
        result['header_mode'] = 'simple'
        result['warnings'].append('未找到可安全复用的重复页边装饰，使用简洁样式。')
    return result


def extract_furniture(source, sidecar, folder):
    import pypdfium2 as pdfium
    from PIL import Image, ImageChops, ImageStat, ImageDraw
    choices = {'header': [], 'footer': []}
    with pdfium.PdfDocument(source) as pdf:
        # Cover is not a source of repeating furniture. Sample throughout long books.
        pages = sorted({1 + round(i * (len(pdf) - 2) / min(11, len(pdf) - 2)) for i in range(min(12, len(pdf) - 1))}) if len(pdf) > 2 else list(range(1, len(pdf)))
        for number in pages:
            body = [r['bbox'] for r in sidecar.provenance if r['page'] == number + 1 and r.get('bbox')]
            furniture = [r for r in sidecar.furniture if r['page'] == number + 1]
            body += [r['bbox'] for r in furniture if not r['margin']]
            if not body:
                continue
            page = pdf[number]
            bitmap = page.render(scale=2)
            image = bitmap.to_pil().convert('RGB').copy()
            bitmap.close()
            page.close()
            w, h = image.size
            draw = ImageDraw.Draw(image)
            for r in furniture:
                if r['type'] == 'page_number' or (r['margin'] and r['text'].strip().isdigit()):
                    a, b, c, d = r['bbox']
                    draw.rectangle((a*w/1000-3, b*h/1000-3, c*w/1000+3, d*h/1000+3), fill='white')
            bounds = {'header': (0, min(100, min(b[1] for b in body)-5)),
                      'footer': (max(900, max(b[3] for b in body)+5), 1000)}
            for kind, (y0, y1) in bounds.items():
                if y1-y0 < 8:
                    continue
                crop = image.crop((int(.07*w), int(y0*h/1000), int(.93*w), int(y1*h/1000)))
                difference = ImageChops.difference(crop, Image.new('RGB', crop.size, 'white')).convert('L')
                ink = difference.point(lambda v: 255 if v > 45 else 0).getbbox()
                if not ink or ink[2]-ink[0] < crop.width*.25:
                    continue
                crop = crop.crop((max(0,ink[0]-3),max(0,ink[1]-3),min(crop.width,ink[2]+3),min(crop.height,ink[3]+3)))
                thumb = crop.convert('L').resize((240, 40))
                choices[kind].append((crop, thumb))
        output = {}
        for kind, candidates in choices.items():
            if len(candidates) < 2:
                continue
            votes = [sum(ImageStat.Stat(ImageChops.difference(a[1], b[1])).mean[0] < 9 for b in candidates) for a in candidates]
            best = max(range(len(votes)), key=votes.__getitem__)
            if votes[best] < max(2, (len(candidates)+1)//2):
                continue
            candidates[best][0].save(folder / f'{kind}.png')
            output[kind] = f'{kind}.png'
    return output


def preview_layout(out, document, options=None):
    layout = prepare_layout(out, document, options)
    folder = artifact(out, 'page_furniture')
    folder.mkdir(parents=True, exist_ok=True)
    pictures = ''.join(f'<h2>{"页眉" if k == "header" else "页脚"}</h2><img style="max-width:100%" src="{Path(v).name}">' for k, v in layout['furniture'].items())
    path = folder / 'preview.html'
    atomic_write(path, '<meta charset="utf-8"><title>页眉页脚预览</title><body style="max-width:900px;margin:40px auto;font:18px/1.7 system-ui">'
                 f'<h1>{MODES[layout["header_mode"]]}</h1><p>译文使用新页码；装饰不代表原文分页。</p>{pictures}'
                 + '<p>' + html.escape('；'.join(layout['warnings'])) + '</p></body>')
    return path
