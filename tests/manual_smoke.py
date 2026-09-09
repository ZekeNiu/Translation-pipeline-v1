"""Generate a synthetic academic sample. --live uses the existing provider config."""
import argparse
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translate

SOURCE = '''# Synthetic academic validation sample

## 1 Methods

These values are synthetic test data, not research findings. Twenty participants completed two sessions. The measured duration was 45 s, and the recovery period was 2 min. The protocol used the same instruments in both sessions [1].

The energy relation is $E = m c^2$. Oxygen concentration is denoted by $O_2$. The symbols and numerical values must remain unchanged.

## 2 Results

<table><tr><th rowspan="2">Group</th><th colspan="2">Duration</th></tr><tr><th>Mean (s)</th><th>SD (s)</th></tr><tr><td>Control</td><td>45</td><td>3</td></tr><tr><td>Intervention</td><td>42</td><td>4</td></tr></table>

Table 1. Synthetic measurements for testing table alignment.

![](images/figure.png)

Figure 1. A reference image used to check that the original image remains next to its caption.

## References

[1] Example Author. Synthetic reference for software testing. Example Journal. 2026;1:1-2.

## 3 Discussion

This section follows the reference list and must still be translated. The observed difference does not establish causation. No additional interpretation should be introduced by the translator.

## Appendix

The appendix must remain after the discussion. The identifiers PDF and DOI are preserved as acronyms.
'''

TRANSLATIONS = {
    'Synthetic academic validation sample': '学术文档验证样例', '1 Methods': '1 方法', '2 Results': '2 结果',
    '3 Discussion': '3 讨论', 'Appendix': '附录', 'Group': '组别', 'Duration': '时长',
    'Mean (s)': '均值 (s)', 'Control': '对照组', 'Intervention': '干预组',
    'These values are synthetic test data, not research findings. Twenty participants completed two sessions. The measured duration was 45 s, and the recovery period was 2 min. The protocol used the same instruments in both sessions':
        '这些数值是用于软件测试的合成数据，不是研究结果。二十名参与者完成了两次测试。测得的持续时间为 45 s，恢复期为 2 min。两次测试均使用相同仪器',
    'The energy relation is': '能量关系为', 'Oxygen concentration is denoted by': '氧浓度表示为',
    'The symbols and numerical values must remain unchanged.': '符号和数值应保持不变。',
    'Table 1. Synthetic measurements for testing table alignment.': '表 1. 用于检查表格对齐的合成测量值。',
    'Figure 1. A reference image used to check that the original image remains next to its caption.': '图 1. 用于检查原始图片是否保留在图注旁的参考图。',
    'This section follows the reference list and must still be translated. The observed difference does not establish causation. No additional interpretation should be introduced by the translator.':
        '本节位于参考文献之后，仍须翻译。观察到的差异不能确立因果关系。翻译者不应引入额外解释。',
    'The appendix must remain after the discussion. The identifiers PDF and DOI are preserved as acronyms.':
        '附录应保留在讨论之后。PDF 和 DOI 作为缩写保留。',
}


def fake(config, messages, timeout=None):
    content = messages[-1]['content']
    is_table = content.startswith('[')
    values = json.loads(content) if is_table else [re.search(r'<SOURCE>\n(.*?)\n</SOURCE>', content, re.S)[1]]
    for i, value in enumerate(values):
        for source, target in sorted(TRANSLATIONS.items(), key=lambda kv: -len(kv[0])):
            value = value.replace(source, target)
        values[i] = value
    return json.dumps(values, ensure_ascii=False) if is_table else values[0]


def generate(root):
    from PIL import Image, ImageDraw
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, Image as PdfImage
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    root.mkdir(parents=True, exist_ok=True)
    (root / 'images').mkdir(exist_ok=True)
    asset = Image.new('RGB', (900, 200), 'white')
    draw = ImageDraw.Draw(asset)
    for x, label in ((40, 'Original page'), (340, 'Parsed document'), (640, 'Translated document')):
        draw.rectangle((x, 50, x + 210, 145), outline='#335577', width=3)
        draw.text((x + 15, 85), label, fill='black', font_size=17)
    draw.line((250, 100, 340, 100), fill='#335577', width=3)
    draw.line((550, 100, 640, 100), fill='#335577', width=3)
    asset.save(root / 'images' / 'figure.png')
    (root / 'full.md').write_text(SOURCE, encoding='utf-8')
    styles = getSampleStyleSheet()
    story = [Paragraph('Synthetic academic validation sample', styles['Title']), Spacer(1, 12)]
    for text in SOURCE.split('\n\n')[1:]:
        if text.startswith('<table'):
            table = Table([['Group', 'Duration', ''], ['', 'Mean (s)', 'SD (s)'], ['Control', '45', '3'], ['Intervention', '42', '4']], colWidths=[170, 120, 120])
            table.setStyle([('SPAN', (0, 0), (0, 1)), ('SPAN', (1, 0), (2, 0)), ('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')])
            story.extend([table, Spacer(1, 10)])
        elif text.startswith('!['):
            story.append(PdfImage(str(root / 'images' / 'figure.png'), width=450, height=100))
        elif text.startswith('## '):
            if 'Discussion' in text:
                story.append(PageBreak())
            story.append(Paragraph(text[3:], styles['Heading2']))
        else:
            story.extend([Paragraph(text.replace('$', ''), styles['BodyText']), Spacer(1, 8)])
    SimpleDocTemplate(str(root / 'sample.pdf')).build(story)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', default='tests/.artifacts/offline')
    args = parser.parse_args()
    root = Path(args.output).resolve()
    generate(root / 'source')
    if args.live:
        md, docx = translate.run(root / 'source', root / 'translation', max_workers=1)
    else:
        with patch('translate.call_chat_completion', side_effect=fake):
            md, docx = translate.run(root / 'source', root / 'translation', provider='custom', base_url='https://example.invalid', api_key='dummy-key', model='mock', max_workers=1)
    report = json.loads((md.parent / 'quality_report.json').read_text(encoding='utf-8'))
    print(json.dumps({'markdown': str(md), 'docx': str(docx), 'status': report['status'], 'requests': report['request_count'], 'warnings': report['warnings']}, ensure_ascii=False))
