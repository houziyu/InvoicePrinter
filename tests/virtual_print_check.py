"""手动集成检查：仅使用 Microsoft Print to PDF，生成合成发票，不打印到实体设备。

从项目根目录运行 python tests/virtual_print_check.py。
DOCINFO 的输出路径用于跳过虚拟打印机的文件名对话框，其余流程使用应用的真实打印实现。
"""
import ctypes
import json
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import invoice_printer as app


class DOCINFOW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_int), ("lpszDocName", wintypes.LPCWSTR),
                ("lpszOutput", wintypes.LPCWSTR), ("lpszDatatype", wintypes.LPCWSTR),
                ("fwType", wintypes.DWORD)]


def main():
    printer = "Microsoft Print to PDF"
    names = [p[2] for p in app.win32print.EnumPrinters(app.win32print.PRINTER_ENUM_LOCAL)]
    if printer not in names:
        raise RuntimeError("需要安装 Windows 自带的 Microsoft Print to PDF 虚拟打印机。")
    root = Path(tempfile.mkdtemp(prefix="InvoicePrinter-virtual-QA-"))
    source = root / "synthetic-source.pdf"
    with fitz.open() as doc:
        for i in range(3):
            page = doc.new_page(width=500, height=300)
            page.draw_rect((20, 20, 480, 280), color=(0.15, 0.2, 0.5))
            page.insert_text((40, 70), f"SYNTHETIC INVOICE QA-{i + 1:03}", fontsize=18)
            page.insert_text((40, 150), f"CNY {(i + 1) * 12.34:.2f}", fontsize=22)
        doc.save(source)
    # 混合 PDF、透明 PNG 与 JPEG，验证图片走过真实 Windows 打印流程。
    items = [app.InvoicePage(str(source), 0, "QA-PDF", None, b"")]
    with fitz.open(source) as doc:
        for page_no, extension in ((1, "png"), (2, "jpg")):
            path = root / f"synthetic-image.{extension}"
            pix = doc[page_no].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=extension == "png")
            pix.save(path)
            with app.open_invoice_document(path) as image_doc:
                items.append(app.InvoicePage(str(path), 0, f"QA-{extension}", None,
                                             app.make_thumbnail(image_doc[0])))
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.StartDocW.argtypes = (wintypes.HDC, ctypes.POINTER(DOCINFOW))
    gdi.StartDocW.restype = ctypes.c_int
    original_create_dc = app.win32ui.CreateDCFromHandle
    results = []
    for paper, landscape, layout, copies in (("A4", False, "vertical", 2),
                                               ("A5", True, "horizontal", 1),
                                               ("Letter", False, "single", 1)):
        settings = app.Settings(paper=paper, landscape=landscape, layout=layout, copies=copies)
        data = app.compose_pdf(items, settings)
        target = root / f"{paper}-{'landscape' if landscape else 'portrait'}-{layout}.pdf"

        class OutputDC:
            def __init__(self, dc):
                self.dc = dc

            def __getattr__(self, name):
                return getattr(self.dc, name)

            def StartDoc(self, name):
                info = DOCINFOW(ctypes.sizeof(DOCINFOW), name, str(target), None, 0)
                result = gdi.StartDocW(int(self.dc.GetHandleOutput()), ctypes.byref(info))
                if result <= 0:
                    raise ctypes.WinError(ctypes.get_last_error())
                return result

        with patch.object(app.win32ui, "CreateDCFromHandle", side_effect=lambda handle: OutputDC(original_create_dc(handle))):
            app.print_pdf_bytes(data, printer, settings)
        with fitz.open(target) as printed, fitz.open(stream=data, filetype="pdf") as expected:
            assert printed.page_count == expected.page_count, "打印页数与预览不一致"
            differences = []
            for actual_page, expected_page in zip(printed, expected):
                assert abs(actual_page.rect.width - expected_page.rect.width) < 2
                assert abs(actual_page.rect.height - expected_page.rect.height) < 2
                def thumbnail(page):
                    pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), alpha=False)
                    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                actual = thumbnail(actual_page)
                reference = thumbnail(expected_page).resize(actual.size)
                difference = sum(ImageStat.Stat(ImageChops.difference(actual, reference)).mean) / 3
                assert difference < 12, f"打印图像与预览偏差过大：{difference}"
                actual_ink = 255 - ImageStat.Stat(actual.convert("L")).mean[0]
                expected_ink = 255 - ImageStat.Stat(reference.convert("L")).mean[0]
                assert actual.convert("L").getextrema()[0] < 200, "打印结果为空白"
                assert 0.65 < actual_ink / expected_ink < 1.5, "打印内容覆盖量与预览不一致"
                differences.append(round(difference, 3))
            results.append({"paper": paper, "landscape": landscape, "layout": layout, "copies": copies,
                            "pages": printed.page_count, "image_difference_0_to_255": differences, "output": str(target)})
    print(json.dumps({"printer": printer, "results": results}, indent=2))


if __name__ == "__main__":
    main()
