import io
import tempfile
import threading
import unittest
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from invoice_printer import (IMAGE_EXTENSIONS, Settings, TaskCancelled, compose_pdf,
                             open_invoice_document, read_invoice_paths, write_export)


class ImageImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def make_image(self, name, color="red"):
        path = self.root / name
        with Image.new("RGB", (120, 60), color) as image:
            image.save(path)
        return path

    def read(self, paths, existing=None):
        return read_invoice_paths(paths, existing if existing is not None else set(),
                                  threading.Event(), lambda _: None)

    def test_all_formats_import_thumbnail_and_compose_without_resizing_pixels(self):
        for extension in IMAGE_EXTENSIONS:
            with self.subTest(extension=extension):
                path = self.make_image("图片 有空格" + extension.upper())
                items, errors, duplicates, cancelled = self.read([path, path])
                self.assertEqual((len(items), errors, duplicates, cancelled), (1, [], 1, False))
                self.assertIsNone(items[0].amount)
                with Image.open(io.BytesIO(items[0].thumb_png)) as thumb:
                    self.assertGreater(thumb.width, thumb.height)
                with fitz.open(stream=compose_pdf(items, Settings()), filetype="pdf") as result:
                    images = result[0].get_image_info()
                    self.assertEqual(len(images), 1)
                    self.assertEqual((images[0]["width"], images[0]["height"]), (120, 60))
                    bbox = fitz.Rect(images[0]["bbox"])
                    self.assertAlmostEqual(bbox.width / bbox.height, 2, places=4)

    def test_photo_exif_orientation_is_applied(self):
        path = self.root / "rotated.jpg"
        with Image.new("RGB", (120, 60), "red") as image:
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, exif=exif)
        with open_invoice_document(path) as doc:
            self.assertAlmostEqual(doc[0].rect.width / doc[0].rect.height, 0.5)
            info = doc[0].get_image_info()[0]
            self.assertEqual((info["width"], info["height"]), (60, 120))

    def test_transparency_is_composited_onto_white(self):
        for mode in ("RGBA", "P"):
            with self.subTest(mode=mode):
                path = self.root / f"transparent-{mode}.png"
                with Image.new("RGBA", (100, 50), (0, 0, 0, 0)) as image:
                    image.paste((255, 0, 0, 255), (50, 0, 100, 50))
                    image.convert(mode).save(path)
                with open_invoice_document(path) as doc:
                    pix = doc[0].get_pixmap(alpha=False)
                    self.assertEqual(pix.pixel(10, 10), (255, 255, 255))
                    self.assertEqual(pix.pixel(pix.width - 10, 10), (255, 0, 0))

    def test_multipage_tiff_and_animated_images(self):
        for extension in ("tiff", "gif", "webp"):
            with self.subTest(extension=extension):
                path = self.root / f"frames.{extension}"
                with Image.new("RGB", (120, 60), "red") as first, Image.new("RGB", (120, 60), "blue") as second:
                    first.save(path, save_all=True, append_images=[second], lossless=True)
                items, errors, _, _ = self.read([path])
                self.assertEqual(errors, [])
                self.assertEqual(len(items), 2 if extension == "tiff" else 1)
                with fitz.open(stream=compose_pdf(items, Settings()), filetype="pdf") as doc:
                    self.assertEqual(doc.page_count, len(items))
                    for index, page in enumerate(doc):
                        pix = page.get_pixmap(alpha=False)
                        self.assertEqual(pix.pixel(pix.width // 2, pix.height // 2),
                                         (255, 0, 0) if index == 0 else (0, 0, 255))

    def test_mixed_pdf_and_images_layout_copies_and_export(self):
        pdf = self.root / "invoice.pdf"
        with fitz.open() as doc:
            doc.new_page().insert_text((30, 50), "CNY 12.34")
            doc.save(pdf)
        items, errors, _, _ = self.read([pdf, self.make_image("image.png")])
        self.assertEqual(errors, [])
        self.assertEqual([item.amount for item in items], [12.34, None])
        for layout in ("single", "vertical", "horizontal"):
            with self.subTest(layout=layout):
                data = compose_pdf(items, Settings(layout=layout, copies=2))
                target = self.root / "result.pdf"
                write_export(target, data, [item.path for item in items])
                with fitz.open(target) as doc:
                    self.assertEqual(doc.page_count, 4 if layout == "single" else 2)
                    self.assertEqual(sum(p.get_text().count("12.34") for p in doc), 2)
                    self.assertEqual(sum(len(p.get_image_info()) for p in doc), 2)

    def test_bad_images_do_not_prevent_later_import_and_source_is_protected(self):
        broken = self.root / "broken.png"
        broken.write_bytes(b"broken")
        unsupported = self.root / "other.txt"
        unsupported.write_text("other")
        valid = self.make_image("valid.jpg")
        original = valid.read_bytes()
        items, errors, _, _ = self.read([broken, unsupported, valid])
        self.assertEqual((len(items), len(errors)), (1, 2))
        with self.assertRaises(ValueError):
            write_export(valid, b"replacement", [valid])
        self.assertEqual(valid.read_bytes(), original)
        valid.unlink()
        with self.assertRaises(FileNotFoundError):
            compose_pdf(items, Settings())

    def test_image_conversion_cancellation(self):
        path = self.make_image("cancel.png")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            open_invoice_document(path, cancel)
        # 取消后文件句柄已关闭。
        path.unlink()


if __name__ == "__main__":
    unittest.main()
