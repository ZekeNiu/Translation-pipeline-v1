from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, NumberObject


def make_pdf(path, count, bookmarks=(), payload_bytes=0, scanned=False):
    writer = PdfWriter()
    for i in range(count):
        page = writer.add_blank_page(width=612 + i % 2, height=792)
        page.rotate(90 if i % 3 == 0 else 0)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        text = f'BT /F1 12 Tf 50 700 Td (Original page {i + 1}) Tj ET\n'
        if scanned:
            image = DecodedStreamObject()
            image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                          NameObject('/Width'): NumberObject(8), NameObject('/Height'): NumberObject(8),
                          NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
            image.set_data(bytes((i * 31 + n) % 256 for n in range(8 * 8 * 3)))
            page['/Resources'][NameObject('/XObject')] = DictionaryObject({NameObject('/Im1'): writer._add_object(image)})
            text = 'q 400 0 0 400 50 100 cm /Im1 Do Q\n'
        stream.set_data(text.encode() + b'%' + b'x' * payload_bytes)
        page[NameObject('/Contents')] = writer._add_object(stream)
    for page in bookmarks:
        writer.add_outline_item(f'Chapter {page}', page)
    writer.write(path)
    writer.close()
    return Path(path)
