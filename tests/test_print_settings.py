import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import threading
import pymupdf as fitz

import win32con

from invoice_printer import Settings, TaskCancelled, printer_devmode, print_pdf_bytes


class PrinterSettingsTests(unittest.TestCase):
    def test_job_settings_match_preview_without_driver_copies(self):
        for paper, landscape, code in (("A4", False, 9), ("A5", True, 11), ("Letter", False, 1)):
            with self.subTest(paper=paper, landscape=landscape):
                mode = SimpleNamespace(Fields=0, Orientation=0, PaperSize=0, Duplex=0, Copies=3)
                with patch("invoice_printer.win32print.OpenPrinter", return_value=object()), \
                     patch("invoice_printer.win32print.GetPrinter", return_value={"pDevMode": mode}), \
                     patch("invoice_printer.win32print.DocumentProperties", return_value=1) as document_properties, \
                     patch("invoice_printer.win32print.ClosePrinter") as close_printer:
                    result = printer_devmode("printer", Settings(paper=paper, landscape=landscape, copies=2))
                self.assertIs(result, mode)
                self.assertEqual(mode.PaperSize, code)
                self.assertEqual(mode.Orientation, win32con.DMORIENT_LANDSCAPE if landscape else win32con.DMORIENT_PORTRAIT)
                self.assertEqual(mode.Duplex, win32con.DMDUP_SIMPLEX)
                self.assertEqual(mode.Copies, 1)
                self.assertEqual(mode.Fields & (win32con.DM_ORIENTATION | win32con.DM_PAPERSIZE |
                                                win32con.DM_DUPLEX | win32con.DM_COPIES),
                                 win32con.DM_ORIENTATION | win32con.DM_PAPERSIZE |
                                 win32con.DM_DUPLEX | win32con.DM_COPIES)
                document_properties.assert_called_once()
                close_printer.assert_called_once()

    def test_driver_failure_and_rejected_fields_close_handle(self):
        for field, value in (("PaperSize", 1), ("Orientation", 2), ("Duplex", 2), ("Copies", 3), (None, None)):
            mode = SimpleNamespace(Fields=0, Orientation=1, PaperSize=9, Duplex=1, Copies=1)
            def driver(*args):
                if field:
                    setattr(mode, field, value)
                    return 1
                return -1
            with self.subTest(field=field), \
                 patch("invoice_printer.win32print.OpenPrinter", return_value=object()), \
                 patch("invoice_printer.win32print.GetPrinter", return_value={"pDevMode": mode}), \
                 patch("invoice_printer.win32print.DocumentProperties", side_effect=driver), \
                 patch("invoice_printer.win32print.ClosePrinter") as close:
                with self.assertRaises(RuntimeError):
                    printer_devmode("printer", Settings())
                close.assert_called_once()

    def test_print_success_cancel_and_device_failure_cleanup(self):
        with fitz.open() as doc:
            doc.new_page().insert_text((30, 50), "Synthetic QA invoice")
            data = doc.tobytes()
        for outcome in ("success", "cancel", "failure"):
            with self.subTest(outcome=outcome):
                cancel = threading.Event()
                dc = MagicMock()
                caps = {win32con.PHYSICALWIDTH: 2480, win32con.PHYSICALHEIGHT: 3508,
                        win32con.PHYSICALOFFSETX: 20, win32con.PHYSICALOFFSETY: 20}
                dc.GetDeviceCaps.side_effect = caps.__getitem__
                if outcome == "cancel":
                    dc.EndPage.side_effect = cancel.set
                if outcome == "failure":
                    dc.StartPage.side_effect = OSError("printer offline")
                with patch("invoice_printer.printer_devmode", return_value=object()), \
                     patch("invoice_printer.win32gui.CreateDC", return_value=123), \
                     patch("invoice_printer.win32ui.CreateDCFromHandle", return_value=dc), \
                     patch("invoice_printer.ImageWin.Dib"):
                    if outcome == "success":
                        print_pdf_bytes(data, "printer", Settings(), cancel=cancel)
                        dc.EndDoc.assert_called_once()
                        dc.AbortDoc.assert_not_called()
                    else:
                        with self.assertRaises(TaskCancelled if outcome == "cancel" else OSError):
                            print_pdf_bytes(data, "printer", Settings(), cancel=cancel)
                        dc.AbortDoc.assert_called_once()
                        dc.EndDoc.assert_not_called()
                dc.DeleteDC.assert_called_once()


if __name__ == "__main__":
    unittest.main()
