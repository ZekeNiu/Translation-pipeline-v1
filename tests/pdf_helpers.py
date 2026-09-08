from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject


def make_pdf(path, count, bookmarks=(), payload_bytes=0, scanned=False):
    writer = PdfWriter()
    for i in range(count):
        page = writer.add_blank_page(width=612 + i % 2, height=792)
        page.rotate(90 if i % 3 == 0 else 0)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        text = '' if scanned else f'BT /F1 12 Tf 50 700 Td (Original page {i + 1}) Tj ET\n'
        stream.set_data(text.encode() + b'%' + b'x' * payload_bytes)
        page[NameObject('/Contents')] = writer._add_object(stream)
    for page in bookmarks:
        writer.add_outline_item(f'Chapter {page}', page)
    writer.write(path)
    writer.close()
    return Path(path)
