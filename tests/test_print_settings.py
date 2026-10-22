import unittest
from types import SimpleNamespace
from unittest.mock import patch

import win32con

from invoice_printer import Settings, printer_devmode


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


if __name__ == "__main__":
    unittest.main()
