import unittest
from types import SimpleNamespace

import pymupdf as fitz

from ui_helpers import AppTestCase


class InvoiceListTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.pdf_path = self.make_pdf("drag test invoice.pdf")

    def test_add_duplicate_and_remove_one(self):
        self.app.add_paths([str(self.pdf_path)])
        self.wait_idle()
        self.assertEqual(len(self.app.items), 2)
        self.app.add_paths([str(self.pdf_path)])
        self.wait_idle()
        self.assertEqual(len(self.app.items), 2)
        self.app.remove_item(0)
        self.assertEqual(len(self.app.items), 1)
        self.assertTrue(self.pdf_path.exists())

    def test_preview_uses_copies_per_invoice(self):
        self.app.add_paths([str(self.pdf_path)])
        self.wait_idle()
        self.app.layout_var.set("vertical")
        self.app.copies_var.set(2)
        self.app._rebuild_preview()
        self.wait_idle()
        self.assertEqual(self.app.preview_count, 2)
        with fitz.open(stream=self.app.preview_data, filetype="pdf") as doc:
            first_page = doc[0].get_text()
            second_page = doc[1].get_text()
        self.assertEqual(first_page.count("18.00"), 2)
        self.assertNotIn("25.00", first_page)
        self.assertEqual(second_page.count("25.00"), 2)

    def test_mouse_wheel_cannot_scroll_above_first_item(self):
        self.app.add_paths([str(self.pdf_path)])
        self.wait_idle()
        self.app.deiconify()
        self.app.update()
        self.app.thumb_canvas.yview_moveto(0)
        event = SimpleNamespace(
            widget=self.app.thumb_canvas,
            x_root=self.app.thumb_canvas.winfo_rootx() + 20,
            y_root=self.app.thumb_canvas.winfo_rooty() + 20,
            delta=120,
        )
        self.assertEqual(self.app._on_thumb_wheel(event), "break")
        self.assertAlmostEqual(self.app.thumb_canvas.yview()[0], 0.0)


if __name__ == "__main__":
    unittest.main()
