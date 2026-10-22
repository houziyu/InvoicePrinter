import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from invoice_printer import InvoicePrinterApp


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.errors, self.warnings, self.infos = [], [], []
        for name, values in (("showerror", self.errors), ("showwarning", self.warnings), ("showinfo", self.infos)):
            mock = patch(f"invoice_printer.messagebox.{name}", side_effect=lambda *args, target=values, **kwargs: target.append(args))
            mock.start()
            self.addCleanup(mock.stop)
        for name, result in (("askyesno", False), ("askokcancel", True)):
            mock = patch(f"invoice_printer.messagebox.{name}", return_value=result)
            mock.start()
            self.addCleanup(mock.stop)
        enum = patch("invoice_printer.win32print.EnumPrinters", return_value=[(0, "", "Test printer", "")])
        enum.start()
        self.addCleanup(enum.stop)
        default = patch("invoice_printer.win32print.GetDefaultPrinter", return_value="Test printer")
        default.start()
        self.addCleanup(default.stop)
        self.app = InvoicePrinterApp(self.root / "preferences.json")
        self.app.withdraw()

    def make_pdf(self, name="invoice.pdf", values=(18.00, 25.00), size=(500, 300)):
        path = self.root / name
        with fitz.open() as doc:
            for value in values:
                page = doc.new_page(width=size[0], height=size[1])
                page.insert_text((30, 50), f"Invoice CNY {value:.2f}")
            doc.save(path)
        return path

    def spin_until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"Timed out; active jobs: {list(self.app._jobs)}, status: {self.app.status.get()}")

    def wait_idle(self):
        self.spin_until(lambda: not self.app._jobs and not self.app._preview_job and not self.app._render_job and not self.app._save_job)

    def import_pdf(self, path=None):
        path = path or self.make_pdf()
        self.app.add_paths([str(path)])
        self.wait_idle()
        return path

    def tearDown(self):
        self.app.destroy()
        self.app._executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()
        self.assertEqual(self.errors, [], f"Unexpected UI error dialogs: {self.errors}")
