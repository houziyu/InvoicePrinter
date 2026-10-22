import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as fitz

from invoice_printer import (InvoicePage, Settings, TaskCancelled, compose_pdf, extract_amount,
                             layout_slots, load_preferences, page_size, parse_amount, write_export)


class SafetyAndLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        with fitz.open() as doc:
            for value in (10, 20, 30):
                doc.new_page(width=500, height=300).insert_text((30, 50), f"CNY {value:.2f}")
            doc.save(self.source)
        self.items = [InvoicePage(str(self.source), i, str(i), None, b"") for i in range(3)]

    def tearDown(self):
        self.temp.cleanup()

    def test_layout_matrix_paper_direction_layout_copies_and_margins(self):
        for paper in ("A4", "A5", "Letter"):
            for landscape in (False, True):
                for layout in ("single", "vertical", "horizontal"):
                    for copies in (1, 2, 3):
                        with self.subTest(paper=paper, landscape=landscape, layout=layout, copies=copies):
                            settings = Settings(paper, landscape, layout, copies, True, 8)
                            slots = layout_slots(settings)
                            width, height = page_size(settings)
                            with fitz.open(stream=compose_pdf(self.items, settings), filetype="pdf") as doc:
                                self.assertEqual(doc.page_count, (3 * copies + len(slots) - 1) // len(slots))
                                text = "".join(p.get_text() for p in doc)
                                for value in (10, 20, 30):
                                    self.assertEqual(text.count(f"{value:.2f}"), copies)
                                self.assertAlmostEqual(doc[0].rect.width, width, places=2)
                                self.assertAlmostEqual(doc[0].rect.height, height, places=2)
                                self.assertTrue(doc[0].get_drawings())
                            for slot in slots:
                                self.assertGreater(slot.width, 0)
                                self.assertGreater(slot.height, 0)
        for margin in (0, 50):
            for layout in ("single", "vertical", "horizontal"):
                with fitz.open(stream=compose_pdf(self.items, Settings(paper="A5", layout=layout, margin_mm=margin)), filetype="pdf") as doc:
                    self.assertGreater(doc.page_count, 0)

    def test_invalid_layout_inputs_and_copy_boundary(self):
        for kwargs in ({"copies": 0}, {"copies": 100}, {"copies": 1.2}, {"margin_mm": float("nan")}, {"margin_mm": 51}, {"layout": "other"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                compose_pdf(self.items, Settings(**kwargs))
        with self.assertRaises(ValueError):
            compose_pdf([], Settings())
        with fitz.open(stream=compose_pdf(self.items[:1], Settings(copies=99, layout="vertical")), filetype="pdf") as doc:
            self.assertEqual(doc.page_count, 50)
            self.assertEqual(sum(p.get_text().count("10.00") for p in doc), 99)

    def test_compose_cancel_before_and_during_work(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            compose_pdf(self.items, Settings(), cancel=cancel)
        cancel.clear()
        with self.assertRaises(TaskCancelled):
            compose_pdf(self.items, Settings(), cancel=cancel, progress=lambda _: cancel.set())

    def test_atomic_export_preserves_existing_file_on_failure_and_cancel(self):
        target = self.root / "existing.pdf"
        target.write_bytes(b"existing")
        with patch("invoice_printer.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                write_export(target, b"new", [self.source])
        self.assertEqual(target.read_bytes(), b"existing")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            write_export(target, b"new", [self.source], cancel)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertEqual(list(self.root.glob(".invoice-*.tmp")), [])
        original = self.source.read_bytes()
        with self.assertRaises(ValueError):
            write_export(self.source, b"new", [self.source])
        self.assertEqual(self.source.read_bytes(), original)

    def test_amount_parse_and_automatic_negative_zero_and_priority(self):
        for raw, expected in (("", None), ("￥1,234.56", 1234.56), ("-12.34", -12.34), ("0", 0), ("12.3", 12.3)):
            self.assertEqual(parse_amount(raw), expected)
        for raw in ("nan", "inf", "1.234", "100000000", "abc", "-Infinity", "1e9999999"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_amount(raw)
        for text, expected in (("价税合计（小写）￥-123.45", -123.45), ("价税合计￥0.00", 0), ("单价 999.00 小写￥12.34", 12.34), ("CNY -20.00", -20), ("plain image", None)):
            page = SimpleNamespace(get_text=lambda _, text=text: text)
            self.assertEqual(extract_amount(page), expected)

    def test_preferences_invalid_corrupt_and_boundary(self):
        path = self.root / "settings.json"
        for raw in ("broken", "[]", "null", json.dumps({"paper": "bad", "layout": "bad", "landscape": "false", "margin_mm": float("nan"), "copies": 99})):
            path.write_text(raw, encoding="utf-8")
            settings, printer, compact = load_preferences(path)
            self.assertEqual(settings, Settings())
        for margin in (0, 50):
            path.write_text(json.dumps({"margin_mm": margin}), encoding="utf-8")
            self.assertEqual(load_preferences(path)[0].margin_mm, margin)
