"""Deterministic integrity checks; semantic suspicions are reported, not proof."""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import re

TOKEN = re.compile(r"\[\[\[TP_[A-Z]+_\d+\]\]\]")


def plain(text):
    text = TOKEN.sub(" ", text)
    text = re.sub(r"https?://\S+|<[^>]+>", " ", text)
    return text.strip()


def numbers(text):
    text = plain(text)
    dates = []
    months = {name.lower(): i for i, names in enumerate((
        (), ('Jan', 'January'), ('Feb', 'February'), ('Mar', 'March'), ('Apr', 'April'), ('May',),
        ('Jun', 'June'), ('Jul', 'July'), ('Aug', 'August'), ('Sep', 'Sept', 'September'),
        ('Oct', 'October'), ('Nov', 'November'), ('Dec', 'December'))) for name in names}
    month_names = '|'.join(sorted(months, key=len, reverse=True))
    def canonical(match):
        values = match.groupdict()
        month = months[values['month'].lower()] if values.get('month') else int(values['m'])
        try:
            value = date(int(values['year']), month, int(values['day']))
        except ValueError:
            return match[0]
        dates.append('date:' + value.isoformat())
        return ' '
    for pattern in (
        rf'\b(?P<day>\d{{1,2}})\s+(?P<month>{month_names})\.?\s+(?P<year>\d{{4}}|\d{{2}})\b',
        rf'\b(?P<month>{month_names})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}}|\d{{2}})\b',
        r'(?<!\d)(?P<year>\d{4}|\d{2})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日',
    ):
        text = re.sub(pattern, canonical, text, flags=re.I)
    text = re.sub(r'(?<=\d)\s+(?=[%‰])', '', text)
    text = re.sub(r'(?<=\d)[xX](?=\d)', '×', text)
    # CJK text normally has no spaces before numbers (for example, 为45秒).
    text = text.replace('−', '-').replace('﹣', '-')
    # Normalize range dashes without interpreting the second endpoint as negative.
    text = re.sub(r'(?<=\d)[–—](?=\d)', '-', text)
    tokens = re.findall(r"(?<![A-Za-z0-9_])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?(?:%|‰)?", text)
    result = []
    for token in tokens:
        suffix = token[-1] if token[-1] in '%‰' else ''
        value = token[:-1] if suffix else token
        if re.fullmatch(r'[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?', value):
            value = value.replace(',', '')
        try:
            value = str(Decimal(value).normalize())
        except InvalidOperation:
            pass  # Ambiguous decimal-comma notation stays distinct.
        result.append(value + suffix)
    return Counter(dates + result)


def _written_number_count(text, token):
    """Recognize common written counts only when reconciling an actual mismatch."""
    if not token.isdigit() or not 0 <= int(token) < 100:
        return 0
    value = int(token)
    small = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
    tens = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']
    english = small[value] if value < 20 else tens[value // 10] + (r'[-\s]+' + small[value % 10] if value % 10 else '')
    digits = '零一二三四五六七八九'
    chinese = digits[value] if value < 10 else (digits[value // 10] if value >= 20 else '') + '十' + (digits[value % 10] if value % 10 else '')
    english_count = len(re.findall(r'\b' + english + r'\b', text, re.I))
    chinese_count = len(re.findall(r'(?<![零一二三四五六七八九十百千万\d])' + chinese + r'(?=[届名个位次轮台路秒分钟年小时天米维阶项])', text))
    return english_count + chinese_count


def equivalent_body_numbers(source, translated):
    left, right = numbers(source), numbers(translated)
    return (all(_written_number_count(translated, token) >= count for token, count in (left - right).items())
            and all(_written_number_count(source, token) >= count for token, count in (right - left).items()))


def validate_protected(source, translated):
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError("译文为空。")
    if TOKEN.findall(source) != TOKEN.findall(translated):
        raise ValueError("译文丢失、重复或调换了段落、公式、图片、表格或引用标记。")
    if "[[[TP_" in TOKEN.sub("", translated):
        raise ValueError("译文包含损坏的结构标记。")
    pattern = r"(\[\[\[TP_BEGIN_\d+\]\]\])([\s\S]*?)(\[\[\[TP_END_\d+\]\]\])"
    original_blocks, translated_blocks = re.findall(pattern, source), re.findall(pattern, translated)
    if original_blocks and re.sub(pattern, "", translated).strip():
        raise ValueError("译文在段落标记之外增加了内容。")
    for (_, original, _), (_, result, _) in zip(original_blocks, translated_blocks):
        if original.strip() and not result.strip():
            raise ValueError("译文存在空段落。")
        source_headings = re.findall(r"(?m)^(#{1,6})\s+", original.strip())
        result_headings = re.findall(r"(?m)^(#{1,6})\s+", result.strip())
        if source_headings != result_headings:
            raise ValueError("译文标题层级发生变化。")


def concerns(source, translated):
    source, translated = plain(source), plain(translated)
    warnings = []
    if not equivalent_body_numbers(source, translated):
        warnings.append("数字可能发生变化")
    words = re.findall(r"[A-Za-z]{2,}", source)
    if len(words) >= 6 and not re.search(r"[\u4e00-\u9fff]", translated):
        warnings.append("疑似整段未翻译")
    if len(source) >= 120 and len(translated) < len(source) * 0.12:
        warnings.append("译文异常短，可能漏译")
    if len(source) >= 120 and len(translated) > len(source) * 2:
        warnings.append("译文异常长，可能增加内容")
    return warnings
