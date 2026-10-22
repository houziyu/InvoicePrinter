import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

from invoice_printer import InvoicePage, Settings, compose_pdf, extract_amount


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pdf_path = Path(self.temp.name) / "invoice.pdf"
        doc = fitz.open()
        for value in (280.00, 125.50, 9.90):
            page = doc.new_page(width=500, height=300)
            page.insert_text((50, 70), f"Invoice Total CNY {value:.2f}", fontsize=16)
            page.insert_text((50, 110), f"Amount: Y {value:.2f}", fontsize=12)
        doc.save(self.pdf_path)
        doc.close()
        self.items = [InvoicePage(str(self.pdf_path), i, f"invoice-{i}", None, b"") for i in range(3)]

    def tearDown(self):
        self.temp.cleanup()

    def test_amount_fallback(self):
        doc = fitz.open(self.pdf_path)
        self.assertEqual(extract_amount(doc[0]), 280.00)
        doc.close()

    def test_single_layout(self):
        result = fitz.open(stream=compose_pdf(self.items, Settings(layout="single"), copies=1), filetype="pdf")
        self.assertEqual(result.page_count, 3)
        self.assertGreater(len(result[0].get_text()), 0)
        result.close()

    def test_two_up_layout_and_copies(self):
        settings = Settings(layout="vertical", copies=2, crop_marks=True)
        result = fitz.open(stream=compose_pdf(self.items, settings), filetype="pdf")
        self.assertEqual(result.page_count, 3)
        first_text = result[0].get_text()
        second_text = result[1].get_text()
        # 测试发票原页中金额出现两次；两份排版后应各出现四次。
        self.assertEqual(first_text.count("280.00"), 4)
        self.assertEqual(second_text.count("125.50"), 4)
        self.assertNotIn("125.50", first_text)
        result.close()

    def test_landscape_page(self):
        settings = Settings(landscape=True, layout="horizontal")
        result = fitz.open(stream=compose_pdf(self.items, settings, copies=1), filetype="pdf")
        self.assertGreater(result[0].rect.width, result[0].rect.height)
        self.assertEqual(result.page_count, 2)
        result.close()


if __name__ == "__main__":
    unittest.main()
