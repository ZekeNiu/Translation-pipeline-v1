from pathlib import Path
import tempfile
import unittest
from pypdf import PdfReader
from pdf_parts import prepare_parts
from pdf_helpers import make_pdf


class PdfPartsTests(unittest.TestCase):
    def test_page_thresholds_and_lossless_page_properties(self):
        for count in (199, 200, 201):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as td:
                source = make_pdf(Path(td) / 'source.pdf', count)
                parts, _, pages = prepare_parts(source, Path(td) / 'parts')
                main = [p for p in parts if p.kind == 'main']
                self.assertEqual(pages, count)
                self.assertEqual(len(main), 1 if count <= 200 else 2)
                self.assertEqual([i for p in main for i in range(p.start, p.end)], list(range(count)))
                original = PdfReader(source)
                for part in parts:
                    parsed = PdfReader(part.path)
                    self.assertLessEqual(len(parsed.pages), 200)
                    for n, page in enumerate(parsed.pages):
                        expected = original.pages[part.start + n]
                        self.assertEqual(page.mediabox, expected.mediabox)
                        self.assertEqual(page.rotation, expected.rotation)
                        self.assertEqual(page.extract_text(), expected.extract_text())

    def test_bookmarks_preferred_to_limit(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 210, bookmarks=[110])
            parts, _, _ = prepare_parts(source, Path(td) / 'parts')
            self.assertEqual(parts[0].end, 110)
            self.assertEqual(parts[0].reason, 'chapter')
            self.assertTrue(all(p.kind == 'main' for p in parts))

    def test_scanned_unknown_edges_get_seam_windows(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 9, scanned=True)
            parts, _, _ = prepare_parts(source, Path(td) / 'parts', max_pages=5)
            seam = next(p for p in parts if p.kind == 'seam')
            self.assertEqual((seam.start, seam.end), (3, 7))
            original = PdfReader(source)
            for part in parts:
                for i, page in enumerate(PdfReader(part.path).pages):
                    expected = original.pages[part.start + i]
                    self.assertEqual(page['/Resources']['/XObject']['/Im1'].get_data(), expected['/Resources']['/XObject']['/Im1'].get_data())
                    self.assertFalse(page.extract_text())

    def test_password_protected_pdf_is_rejected_before_upload(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'encrypted.pdf'
            writer = PdfWriter()
            writer.add_blank_page(612, 792)
            writer.encrypt('required-password')
            writer.write(path)
            writer.close()
            with self.assertRaisesRegex(ValueError, '损坏或加密'):
                prepare_parts(path, Path(td) / 'parts')

    def test_measured_size_forces_smaller_parts(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 6, payload_bytes=3000)
            parts, _, _ = prepare_parts(source, Path(td) / 'parts', max_bytes=8000)
            self.assertGreater(len([p for p in parts if p.kind == 'main']), 1)
            self.assertTrue(all(Path(p.path).stat().st_size <= 8000 for p in parts))

    def test_single_page_over_limit_is_not_downsampled(self):
        with tempfile.TemporaryDirectory() as td:
            source = make_pdf(Path(td) / 'source.pdf', 1, payload_bytes=10000)
            before = source.read_bytes()
            with self.assertRaisesRegex(ValueError, '单页'):
                prepare_parts(source, Path(td) / 'parts', max_bytes=2000)
            self.assertEqual(source.read_bytes(), before)

    def test_corrupt_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'bad.pdf'
            source.write_bytes(b'%PDF invalid')
            with self.assertRaisesRegex(ValueError, '损坏或加密'):
                prepare_parts(source, Path(td) / 'parts')
