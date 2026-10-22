import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as fitz

from invoice_printer import compose_pdf, load_preferences, make_thumbnail
from ui_helpers import AppTestCase


class WorkflowTests(AppTestCase):
    def test_import_order_restores_after_all_sort_modes(self):
        b = self.make_pdf("B.pdf", (9, 100))
        a = self.make_pdf("A.pdf", (30,))
        self.app.add_paths([b, a])
        self.wait_idle()
        original = [(x.path, x.page_no) for x in self.app.items]
        for mode in ("文件名", "金额从高到低"):
            self.app.sort_var.set(mode)
            self.app._sort_items()
            self.app.sort_var.set("添加顺序")
            self.app._sort_items()
            self.assertEqual([(x.path, x.page_no) for x in self.app.items], original)
        self.wait_idle()

    def test_multidigit_page_numbers_sort_numerically(self):
        self.import_pdf(self.make_pdf(values=range(12)))
        self.app.sort_var.set("文件名")
        self.app._sort_items()
        self.assertEqual([x.page_no for x in self.app.items], list(range(12)))

    def test_manual_order_and_restore(self):
        self.import_pdf(self.make_pdf(values=(10, 20, 30)))
        first, middle, last = [x.import_order for x in self.app.items]
        self.app.move_item(last, first)
        self.assertEqual([x.import_order for x in self.app.items], [last, first, middle])
        self.app.move_item(last, middle, after=True)
        self.assertEqual([x.import_order for x in self.app.items], [first, middle, last])
        self.app.sort_var.set("添加顺序")
        self.app._sort_items()
        self.assertEqual([x.import_order for x in self.app.items], [first, middle, last])

    def test_undo_single_batch_clear_and_duplicate_reimport(self):
        path = self.import_pdf(self.make_pdf(values=(10, 20, 30)))
        original = [x.import_order for x in self.app.items]
        self.app.remove_item(1)
        self.app.undo_remove()
        self.assertEqual([x.import_order for x in self.app.items], original)
        self.app.set_selected(1, False)
        self.app.remove_selected()
        self.assertEqual([x.amount for x in self.app.items], [20])
        self.app.undo_remove()
        self.assertFalse(self.app.items[1].selected)
        self.app.clear_items()
        self.assertEqual(len(self.app.items), 0)
        self.app.undo_remove()
        self.assertEqual([x.import_order for x in self.app.items], original)
        self.app.remove_item(0)
        self.app.add_paths([path])
        self.wait_idle()
        self.app.undo_remove()
        self.assertEqual(len(self.app.items), 3)
        self.assertEqual(len({(x.path, x.page_no) for x in self.app.items}), 3)
        self.assertTrue(path.exists())

    def test_selected_only_preview_export_and_summary(self):
        self.import_pdf()
        self.app.set_selected(0, False)
        self.app.copies_var.set("2")
        self.app.layout_var.set("vertical")
        self.app._settings_changed()
        self.wait_idle()
        self.assertIn("已选 1 / 共 2", self.app.summary_var.get())
        self.assertIn("25.00", self.app.summary_var.get())
        self.assertIn("共 2 张", self.app.output_var.get())
        target = self.root / "output.pdf"
        self.assertTrue(self.app.export_to_path(target))
        self.wait_idle()
        with fitz.open(target) as doc:
            self.assertEqual(doc.page_count, 1)
            self.assertEqual(doc[0].get_text().count("25.00"), 2)
            self.assertNotIn("18.00", doc[0].get_text())
        self.assertFalse(self.app.export_btn.instate(["disabled"]))

    def test_filter_selection_is_explicit_and_does_not_hide_print_selection(self):
        self.import_pdf()
        self.app.set_amount(0, "")
        self.app.filter_var.set("金额未识别")
        self.app._refresh_thumbnails()
        self.assertEqual([i for i, _ in self.app._visible_items()], [0])
        self.app.select_all(False)
        self.assertFalse(self.app.items[0].selected)
        self.assertTrue(self.app.items[1].selected)
        self.app.filter_var.set("金额待核对")
        self.assertEqual([i for i, _ in self.app._visible_items()], [1])
        self.app.set_amount(1, "-12.34")
        self.assertEqual(self.app._visible_items(), [])
        self.assertIn("-12.34", self.app.summary_var.get())
        self.assertIn("待核对 0", self.app.summary_var.get())

    def test_invalid_copies_cannot_output_or_silently_change(self):
        self.import_pdf()
        for raw in ("0", "100", "abc", "", "1.5", "-1", "1e1", "NaN"):
            self.app.copies_var.set(raw)
            self.app._settings_changed()
            self.assertFalse(self.app._validate_copies())
            self.assertEqual(self.app.copies_var.get(), raw)
            self.assertIsNone(self.app._output_snapshot())
            self.assertTrue(self.app.export_btn.instate(["disabled"]))
        self.wait_idle()
        self.assertIsNone(self.app.preview_data)
        self.app.copies_var.set("09")
        self.assertTrue(self.app._validate_copies())
        self.assertEqual(self.app.copies_var.get(), "9")
        self.app.copies_var.set("99")
        self.app._settings_changed()
        self.assertEqual(self.app.settings.copies, 99)

    def test_all_unselected_clears_preview_and_disables_output(self):
        self.import_pdf()
        self.app.select_all(False)
        self.wait_idle()
        self.assertIsNone(self.app.preview_data)
        self.assertEqual(self.app.preview_count, 0)
        self.assertTrue(self.app.print_btn.instate(["disabled"]))
        self.assertTrue(self.app.export_btn.instate(["disabled"]))
        self.app._jump_to_item(0)
        self.assertFalse(self.app.items[0].selected)
        self.app.select_all(True)
        self.wait_idle()
        self.assertEqual(self.app.preview_count, 2)

    def test_export_protects_selected_and_unselected_sources(self):
        path = self.import_pdf()
        original = path.read_bytes()
        self.app.set_selected(0, False)
        self.assertFalse(self.app.export_to_path(path))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.warnings), 1)

    def test_import_status_persists_after_preview_and_reports_all_errors(self):
        path = self.make_pdf()
        broken = []
        for i in range(12):
            p = self.root / f"broken-{i}.pdf"
            p.write_bytes(b"not a PDF")
            broken.append(p)
        encrypted = self.root / "encrypted.pdf"
        with fitz.open(path) as doc:
            doc.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
        self.app.add_paths([path, path, encrypted, *broken])
        self.wait_idle()
        self.assertEqual(len(self.app.items), 2)
        self.assertEqual(len(self.app.import_errors), 13)
        self.assertIn("跳过重复 2", self.app.status.get())
        self.assertIn("13 个文件", self.app.status.get())
        self.app.show_import_errors()
        dialogs = [x for x in self.app.winfo_children() if x.winfo_class() == "Toplevel"]
        self.assertEqual(len(dialogs), 1)
        dialogs[0].destroy()

    def test_drop_folder_and_chinese_space_paths(self):
        folder = self.root / "测试 文件夹"
        folder.mkdir()
        path = self.make_pdf("文件 有 空格.pdf", (10,))
        path.rename(folder / path.name)
        (folder / "ignore.txt").write_text("ignore")
        event = SimpleNamespace(data=self.app.tk.call("list", str(folder)), action="copy")
        self.assertEqual(self.app._drop_files(event), "copy")
        self.wait_idle()
        self.assertEqual(len(self.app.items), 1)

    def test_amount_edit_dialog_invalid_then_correct_and_cancel(self):
        self.import_pdf()
        with patch("invoice_printer.simpledialog.askstring", side_effect=["abc", "1,234.56"]):
            self.app.edit_amount(0)
        self.assertEqual(self.app.items[0].amount, 1234.56)
        self.assertTrue(self.app.items[0].amount_verified)
        self.assertEqual(len(self.warnings), 1)
        with patch("invoice_printer.simpledialog.askstring", return_value=None):
            self.app.edit_amount(0)
        self.assertEqual(self.app.items[0].amount, 1234.56)

    def test_preferences_roundtrip_reset_copies_and_no_invoice_data(self):
        self.import_pdf()
        self.app.paper_var.set("Letter")
        self.app.layout_var.set("horizontal")
        self.app.orientation_var.set("landscape")
        self.app.crop_var.set(True)
        self.app.copies_var.set("3")
        self.app.settings.margin_mm = 12.5
        self.app.compact_var.set(True)
        self.app._settings_changed()
        self.wait_idle()
        path = self.app.preferences_path
        settings, printer, compact = load_preferences(path)
        self.assertEqual((settings.paper, settings.layout, settings.landscape, settings.margin_mm), ("Letter", "horizontal", True, 12.5))
        self.assertEqual(settings.copies, 1)
        self.assertEqual(printer, "Test printer")
        self.assertTrue(compact)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("invoice.pdf", text)
        self.assertNotIn("copies", text)

    def test_printer_refresh_without_default_and_no_printers(self):
        with patch("invoice_printer.win32print.GetDefaultPrinter", side_effect=OSError("no default")):
            self.app._load_printers()
        self.assertEqual(self.app.printer_var.get(), "Test printer")
        self.import_pdf()
        with patch("invoice_printer.win32print.EnumPrinters", return_value=[]):
            self.app._load_printers()
        self.assertEqual(self.app.printer_var.get(), "")
        self.assertTrue(self.app.print_btn.instate(["disabled"]))
        self.assertFalse(self.app.export_btn.instate(["disabled"]))

    def test_print_snapshot_feedback_and_queue(self):
        self.import_pdf()
        self.app.copies_var.set("2")
        self.app.layout_var.set("vertical")
        self.app._settings_changed()
        self.wait_idle()
        started, release = threading.Event(), threading.Event()
        outputs = []
        def fake_print(data, printer, settings, **kwargs):
            started.set()
            release.wait(5)
            with fitz.open(stream=data, filetype="pdf") as doc:
                outputs.append((printer, settings.copies, doc.page_count, doc[0].get_text()))
        with patch("invoice_printer.print_pdf_bytes", side_effect=fake_print):
            self.app.print_document()
            self.spin_until(started.is_set)
            self.assertTrue(self.app.print_btn.instate(["disabled"]))
            self.app.copies_var.set("1")
            self.app.set_selected(0, False)
            release.set()
            self.wait_idle()
        self.assertEqual(outputs[0][:3], ("Test printer", 2, 2))
        self.assertEqual(outputs[0][3].count("18.00"), 2)
        self.assertIn("已提交打印任务", self.infos[-1][0])
        with patch("invoice_printer.subprocess.Popen") as popen:
            self.app.open_print_queue()
        self.assertIn("/o", popen.call_args.args[0])

    def test_print_failure_restores_buttons(self):
        self.import_pdf()
        with patch("invoice_printer.print_pdf_bytes", side_effect=RuntimeError("offline")):
            self.app.print_document()
            self.wait_idle()
        self.assertIn("offline", self.errors.pop()[1])
        self.assertFalse(self.app.print_btn.instate(["disabled"]))

    def test_preview_zoom_pan_fit_navigation_and_highlight(self):
        self.app.deiconify()
        self.app.geometry("1100x720")
        self.import_pdf(self.make_pdf(values=(10, 20, 30), size=(500, 300)))
        self.app.layout_var.set("vertical")
        self.app._settings_changed()
        self.wait_idle()
        self.app._jump_to_item(1)
        self.wait_idle()
        self.assertEqual(self.app.preview_page, 0)
        highlights = [i for i in self.app.preview_canvas.find_all() if self.app.preview_canvas.type(i) == "rectangle" and self.app.preview_canvas.itemcget(i, "dash")]
        self.assertEqual(len(highlights), 1)
        self.app.fit_preview("width")
        self.app._zoom(3)
        self.wait_idle()
        self.assertLess(self.app.preview_canvas.xview()[1] - self.app.preview_canvas.xview()[0], 1)
        self.app.preview_canvas.xview_moveto(1)
        self.assertGreater(self.app.preview_canvas.xview()[0], 0)
        self.app.fit_preview("page")
        self.wait_idle()
        self.assertAlmostEqual(self.app.preview_canvas.xview()[1], 1, places=2)
        self.app._go_last()
        self.wait_idle()
        self.assertEqual(self.app.preview_page, 1)
        self.app._go_page(-10)
        self.wait_idle()
        self.assertEqual(self.app.preview_page, 0)

    def test_latest_preview_wins_and_ui_remains_responsive(self):
        self.import_pdf()
        started, release = threading.Event(), threading.Event()
        calls = [0]
        def delayed(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                started.set()
                release.wait(5)
            return compose_pdf(*args, **kwargs)
        with patch("invoice_printer.compose_pdf", side_effect=delayed):
            self.app.copies_var.set("3")
            self.app._settings_changed()
            self.spin_until(started.is_set)
            heartbeat = []
            self.app.after(10, lambda: heartbeat.append(True))
            self.spin_until(lambda: bool(heartbeat))
            self.app.copies_var.set("1")
            self.app.set_selected(0, False)
            release.set()
            self.wait_idle()
        self.assertEqual(self.app.preview_count, 1)
        with fitz.open(stream=self.app.preview_data, filetype="pdf") as doc:
            self.assertNotIn("18.00", doc[0].get_text())

    def test_import_cancellation_keeps_completed_pages_and_queued_import(self):
        path = self.make_pdf(values=range(10, 30))
        started, release = threading.Event(), threading.Event()
        calls = [0]
        def slow_thumbnail(page):
            calls[0] += 1
            if calls[0] == 1:
                started.set()
                release.wait(5)
            return make_thumbnail(page)
        with patch("invoice_printer.make_thumbnail", side_effect=slow_thumbnail):
            self.app.add_paths([path])
            self.spin_until(started.is_set)
            self.app.cancel_task()
            release.set()
            self.wait_idle()
        self.assertGreater(len(self.app.items), 0)
        self.assertLess(len(self.app.items), 20)
        self.assertIn("取消", self.app.status.get())
        another = self.make_pdf("another.pdf", (99,))
        self.app.add_paths([path])
        self.app.add_paths([another])
        self.wait_idle()
        self.assertEqual(len(self.app.items), 21)

    def test_missing_source_preview_and_export_fail_without_partial_file(self):
        path = self.import_pdf()
        path.unlink()
        self.app._schedule_preview()
        self.wait_idle()
        self.assertIsNone(self.app.preview_data)
        self.assertTrue(self.errors)
        self.errors.clear()
        target = self.root / "failed.pdf"
        self.app.export_to_path(target)
        self.wait_idle()
        self.assertFalse(target.exists())
        self.assertTrue(self.errors)
        self.errors.clear()
        self.assertFalse(self.app.export_btn.instate(["disabled"]))

    def test_close_decline_then_cancel_running_task(self):
        started = threading.Event()
        def work(cancel, progress):
            started.set()
            cancel.wait(5)
        self.app._submit("export", work, lambda _: None)
        self.spin_until(started.is_set)
        with patch("invoice_printer.messagebox.askokcancel", return_value=False):
            self.app._on_close()
        self.assertFalse(self.app._closed)
        self.app._on_close()
        self.assertTrue(self.app._closed)

    def test_small_window_keeps_actions_and_list_toolbar_visible(self):
        self.app.deiconify()
        for width, height in ((1050, 640), (1100, 720), (1380, 850)):
            self.app.geometry(f"{width}x{height}")
            self.app.update()
            for widget in (self.app.undo_btn, self.app.add_btn, self.app.export_btn, self.app.print_btn, self.app.cancel_btn):
                with self.subTest(size=(width, height), text=widget.cget("text")):
                    self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth() - 1)
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.app.winfo_rootx() + self.app.winfo_width())
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.app.winfo_rooty() + self.app.winfo_height())

    def test_drop_feedback_preserves_import_result(self):
        event = SimpleNamespace(action="copy")
        self.app.status.set("previous result")
        self.app._drop_enter(event)
        self.app._drop_enter(event)
        self.app._drop_leave(event)
        self.assertEqual(self.app.status.get(), "previous result")
        self.app._drop_enter(event)
        self.app.status.set("new import result")
        self.app._drop_leave(event)
        self.assertEqual(self.app.status.get(), "new import result")
