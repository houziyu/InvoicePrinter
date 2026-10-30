from __future__ import annotations

import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal, InvalidOperation
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import pymupdf as fitz
from PIL import Image, ImageOps, ImageTk, ImageWin
from tkinterdnd2 import DND_FILES, TkinterDnD

try:
    import win32con
    import win32gui
    import win32print
    import win32ui
except ImportError:  # 便于在非 Windows 环境中预览界面
    win32con = win32gui = win32print = win32ui = None


APP_NAME = "简印 · 发票打印"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif")
SUPPORTED_EXTENSIONS = (".pdf", *IMAGE_EXTENSIONS)
IMPORT_FILETYPES = [
    ("PDF 和图片", tuple(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)),
    ("PDF 文件", "*.pdf"),
    ("图片文件", tuple(f"*{ext}" for ext in IMAGE_EXTENSIONS)),
]
COLORS = {
    "brand": "#3159C9",
    "brand_hover": "#2447AA",
    "brand_soft": "#EAF0FF",
    "canvas": "#F3F5F9",
    "sidebar": "#F9FAFD",
    "panel": "#FFFFFF",
    "border": "#E2E8F0",
    "text": "#1C2941",
    "muted": "#64748B",
    "danger": "#DC2626",
}
PAPER_SIZES = {
    "A4": (595.276, 841.890),
    "A5": (419.528, 595.276),
    "Letter": (612.0, 792.0),
}
PAPER_CODES = {"A4": 9, "A5": 11, "Letter": 1}


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


@dataclass
class InvoicePage:
    path: str
    page_no: int
    label: str
    amount: float | None
    thumb_png: bytes
    import_order: int = -1
    selected: bool = True
    amount_verified: bool = False


@dataclass
class Settings:
    paper: str = "A4"
    landscape: bool = False
    layout: str = "single"  # single / vertical / horizontal
    copies: int = 1
    crop_marks: bool = False
    margin_mm: float = 8.0


class TaskCancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise TaskCancelled()


def canonical_path(path):
    return os.path.normcase(os.path.realpath(path))


def parse_amount(value: str) -> float | None:
    value = value.strip().replace(",", "").replace("，", "").replace("￥", "").replace("¥", "").strip()
    if not value:
        return None
    try:
        number = Decimal(value)
        if not number.is_finite() or number.copy_abs() >= 100_000_000 or number != number.quantize(Decimal("0.01")):
            raise ValueError
        return float(number)
    except (InvalidOperation, ValueError):
        raise ValueError("请输入有效金额，最多两位小数，绝对值小于一亿元；留空表示未识别。") from None


def write_export(path, data, source_paths, cancel=None):
    """同目录临时文件 + 原子替换，避免失败时留下半个 PDF。"""
    target = Path(path)
    if canonical_path(target) in {canonical_path(p) for p in source_paths}:
        raise ValueError("不能覆盖已导入的原始文件，请选择其他文件名。")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".invoice-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        check_cancel(cancel)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_preferences(path):
    settings = Settings()
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            return settings, "", False
        if values.get("paper") in PAPER_SIZES:
            settings.paper = values["paper"]
        if values.get("layout") in ("single", "vertical", "horizontal"):
            settings.layout = values["layout"]
        for name in ("landscape", "crop_marks"):
            if isinstance(values.get(name), bool):
                setattr(settings, name, values[name])
        margin = values.get("margin_mm", 8)
        if isinstance(margin, (int, float)) and math.isfinite(margin) and 0 <= margin <= 50:
            settings.margin_mm = float(margin)
        return settings, str(values.get("printer", "")), values.get("compact") is True
    except (OSError, ValueError, TypeError):
        return settings, "", False


def extract_amount(page: fitz.Page) -> float | None:
    """尽量从价税合计附近提取金额；失败时取带两位小数的合理最大值。"""
    text = page.get_text("text") or ""
    compact = re.sub(r"\s+", "", text)
    patterns = [
        r"价税合计[^¥￥\d-]{0,20}[¥￥]?(-?[0-9,]+\.\d{2})",
        r"小写[^¥￥\d-]{0,20}[¥￥]?(-?[0-9,]+\.\d{2})",
        r"[¥￥]\s*(-?[0-9,]+\.\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                pass
    values = []
    for raw in re.findall(r"(?<![\d.])(-?[0-9]{1,9}(?:,[0-9]{3})*\.\d{2})(?!\d)", text):
        try:
            val = float(raw.replace(",", ""))
            if abs(val) < 100_000_000:
                values.append(val)
        except ValueError:
            pass
    return max(values, key=abs) if values else None


def make_thumbnail(page: fitz.Page, max_size=(112, 150)) -> bytes:
    pix = page.get_pixmap(matrix=fitz.Matrix(0.55, 0.55), alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def page_size(settings: Settings) -> tuple[float, float]:
    w, h = PAPER_SIZES[settings.paper]
    return (h, w) if settings.landscape else (w, h)


def layout_slots(settings: Settings) -> list[fitz.Rect]:
    w, h = page_size(settings)
    margin = settings.margin_mm * 72 / 25.4
    gap = 8.0
    usable = fitz.Rect(margin, margin, w - margin, h - margin)
    if settings.layout == "vertical":
        mid = (usable.y0 + usable.y1) / 2
        return [
            fitz.Rect(usable.x0, usable.y0, usable.x1, mid - gap / 2),
            fitz.Rect(usable.x0, mid + gap / 2, usable.x1, usable.y1),
        ]
    if settings.layout == "horizontal":
        mid = (usable.x0 + usable.x1) / 2
        return [
            fitz.Rect(usable.x0, usable.y0, mid - gap / 2, usable.y1),
            fitz.Rect(mid + gap / 2, usable.y0, usable.x1, usable.y1),
        ]
    return [usable]


def fit_rect(source: fitz.Rect, target: fitz.Rect) -> fitz.Rect:
    scale = min(target.width / source.width, target.height / source.height)
    w, h = source.width * scale, source.height * scale
    x = target.x0 + (target.width - w) / 2
    y = target.y0 + (target.height - h) / 2
    return fitz.Rect(x, y, x + w, y + h)


def open_invoice_document(path, cancel=None) -> fitz.Document:
    """统一返回 PDF；图片保留像素，校正方向并铺白底，调用方负责关闭。"""
    extension = Path(path).suffix.lower()
    if extension == ".pdf":
        return fitz.open(path)
    if extension not in IMAGE_EXTENSIONS:
        raise ValueError("不支持的文件格式，请选择 PDF 或 PNG、JPG、BMP、TIFF、WebP、GIF 图片。")
    document = fitz.open()
    try:
        with Image.open(path) as source:
            # TIFF 扫描件按页导入；动画图片只使用首帧。
            page_count = getattr(source, "n_frames", 1) if source.format == "TIFF" else 1
            for page_no in range(page_count):
                check_cancel(cancel)
                source.seek(page_no)
                with ImageOps.exif_transpose(source) as oriented:
                    with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as rgb:
                        rgb.paste(rgba, mask=rgba.getchannel("A"))
                        with io.BytesIO() as stream:
                            rgb.save(stream, "PNG")
                            # 页尺寸只用于保持比例，像素数据不缩小。
                            scale = 1000 / max(rgb.size)
                            page = document.new_page(width=rgb.width * scale, height=rgb.height * scale)
                            page.insert_image(page.rect, stream=stream.getvalue())
                check_cancel(cancel)
        return document
    except BaseException:
        document.close()
        raise


def compose_pdf(items: list[InvoicePage], settings: Settings, copies: int | None = None, cancel=None, progress=None) -> bytes:
    copy_count = settings.copies if copies is None else copies
    if not items:
        raise ValueError("请先勾选至少一张发票。")
    if not isinstance(copy_count, int) or not 1 <= copy_count <= 99:
        raise ValueError("每张发票份数必须是 1 到 99 的整数。")
    if not math.isfinite(settings.margin_mm) or not 0 <= settings.margin_mm <= 50:
        raise ValueError("页边距必须在 0 到 50 毫米之间。")
    if settings.layout not in ("single", "vertical", "horizontal"):
        raise ValueError("未知的排版方式。")
    check_cancel(cancel)
    output = fitz.open()
    opened: dict[str, fitz.Document] = {}
    slots = layout_slots(settings)
    # “份数”属于每张发票：A、B 各 2 份应排成 A、A、B、B，
    # 而不是把整份清单排成 A、B、A、B。
    repeated = [item for item in items for _ in range(copy_count)]
    try:
        for start in range(0, len(repeated), len(slots)):
            check_cancel(cancel)
            w, h = page_size(settings)
            target_page = output.new_page(width=w, height=h)
            for slot, item in zip(slots, repeated[start : start + len(slots)]):
                check_cancel(cancel)
                source_doc = opened.get(item.path)
                if source_doc is None:
                    source_doc = open_invoice_document(item.path, cancel)
                    opened[item.path] = source_doc
                source_page = source_doc[item.page_no]
                dest = fit_rect(source_page.rect, slot)
                target_page.show_pdf_page(dest, source_doc, item.page_no, keep_proportion=True)
                if settings.crop_marks:
                    draw_crop_marks(target_page, dest)
            if progress:
                progress(f"正在排版：{output.page_count} / {(len(repeated) + len(slots) - 1) // len(slots)} 页")
        return output.tobytes(garbage=4, deflate=True)
    finally:
        output.close()
        for doc in opened.values():
            doc.close()


def draw_crop_marks(page: fitz.Page, rect: fitz.Rect) -> None:
    length, offset = 8.0, 3.0
    points = [
        ((rect.x0 - offset - length, rect.y0), (rect.x0 - offset, rect.y0)),
        ((rect.x0, rect.y0 - offset - length), (rect.x0, rect.y0 - offset)),
        ((rect.x1 + offset, rect.y0), (rect.x1 + offset + length, rect.y0)),
        ((rect.x1, rect.y0 - offset - length), (rect.x1, rect.y0 - offset)),
        ((rect.x0 - offset - length, rect.y1), (rect.x0 - offset, rect.y1)),
        ((rect.x0, rect.y1 + offset), (rect.x0, rect.y1 + offset + length)),
        ((rect.x1 + offset, rect.y1), (rect.x1 + offset + length, rect.y1)),
        ((rect.x1, rect.y1 + offset), (rect.x1, rect.y1 + offset + length)),
    ]
    for p1, p2 in points:
        page.draw_line(p1, p2, color=(0, 0, 0), width=0.5)


def read_invoice_paths(paths, existing, cancel, progress):
    items, errors, duplicates = [], [], 0
    try:
        for file_index, path in enumerate(paths):
            check_cancel(cancel)
            try:
                with open_invoice_document(path, cancel) as doc:
                    if doc.needs_pass:
                        errors.append(f"{Path(path).name}（需要密码，未添加）")
                        continue
                    if not doc.is_pdf or doc.page_count == 0:
                        errors.append(f"{Path(path).name}（不是有效的 PDF 或图片）")
                        continue
                    for page_no in range(doc.page_count):
                        check_cancel(cancel)
                        progress(f"读取文件 {file_index + 1}/{len(paths)}，第 {page_no + 1}/{doc.page_count} 页")
                        key = (canonical_path(path), page_no)
                        if key in existing:
                            duplicates += 1
                            continue
                        page = doc[page_no]
                        suffix = f" · 第 {page_no + 1} 页" if doc.page_count > 1 else ""
                        items.append(InvoicePage(str(Path(path).resolve()), page_no, Path(path).name + suffix,
                                                 extract_amount(page), make_thumbnail(page)))
                        existing.add(key)
            except TaskCancelled:
                raise
            except Exception as exc:
                errors.append(f"{Path(path).name}（{exc}）")
    except TaskCancelled:
        return items, errors, duplicates, True
    return items, errors, duplicates, False


def render_pdf_preview(data, page_no, canvas_size, mode, zoom, highlight, settings, cancel):
    check_cancel(cancel)
    with fitz.open(stream=data, filetype="pdf") as doc:
        page_no = max(0, min(page_no, doc.page_count - 1))
        page = doc[page_no]
        width, height = canvas_size
        fit_scale = min(width / page.rect.width, height / page.rect.height)
        scale = width / page.rect.width if mode == "width" else fit_scale
        scale = max(0.1, min(4, scale * zoom))
        # 限制极端超宽显示器下的预览内存，实际 PDF 保持矢量。
        scale = min(scale, 3500 / max(page.rect.width, page.rect.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        box = None
        if highlight is not None:
            slot_index = highlight - page_no * len(layout_slots(settings))
            if 0 <= slot_index < len(layout_slots(settings)):
                rect = layout_slots(settings)[slot_index]
                box = tuple(v * scale for v in rect)
        check_cancel(cancel)
        return pix.tobytes("png"), doc.page_count, page_no, scale, box


class MarginDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, current: float):
        super().__init__(parent)
        self.result = None
        self.title("页边距")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        ttk.Label(self, text="四周页边距（毫米）").grid(row=0, column=0, padx=18, pady=(18, 6), sticky="w")
        self.value = tk.DoubleVar(value=current)
        spin = ttk.Spinbox(self, from_=0, to=50, increment=1, textvariable=self.value, width=18)
        spin.grid(row=1, column=0, padx=18, sticky="ew")
        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, padx=18, pady=18, sticky="e")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="确定", command=self.ok).pack(side="left")
        self.bind("<Return>", lambda _: self.ok())
        self.bind("<Escape>", lambda _: self.destroy())
        self.wait_visibility()
        spin.focus_set()

    def ok(self):
        try:
            value = float(self.value.get())
            if not 0 <= value <= 50:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showwarning("页边距", "请输入 0 到 50 之间的数字。", parent=self)
            return
        self.result = value
        self.destroy()


class InvoicePrinterApp(TkinterDnD.Tk):
    def __init__(self, preferences_path=None):
        super().__init__()
        self.title(APP_NAME)
        self.geometry(f"{min(1380, self.winfo_screenwidth() - 60)}x{min(850, self.winfo_screenheight() - 90)}")
        self.minsize(1050, 640)
        self.configure(bg=COLORS["canvas"])
        self.preferences_path = Path(preferences_path) if preferences_path else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InvoicePrinter" / "settings.json"
        self.settings, self._saved_printer, self._saved_compact = load_preferences(self.preferences_path)
        self.items: list[InvoicePage] = []
        self.preview_data: bytes | None = None
        self.preview_count = 0
        self.preview_image: ImageTk.PhotoImage | None = None
        self.thumb_images: list[ImageTk.PhotoImage] = []
        self.preview_page = 0
        self.zoom = 1.0
        self.zoom_mode = "page"
        self._render_job = None
        self._save_job = None
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="invoice")
        self._events = queue.Queue()
        self._jobs = {}
        self._job_serial = 0
        self._undo_stack = []
        self._next_order = 0
        self._highlight_order = None
        self.import_errors = []
        self._pending_paths = []
        self._preview_job = None
        self._thumb_reset_job = None
        self._thumb_hover = False
        self.status = tk.StringVar(value="添加 PDF 或图片发票后即可预览和打印")
        self.count_var = tk.StringVar(value="0 张")
        self.output_var = tk.StringVar(value="A4 · 纵向 · 每页 1 张")
        self._configure_style()
        self._build_ui()
        self._setup_drag_and_drop()
        self._load_printers()
        self._refresh_thumbnails()
        self._update_summary()
        self._schedule_preview()
        self._poll_job = self.after(50, self._poll_tasks)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-o>", lambda _: self.add_files())
        self.bind("<Control-z>", lambda _: self.undo_remove())

    def _configure_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["panel"])
        style.configure("TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"), foreground=COLORS["text"])
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 9, "bold"), foreground="#475569")
        style.configure("Muted.TLabel", foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", background=COLORS["brand"], foreground="white", padding=(18, 10), borderwidth=0, font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", COLORS["brand_hover"]), ("disabled", "#AFC4EC")])
        style.configure("Secondary.TButton", background="#FFFFFF", foreground="#334155", padding=(14, 9), borderwidth=1, relief="solid", font=("Microsoft YaHei UI", 9))
        style.map("Secondary.TButton", background=[("active", "#F1F5F9")])
        style.configure("Tool.TButton", background="#FFFFFF", foreground="#475569", padding=(7, 5), borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.map("Tool.TButton", background=[("active", COLORS["brand_soft"])], foreground=[("active", COLORS["brand"])])
        style.configure("Compact.Tool.TButton", padding=(3, 4), font=("Microsoft YaHei UI", 9))
        style.configure("TButton", padding=(10, 7), font=("Microsoft YaHei UI", 9), borderwidth=1)
        style.configure("TRadiobutton", background=COLORS["panel"], font=("Microsoft YaHei UI", 9))
        style.configure("TCheckbutton", background=COLORS["panel"], font=("Microsoft YaHei UI", 9))
        style.configure("TCombobox", padding=7, fieldbackground="#FFFFFF", background="#FFFFFF", bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"])
        style.configure("TSpinbox", padding=7, fieldbackground="#FFFFFF", bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"])
        style.configure("Vertical.TScrollbar", background="#CBD5E1", troughcolor=COLORS["sidebar"], borderwidth=0, arrowsize=0)

    def _build_ui(self):
        header = tk.Frame(self, bg=COLORS["panel"], height=66, highlightthickness=0)
        header.pack(fill="x")
        header.pack_propagate(False)
        logo = tk.Frame(header, bg=COLORS["brand"], width=38, height=38)
        logo.pack(side="left", padx=(22, 11))
        logo.pack_propagate(False)
        tk.Label(logo, text="▣", fg="white", bg=COLORS["brand"], font=("Segoe UI Symbol", 18)).pack(expand=True)
        title_box = tk.Frame(header, bg=COLORS["panel"])
        title_box.pack(side="left")
        tk.Label(title_box, text="简印发票打印", fg=COLORS["text"], bg=COLORS["panel"], font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(title_box, text="PDF / 图片批量排版与打印", fg=COLORS["muted"], bg=COLORS["panel"], font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        badge = tk.Label(header, text="●  本地离线", fg="#15803D", bg="#ECFDF3", font=("Microsoft YaHei UI", 9), padx=12, pady=5)
        badge.pack(side="right", padx=22)
        tk.Frame(self, bg=COLORS["border"], height=1).pack(fill="x")

        body = tk.Frame(self, bg=COLORS["canvas"])
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        self.left = tk.Frame(body, width=270, bg=COLORS["sidebar"])
        self.center = tk.Frame(body, bg=COLORS["canvas"])
        self.right = tk.Frame(body, width=380, bg=COLORS["panel"])
        self.left.grid(row=0, column=0, sticky="nsew")
        tk.Frame(body, width=1, bg=COLORS["border"]).grid(row=0, column=0, sticky="nse")
        self.center.grid(row=0, column=1, sticky="nsew")
        tk.Frame(body, width=1, bg=COLORS["border"]).grid(row=0, column=2, sticky="nsw")
        self.right.grid(row=0, column=2, sticky="nsew")
        self.left.grid_propagate(False)
        self.right.grid_propagate(False)
        self.left.pack_propagate(False)
        self.right.pack_propagate(False)
        self._build_left()
        self._build_center()
        self._build_right()
        task_bar = tk.Frame(self, bg=COLORS["panel"])
        task_bar.pack(fill="x")
        self.progress_var = tk.StringVar(value="就绪")
        tk.Label(task_bar, textvariable=self.progress_var, anchor="w", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=16, pady=7)
        self.cancel_btn = ttk.Button(task_bar, text="取消任务", command=self.cancel_task, state="disabled")
        self.cancel_btn.pack(side="right", padx=12, pady=4)

    def _setup_drag_and_drop(self):
        """允许从资源管理器拖入 PDF、图片或文件夹（含空格和中文路径）。"""
        targets = (self, self.left, self.center, self.thumb_canvas, self.preview_canvas)
        for widget in targets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._drop_leave)
            widget.dnd_bind("<<Drop>>", self._drop_files)

    def _drop_enter(self, event):
        if self.status.get() != "松开鼠标即可添加 PDF 或图片发票":
            self._status_before_drop = self.status.get()
        self.status.set("松开鼠标即可添加 PDF 或图片发票")
        return event.action

    def _drop_leave(self, event):
        if self.status.get() == "松开鼠标即可添加 PDF 或图片发票":
            self.status.set(getattr(self, "_status_before_drop", "就绪"))
        return event.action

    def _drop_files(self, event):
        try:
            paths = list(self.tk.splitlist(event.data))
        except (tk.TclError, TypeError):
            paths = [event.data]
        files = []
        for path in paths:
            if Path(path).is_dir():
                files.extend(str(p) for p in sorted(Path(path).glob("*")) if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)
            elif Path(path).is_file() and Path(path).suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(path)
        if not files:
            self.status.set("拖入的文件中没有支持的 PDF 或图片")
            messagebox.showinfo("拖放文件", "请拖入 PDF 或 PNG、JPG、BMP、TIFF、WebP、GIF 图片，也可拖入文件夹。", parent=self)
            return event.action
        self.add_paths(files)
        return event.action

    def _build_left(self):
        top = tk.Frame(self.left, bg=COLORS["sidebar"])
        top.pack(fill="x", padx=16, pady=(18, 10))
        tk.Label(top, text="发票列表", bg=COLORS["sidebar"], fg=COLORS["text"], font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        tk.Label(top, textvariable=self.count_var, bg=COLORS["brand_soft"], fg=COLORS["brand"], font=("Microsoft YaHei UI", 8, "bold"), padx=8, pady=3).pack(side="right")

        sort_row = tk.Frame(self.left, bg=COLORS["sidebar"])
        sort_row.pack(fill="x", padx=16, pady=(0, 10))
        tk.Label(sort_row, text="排序", bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 8)).pack(side="left")
        self.sort_var = tk.StringVar(value="添加顺序")
        sort = ttk.Combobox(sort_row, textvariable=self.sort_var, values=("添加顺序", "文件名", "金额从高到低", "手动顺序"), state="readonly", width=13)
        sort.pack(side="right")
        sort.bind("<<ComboboxSelected>>", lambda _: self._sort_items())

        options = tk.Frame(self.left, bg=COLORS["sidebar"])
        options.pack(fill="x", padx=16, pady=(0, 4))
        self.compact_var = tk.BooleanVar(value=self._saved_compact)
        ttk.Checkbutton(options, text="紧凑列表", variable=self.compact_var, command=self._list_view_changed).pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        filter_box = ttk.Combobox(options, textvariable=self.filter_var, values=("全部", "金额未识别", "金额待核对"), state="readonly", width=10)
        filter_box.pack(side="right")
        filter_box.bind("<<ComboboxSelected>>", lambda _: self._refresh_thumbnails())
        selection = tk.Frame(self.left, bg=COLORS["sidebar"])
        selection.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Button(selection, text="全选当前", width=0, style="Compact.Tool.TButton", command=lambda: self.select_all(True)).pack(side="left")
        ttk.Button(selection, text="取消当前", width=0, style="Compact.Tool.TButton", command=lambda: self.select_all(False)).pack(side="left")
        self.undo_btn = ttk.Button(selection, text="撤销移除", width=0, style="Compact.Tool.TButton", command=self.undo_remove, state="disabled")
        self.undo_btn.pack(side="right")

        self.thumb_canvas = tk.Canvas(self.left, bg=COLORS["sidebar"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(self.left, orient="vertical", command=self.thumb_canvas.yview)
        self.thumb_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.thumb_canvas.pack(fill="both", expand=True, padx=(10, 0))
        self.thumb_inner = tk.Frame(self.thumb_canvas, bg=COLORS["sidebar"])
        self.thumb_window = self.thumb_canvas.create_window((0, 0), window=self.thumb_inner, anchor="nw")
        self.thumb_inner.bind("<Configure>", lambda _: self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all")))
        self.thumb_canvas.bind("<Configure>", lambda e: self.thumb_canvas.itemconfigure(self.thumb_window, width=e.width))
        self.thumb_canvas.bind("<Enter>", lambda _: setattr(self, "_thumb_hover", True))
        self.thumb_canvas.bind("<Leave>", lambda _: setattr(self, "_thumb_hover", False))
        self.thumb_canvas.bind_all("<MouseWheel>", self._on_thumb_wheel)

        buttons = tk.Frame(self.left, bg=COLORS["sidebar"])
        buttons.pack(fill="x", padx=16, pady=16)
        self.add_btn = ttk.Button(buttons, text="＋  添加 PDF / 图片", style="Primary.TButton", command=self.add_files)
        self.add_btn.pack(fill="x", pady=(0, 8))
        manage = ttk.Frame(buttons)
        manage.pack(fill="x")
        ttk.Button(manage, text="移除已选", style="Secondary.TButton", command=self.remove_selected).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ttk.Button(manage, text="清空列表", style="Secondary.TButton", command=self.clear_items).pack(side="left", expand=True, fill="x")
        self.errors_btn = ttk.Button(buttons, text="查看导入问题", command=self.show_import_errors)
        self.errors_btn.pack(fill="x", pady=(6, 0))
        self.errors_btn.state(["disabled"])
        tk.Label(buttons, text="移除只影响清单，原文件会保留", bg=COLORS["sidebar"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 8)).pack(pady=(8, 0))

    def _build_center(self):
        bar = tk.Frame(self.center, bg=COLORS["canvas"], height=44)
        bar.pack(fill="x", padx=24, pady=(8, 0))
        bar.pack_propagate(False)
        tk.Label(bar, text="打印预览", bg=COLORS["canvas"], fg=COLORS["text"], font=("Microsoft YaHei UI", 12, "bold")).pack(side="left", pady=10)
        self.status_label = tk.Label(self.center, textvariable=self.status, bg="#E2E8F0", fg=COLORS["muted"], font=("Microsoft YaHei UI", 8), padx=10, pady=4, wraplength=300, justify="left", anchor="w")
        self.status_label.pack(fill="x", padx=24, pady=(0, 6))
        self.center.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(120, e.width - 68)))
        tk.Label(self.center, textvariable=self.output_var, bg=COLORS["canvas"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9), justify="left").pack(anchor="w", padx=24, pady=(0, 2))

        preview_wrap = tk.Frame(self.center, bg=COLORS["canvas"])
        preview_wrap.pack(fill="both", expand=True, padx=24)
        self.preview_canvas = tk.Canvas(preview_wrap, bg=COLORS["canvas"], highlightthickness=0, bd=0)
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.bind("<Configure>", lambda _: self._render_preview())
        self.preview_canvas.configure(cursor="fleur")
        self.preview_canvas.bind("<ButtonPress-1>", lambda e: self.preview_canvas.scan_mark(e.x, e.y))
        self.preview_canvas.bind("<B1-Motion>", lambda e: self.preview_canvas.scan_dragto(e.x, e.y, gain=1))
        self.preview_canvas.bind("<MouseWheel>", self._on_preview_wheel)
        hscroll = ttk.Scrollbar(preview_wrap, orient="horizontal", command=self.preview_canvas.xview)
        vscroll = ttk.Scrollbar(preview_wrap, orient="vertical", command=self.preview_canvas.yview)
        self.preview_canvas.pack_forget()
        preview_wrap.rowconfigure(0, weight=1)
        preview_wrap.columnconfigure(0, weight=1)
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        self.preview_canvas.configure(xscrollcommand=hscroll.set, yscrollcommand=vscroll.set)

        view_bar = tk.Frame(self.center, bg=COLORS["canvas"])
        view_bar.pack(fill="x", padx=24, pady=(4, 0))
        ttk.Button(view_bar, text="适合整页", style="Tool.TButton", command=lambda: self.fit_preview("page")).pack(side="left")
        ttk.Button(view_bar, text="适合宽度", style="Tool.TButton", command=lambda: self.fit_preview("width")).pack(side="left")
        self.zoom_var = tk.StringVar(value="整页")
        tk.Label(view_bar, textvariable=self.zoom_var, bg=COLORS["canvas"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 8)).pack(side="right")

        nav = tk.Frame(self.center, bg=COLORS["panel"], height=54, highlightthickness=1, highlightbackground=COLORS["border"])
        nav.pack(fill="x", padx=24, pady=(8, 18))
        nav.pack_propagate(False)
        inner = tk.Frame(nav, bg=COLORS["panel"])
        inner.pack(anchor="center", pady=7)
        ttk.Button(inner, text="|‹", style="Tool.TButton", width=3, command=lambda: self._go_page(0)).pack(side="left")
        ttk.Button(inner, text="‹", style="Tool.TButton", width=3, command=lambda: self._go_page(self.preview_page - 1)).pack(side="left")
        self.page_label = ttk.Label(inner, text="0 / 0", width=9, anchor="center")
        self.page_label.pack(side="left", padx=5)
        ttk.Button(inner, text="›", style="Tool.TButton", width=3, command=lambda: self._go_page(self.preview_page + 1)).pack(side="left")
        ttk.Button(inner, text="›|", style="Tool.TButton", width=3, command=self._go_last).pack(side="left")
        ttk.Separator(inner, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(inner, text="−", style="Tool.TButton", width=3, command=lambda: self._zoom(-0.1)).pack(side="left")
        ttk.Button(inner, text="＋", style="Tool.TButton", width=3, command=lambda: self._zoom(0.1)).pack(side="left")

    def _build_right(self):
        setting_wrap = ttk.Frame(self.right)
        setting_wrap.pack(fill="both", expand=True, padx=18, pady=(14, 6))
        setting_canvas = tk.Canvas(setting_wrap, bg=COLORS["panel"], highlightthickness=0)
        setting_scroll = ttk.Scrollbar(setting_wrap, orient="vertical", command=setting_canvas.yview)
        setting_canvas.configure(yscrollcommand=setting_scroll.set)
        setting_scroll.pack(side="right", fill="y")
        setting_canvas.pack(side="left", fill="both", expand=True)
        pane = ttk.Frame(setting_canvas)
        pane_window = setting_canvas.create_window((0, 0), window=pane, anchor="nw")
        pane.bind("<Configure>", lambda _: setting_canvas.configure(scrollregion=setting_canvas.bbox("all")))
        setting_canvas.bind("<Configure>", lambda e: setting_canvas.itemconfigure(pane_window, width=e.width))
        self.setting_canvas = setting_canvas
        ttk.Label(pane, text="打印设置", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(pane, text="选择设备并调整输出方式", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 18))

        ttk.Label(pane, text="打印机").grid(row=2, column=0, sticky="w", pady=6)
        self.printer_var = tk.StringVar()
        self.printer_box = ttk.Combobox(pane, textvariable=self.printer_var, state="readonly", width=12)
        self.printer_box.grid(row=2, column=1, sticky="ew", padx=(12, 7), pady=6)
        ttk.Button(pane, text="属性", style="Secondary.TButton", width=6, command=self.printer_properties).grid(row=2, column=2, pady=6)
        self.printer_box.bind("<<ComboboxSelected>>", lambda _: self._schedule_save())

        ttk.Label(pane, text="打印方式").grid(row=3, column=0, sticky="w", pady=6)
        simplex = ttk.Frame(pane)
        simplex.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6)
        ttk.Label(simplex, text="单面", style="Muted.TLabel").pack(side="left")
        ttk.Button(simplex, text="刷新", style="Tool.TButton", command=self._load_printers).pack(side="right")
        ttk.Button(simplex, text="打印队列", style="Tool.TButton", command=self.open_print_queue).pack(side="right")

        self.copies_var = tk.StringVar(value="1")
        copies = ttk.Spinbox(pane, from_=1, to=99, textvariable=self.copies_var, width=12, command=self._settings_changed)
        copies.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6)
        copies.bind("<KeyRelease>", lambda _: self._settings_changed())
        copies.bind("<FocusOut>", lambda _: self._validate_copies())
        copies.bind("<Return>", lambda _: self._validate_copies())
        self.copies_error = tk.StringVar()

        ttk.Label(pane, text="每张份数").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Label(pane, text="纸张").grid(row=5, column=0, sticky="w", pady=6)
        self.paper_var = tk.StringVar(value=self.settings.paper)
        paper = ttk.Combobox(pane, textvariable=self.paper_var, values=tuple(PAPER_SIZES), state="readonly")
        paper.grid(row=5, column=1, sticky="ew", padx=(12, 7), pady=6)
        paper.bind("<<ComboboxSelected>>", lambda _: self._settings_changed())
        self.margin_var = tk.StringVar(value=f"边距 {self.settings.margin_mm:g}mm")
        ttk.Button(pane, textvariable=self.margin_var, style="Secondary.TButton", width=9, command=self.change_margin).grid(row=5, column=2, pady=6)

        ttk.Separator(pane).grid(row=6, column=0, columnspan=3, sticky="ew", pady=16)
        ttk.Label(pane, text="打印排版", style="Section.TLabel").grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(pane, text="方向").grid(row=8, column=0, sticky="w", pady=6)
        self.orientation_var = tk.StringVar(value="landscape" if self.settings.landscape else "portrait")
        orient = ttk.Frame(pane)
        orient.grid(row=8, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=6)
        ttk.Radiobutton(orient, text="纵向", variable=self.orientation_var, value="portrait", command=self._settings_changed).pack(side="left")
        ttk.Radiobutton(orient, text="横向", variable=self.orientation_var, value="landscape", command=self._settings_changed).pack(side="left", padx=16)

        ttk.Label(pane, text="版式").grid(row=9, column=0, sticky="nw", pady=9)
        self.layout_var = tk.StringVar(value=self.settings.layout)
        layout = ttk.Frame(pane)
        layout.grid(row=9, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=5)
        for value, title, glyph in (("single", "每页 1 张", "▭"), ("vertical", "上下 2 张", "▭\n▭"), ("horizontal", "左右 2 张", "▯ ▯")):
            card = tk.Radiobutton(layout, text=f"{glyph}\n{title}", variable=self.layout_var, value=value,
                                  command=self._settings_changed, indicatoron=False, relief="solid", bd=1,
                                  selectcolor=COLORS["brand_soft"], bg="#ffffff", activebackground="#F3F6FB",
                                  font=("Microsoft YaHei UI", 8), padx=7, pady=8, cursor="hand2")
            card.pack(side="left", fill="both", expand=True, padx=(0, 5))

        self.layout_hint = tk.StringVar()
        ttk.Label(pane, textvariable=self.layout_hint, style="Muted.TLabel", wraplength=280).grid(
            row=10, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=(0, 4))

        self.crop_var = tk.BooleanVar(value=self.settings.crop_marks)
        ttk.Checkbutton(pane, text="添加裁剪线", variable=self.crop_var, command=self._settings_changed).grid(row=11, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=10)

        ttk.Label(pane, text="分页").grid(row=12, column=0, sticky="w", pady=6)
        ttk.Label(pane, text="按发票顺序自动分页", style="Muted.TLabel").grid(row=12, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=6)
        ttk.Label(pane, textvariable=self.copies_error, foreground=COLORS["danger"], wraplength=290).grid(row=13, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(pane, text="每张发票连续排入所选份数，例如两张各 2 份：A、A、B、B。", style="Muted.TLabel", wraplength=300).grid(row=14, column=0, columnspan=3, sticky="w", pady=4)
        pane.columnconfigure(1, weight=1)

        bottom = ttk.Frame(self.right)
        bottom.pack(fill="x", padx=24, pady=(0, 20))
        ttk.Separator(bottom).pack(fill="x", pady=(0, 12))
        self.summary_var = tk.StringVar(value="共 0 张发票，发票金额合计：—")
        ttk.Label(bottom, textvariable=self.summary_var, wraplength=330).pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(bottom)
        actions.pack(fill="x")
        self.export_btn = ttk.Button(actions, text="导出 PDF", style="Secondary.TButton", command=self.export_pdf)
        self.export_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.print_btn = ttk.Button(actions, text="开始打印", style="Primary.TButton", command=self.print_document)
        self.print_btn.pack(side="left", fill="x", expand=True)

    def _submit(self, kind, work, done):
        self._cancel_kind(kind)
        self._job_serial += 1
        token, cancel = self._job_serial, threading.Event()
        self._jobs[kind] = (token, cancel, done)

        def run():
            last_progress = [0.0]
            def progress(message):
                now = time.monotonic()
                if now - last_progress[0] >= 0.08:
                    last_progress[0] = now
                    self._events.put(("progress", kind, token, message, None))
            try:
                check_cancel(cancel)
                result = work(cancel, progress)
                self._events.put(("done", kind, token, result, None))
            except Exception as exc:
                self._events.put(("done", kind, token, None, exc))
        self._executor.submit(run)
        self._refresh_action_states()

    def _cancel_kind(self, kind):
        job = self._jobs.pop(kind, None)
        if job:
            job[1].set()

    def _poll_tasks(self):
        if self._closed:
            return
        for _ in range(100):
            try:
                event, kind, token, result, error = self._events.get_nowait()
            except queue.Empty:
                break
            current = self._jobs.get(kind)
            if current is None or current[0] != token:
                continue
            if event == "progress":
                if kind in ("import", "export", "print"):
                    self.progress_var.set(result)
            else:
                del self._jobs[kind]
                if error:
                    if isinstance(error, TaskCancelled):
                        if kind in ("import", "export", "print"):
                            self.status.set("任务已取消；已进入打印队列的任务请在队列中检查。" if kind == "print" else "任务已取消")
                    else:
                        self.status.set({"import": "导入失败", "preview": "预览失败", "render": "预览显示失败", "export": "导出失败", "print": "打印提交失败"}.get(kind, "处理失败"))
                        if kind in ("preview", "render"):
                            self.preview_data = None
                            self.preview_count = 0
                            self.preview_canvas.delete("all")
                            self._draw_empty("预览失败，请检查源文件是否仍可访问。")
                        messagebox.showerror("处理失败", str(error), parent=self)
                else:
                    current[2](result)
                if self._closed:
                    return
                self._refresh_action_states()
        if not self._closed:
            self._poll_job = self.after(50, self._poll_tasks)

    def _refresh_action_states(self):
        busy = any(k in self._jobs for k in ("import", "export", "print"))
        valid = self._copies_value() is not None and any(x.selected for x in self.items)
        self.export_btn.state(["!disabled"] if valid and not busy else ["disabled"])
        self.print_btn.state(["!disabled"] if valid and not busy and self.printer_var.get() and win32ui else ["disabled"])
        self.cancel_btn.state(["!disabled"] if busy else ["disabled"])
        self.add_btn.state(["disabled"] if busy else ["!disabled"])
        self.undo_btn.state(["!disabled"] if self._undo_stack else ["disabled"])
        if not busy:
            self.progress_var.set("就绪 · 仅输出已勾选发票")

    def cancel_task(self):
        for kind in ("print", "export", "import"):
            if kind in self._jobs:
                self._jobs[kind][1].set()
                self._pending_paths.clear()
                self.progress_var.set("正在取消，请稍候…")
                self.cancel_btn.state(["disabled"])
                return

    def _load_printers(self):
        previous = self.printer_var.get() or self._saved_printer
        names, default = [], ""
        if win32print:
            try:
                flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
                names = sorted({p[2] for p in win32print.EnumPrinters(flags)})
                try:
                    default = win32print.GetDefaultPrinter()
                except Exception:
                    pass
            except Exception as exc:
                self.status.set(f"无法刷新打印机：{exc}")
        self.printer_box["values"] = names
        self.printer_var.set(previous if previous in names else default if default in names else names[0] if names else "")
        if not names:
            self.status.set("未找到打印机，仍可导出 PDF")
        if hasattr(self, "cancel_btn"):
            self._refresh_action_states()

    def open_print_queue(self):
        name = self.printer_var.get()
        if not name:
            messagebox.showinfo("打印队列", "请先选择打印机。", parent=self)
            return
        try:
            subprocess.Popen(["rundll32.exe", "printui.dll,PrintUIEntry", "/o", "/n", name])
        except OSError as exc:
            messagebox.showerror("打印队列", str(exc), parent=self)

    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择 PDF 或图片发票", filetypes=IMPORT_FILETYPES)
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths):
        paths = tuple(str(p) for p in paths)
        if not paths:
            return
        if "import" in self._jobs:
            self._pending_paths.extend(paths)
            self.status.set("文件已加入待导入队列")
            return
        if "print" in self._jobs or "export" in self._jobs:
            self.status.set("请等待当前输出任务完成后再导入")
            return
        existing = {(canonical_path(x.path), x.page_no) for x in self.items}
        self.status.set("正在读取发票…")
        self._submit("import", lambda cancel, progress: read_invoice_paths(paths, existing, cancel, progress), self._import_done)

    def _import_done(self, result):
        items, errors, duplicates, cancelled = result
        existing = {(canonical_path(x.path), x.page_no) for x in self.items}
        added = 0
        for item in items:
            key = canonical_path(item.path), item.page_no
            if key not in existing:
                item.import_order = self._next_order
                self._next_order += 1
                self.items.append(item)
                existing.add(key)
                added += 1
        self.import_errors = errors
        self.errors_btn.configure(text=f"查看导入问题（{len(errors)}）" if errors else "查看导入问题")
        self.errors_btn.state(["!disabled"] if errors else ["disabled"])
        self._sort_items()
        detail = f"{'导入已取消，保留' if cancelled else '已添加'} {added} 张，跳过重复 {duplicates} 张"
        if errors:
            detail += f"，{len(errors)} 个文件有问题（见左下角）"
        self.status.set(detail)
        if self._pending_paths and not cancelled:
            paths, self._pending_paths = self._pending_paths, []
            self.add_paths(paths)

    def show_import_errors(self):
        dialog = tk.Toplevel(self)
        dialog.title("导入问题 · 全部记录")
        dialog.geometry("640x380")
        dialog.transient(self)
        text = tk.Text(dialog, wrap="word", font=("Microsoft YaHei UI", 10))
        scroll = ttk.Scrollbar(dialog, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("1.0", "\n\n".join(self.import_errors) or "本批次没有导入问题。")
        text.configure(state="disabled")

    def _sort_items(self):
        for item in self.items:
            if item.import_order < 0:
                item.import_order = self._next_order
                self._next_order += 1
        mode = self.sort_var.get()
        if mode == "文件名":
            self.items.sort(key=lambda x: (Path(x.path).name.casefold(), x.page_no, x.import_order))
        elif mode == "金额从高到低":
            self.items.sort(key=lambda x: (x.amount is None, -(x.amount or 0), x.import_order))
        elif mode == "添加顺序":
            self.items.sort(key=lambda x: x.import_order)
        self._refresh_thumbnails()
        self._update_summary()
        self._schedule_preview()

    def _list_view_changed(self):
        self._refresh_thumbnails()
        self._schedule_save()

    def _visible_items(self):
        mode = self.filter_var.get()
        return [(i, x) for i, x in enumerate(self.items) if mode == "全部" or
                (mode == "金额未识别" and x.amount is None) or
                (mode == "金额待核对" and x.amount is not None and not x.amount_verified)]

    def _refresh_thumbnails(self, reset_scroll=True):
        first = self.thumb_canvas.yview()[0]
        if self._thumb_reset_job:
            self.after_cancel(self._thumb_reset_job)
            self._thumb_reset_job = None
        for child in self.thumb_inner.winfo_children():
            child.destroy()
        self.thumb_images.clear()
        self._selection_vars = []
        visible = self._visible_items()
        if not visible:
            empty = "暂无发票\n拖入 PDF、图片或文件夹，或点击下方添加" if not self.items else "没有符合筛选条件的发票\n可切回“全部”查看"
            tk.Label(self.thumb_inner, text=empty, bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 9), wraplength=210).pack(padx=12, pady=30)
        for index, item in visible:
            card = tk.Frame(self.thumb_inner, bg="white", highlightthickness=1,
                            highlightbackground=COLORS["brand"] if item.import_order == self._highlight_order else COLORS["border"])
            card.pack(fill="x", padx=7, pady=4)
            card._invoice_order = item.import_order
            top = tk.Frame(card, bg="white")
            top.pack(fill="x", padx=5, pady=4)
            selected = tk.BooleanVar(value=item.selected)
            self._selection_vars.append(selected)
            ttk.Checkbutton(top, text=f"发票 {index + 1}", variable=selected,
                            command=lambda i=index, v=selected: self.set_selected(i, v.get())).pack(side="left")
            ttk.Button(top, text="移除", style="Tool.TButton", command=lambda i=index: self.remove_item(i)).pack(side="right")
            if not self.compact_var.get():
                image = ImageTk.PhotoImage(Image.open(io.BytesIO(item.thumb_png)))
                self.thumb_images.append(image)
                thumbnail = tk.Label(card, image=image, bg="#F8FAFC", cursor="hand2")
                thumbnail.pack(pady=(0, 4))
                self._bind_card_drag(thumbnail, item.import_order)
            label = tk.Label(card, text=item.label, bg="white", fg=COLORS["text"], font=("Microsoft YaHei UI", 8), wraplength=214, cursor="hand2")
            label.pack(padx=6)
            self._bind_card_drag(label, item.import_order)
            amount = "金额未识别 · 点击填写" if item.amount is None else f"¥ {item.amount:,.2f} · {'已核对' if item.amount_verified else '待核对'}"
            tk.Button(card, text=amount, command=lambda i=index: self.edit_amount(i), relief="flat", bg="white",
                      fg=COLORS["muted"] if item.amount is None else "#0F766E", font=("Microsoft YaHei UI", 8), cursor="hand2").pack(pady=(1, 4))
        self._thumb_reset_job = self.after_idle(lambda: self._restore_thumb_scroll(0 if reset_scroll else first))

    def _bind_card_drag(self, widget, order):
        widget.bind("<ButtonPress-1>", lambda e: setattr(self, "_card_drag", (order, e.x_root, e.y_root)))
        widget.bind("<ButtonRelease-1>", self._finish_card_drag)

    def _finish_card_drag(self, event):
        drag = getattr(self, "_card_drag", None)
        self._card_drag = None
        if drag is None:
            return
        source, x, y = drag
        if abs(event.x_root - x) + abs(event.y_root - y) < 8:
            index = next((i for i, item in enumerate(self.items) if item.import_order == source), None)
            if index is not None:
                self._jump_to_item(index)
            return
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None and not hasattr(widget, "_invoice_order"):
            widget = getattr(widget, "master", None)
        if widget is not None:
            after = event.y_root > widget.winfo_rooty() + widget.winfo_height() / 2
            self.move_item(source, widget._invoice_order, after)

    def move_item(self, source_order, target_order, after=False):
        if source_order == target_order:
            return
        source = next((x for x in self.items if x.import_order == source_order), None)
        target = next((x for x in self.items if x.import_order == target_order), None)
        if source is None or target is None:
            return
        self.items.remove(source)
        self.items.insert(self.items.index(target) + int(after), source)
        self.sort_var.set("手动顺序")
        self._sort_items()
        self.status.set("已调整打印顺序；选择“添加顺序”可恢复导入顺序")

    def _restore_thumb_scroll(self, fraction):
        self._thumb_reset_job = None
        self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all"))
        self.thumb_canvas.yview_moveto(fraction)

    def set_selected(self, index, selected):
        if 0 <= index < len(self.items):
            self.items[index].selected = bool(selected)
            self._update_summary()
            self._schedule_preview()

    def select_all(self, selected):
        for _, item in self._visible_items():
            item.selected = selected
        self._refresh_thumbnails(reset_scroll=False)
        self._update_summary()
        self._schedule_preview()
        self.status.set("已更新当前筛选结果的勾选状态")

    def edit_amount(self, index):
        if not 0 <= index < len(self.items):
            return
        item = self.items[index]
        initial = "" if item.amount is None else f"{item.amount:.2f}"
        while True:
            raw = simpledialog.askstring("核对发票金额", f"{item.label}\n请输入原票价税合计；确认后标记为已核对。\n留空表示未识别。仅修改清单金额。", initialvalue=initial, parent=self)
            if raw is None:
                return
            try:
                current = next((i for i, x in enumerate(self.items) if x.import_order == item.import_order), None)
                if current is None:
                    self.status.set("该发票已移除，请恢复后再核对金额")
                    return
                self.set_amount(current, raw)
                return
            except ValueError as exc:
                messagebox.showwarning("金额格式", str(exc), parent=self)
                initial = raw

    def set_amount(self, index, raw):
        value = parse_amount(raw)
        self.items[index].amount = value
        self.items[index].amount_verified = value is not None
        self._sort_items()
        self.status.set("清单金额已更新，原文件内容保留")

    def _remember_removal(self, indices):
        removed = [(i, self.items[i]) for i in sorted(set(indices)) if 0 <= i < len(self.items)]
        if not removed:
            return
        self._undo_stack.append(removed)
        self._undo_stack = self._undo_stack[-20:]
        for i, _ in reversed(removed):
            self.items.pop(i)
        self.preview_page = min(self.preview_page, max(0, self._preview_page_count() - 1))
        self._refresh_thumbnails(reset_scroll=False)
        self._update_summary()
        self._schedule_preview()
        self.status.set(f"已移除 {len(removed)} 张，可点击“撤销移除”恢复")

    def remove_item(self, item_index):
        self._remember_removal([item_index])

    def remove_selected(self):
        self._remember_removal([i for i, x in enumerate(self.items) if x.selected])

    def clear_items(self):
        self._remember_removal(range(len(self.items)))

    def undo_remove(self):
        if not self._undo_stack:
            return
        removed = self._undo_stack.pop()
        existing = {(canonical_path(x.path), x.page_no) for x in self.items}
        for position, item in removed:
            key = canonical_path(item.path), item.page_no
            if key not in existing:
                self.items.insert(min(position, len(self.items)), item)
                existing.add(key)
        self._sort_items()
        self.status.set("已恢复上一次移除的发票")

    def _selected_items(self):
        return [x for x in self.items if x.selected]

    def _copies_value(self):
        raw = str(self.copies_var.get()).strip()
        return int(raw) if re.fullmatch(r"[0-9]{1,2}", raw) and 1 <= int(raw) <= 99 else None

    def _validate_copies(self):
        value = self._copies_value()
        if value is None:
            self.copies_error.set("每张份数请输入 1～99 的整数，修正后才能输出。")
        else:
            self.copies_var.set(str(value))
            self.copies_error.set("")
            if value != self.settings.copies:
                self._sync_settings()
                self._schedule_preview()
        self._refresh_action_states()
        return value is not None

    def _preview_page_count(self):
        count = self._copies_value()
        per_page = 1 if self.layout_var.get() == "single" else 2
        return (len(self._selected_items()) * count + per_page - 1) // per_page if count else 0

    def _update_summary(self):
        selected = self._selected_items()
        amounts = [Decimal(str(x.amount)) for x in selected if x.amount is not None]
        total = sum(amounts, Decimal("0"))
        unknown = len(selected) - len(amounts)
        pending = sum(x.amount is not None and not x.amount_verified for x in selected)
        self.summary_var.set(f"已选 {len(selected)} / 共 {len(self.items)} 张\n已识别金额合计：{'¥ ' + format(total, ',.2f') if amounts else '—'}\n未识别 {unknown} 张 · 待核对 {pending} 张")
        self.count_var.set(f"{len(selected)}/{len(self.items)} 张")
        self._sync_settings()
        self._refresh_action_states()

    def _sync_settings(self):
        self.settings.paper = self.paper_var.get()
        self.settings.landscape = self.orientation_var.get() == "landscape"
        self.settings.layout = self.layout_var.get()
        self.settings.crop_marks = self.crop_var.get()
        count = self._copies_value()
        if count is not None:
            self.settings.copies = count
        self.copies_error.set("" if count is not None else "每张份数请输入 1～99 的整数，修正后才能输出。")
        per_page = len(layout_slots(self.settings))
        selected = len(self._selected_items())
        pages = (selected * (count or 0) + per_page - 1) // per_page
        layout = {"single": "每页 1 张", "vertical": "上下 2 张", "horizontal": "左右 2 张"}[self.settings.layout]
        orientation = "横向" if self.settings.landscape else "纵向"
        detail = f"{selected} 张 × {count} 份，共 {selected * count} 张 · 使用 {pages} 页纸" if count else "份数无效，请修正"
        self.output_var.set(f"{self.settings.paper} · {orientation} · {layout}\n{detail}")
        self.layout_hint.set("左右排版的单张发票会较窄，请核对预览。" if self.settings.layout == "horizontal" else
                             "上下排版通常适合横版发票。" if self.settings.layout == "vertical" else "每页放置一张发票。")

    def _settings_changed(self):
        self._sync_settings()
        self._refresh_action_states()
        self._schedule_preview()
        self._schedule_save()

    def change_margin(self):
        dialog = MarginDialog(self, self.settings.margin_mm)
        self.wait_window(dialog)
        if dialog.result is not None:
            self.settings.margin_mm = dialog.result
            self.margin_var.set(f"边距 {dialog.result:g}mm")
            self._schedule_preview()
            self._schedule_save()

    def _schedule_save(self):
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(400, self._save_preferences)

    def _save_preferences(self):
        self._save_job = None
        values = {k: getattr(self.settings, k) for k in ("paper", "landscape", "layout", "crop_marks", "margin_mm")}
        values.update(printer=self.printer_var.get(), compact=self.compact_var.get())
        try:
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
            write_export(self.preferences_path, json.dumps(values, ensure_ascii=False, indent=2).encode("utf-8"), [])
        except OSError:
            self.status.set("本次设置无法保存，请检查设置目录权限")

    def _schedule_preview(self):
        self._cancel_kind("preview")
        self._cancel_kind("render")
        if self._preview_job:
            self.after_cancel(self._preview_job)
        if self._render_job:
            self.after_cancel(self._render_job)
            self._render_job = None
        self._preview_dirty = True
        self.zoom_var.set("预览更新中…")
        self._preview_job = self.after(130, self._rebuild_preview)

    def _draw_empty(self, message="将 PDF、图片或文件夹拖到这里\n也可以点击左下角添加 PDF / 图片"):
        self.preview_canvas.delete("all")
        width, height = self.preview_canvas.winfo_width(), self.preview_canvas.winfo_height()
        self.preview_canvas.configure(scrollregion=(0, 0, width, height))
        self.preview_canvas.create_text(width / 2, height / 2, text=message, fill=COLORS["muted"], font=("Microsoft YaHei UI", 10), width=max(120, width - 40), justify="center")
        self.page_label.configure(text="0 / 0")
        self.zoom_var.set("整页")

    def _rebuild_preview(self):
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = None
        self._sync_settings()
        items = [replace(x) for x in self._selected_items()]
        if not items or self._copies_value() is None:
            self.preview_data = None
            self.preview_count = 0
            self._preview_dirty = False
            self._draw_empty("请勾选至少一张发票" if self.items and not items else "请修正每张份数" if items else "将 PDF、图片或文件夹拖到这里\n也可以点击左下角添加 PDF / 图片")
            return
        settings = replace(self.settings)
        def build(cancel, progress):
            return compose_pdf(items, settings, cancel=cancel), settings, [x.import_order for x in items]
        self._submit("preview", build, self._preview_ready)

    def _preview_ready(self, result):
        self.preview_data, self._preview_settings, self._preview_orders = result
        self.preview_count = (len(self._preview_orders) * self._preview_settings.copies + len(layout_slots(self._preview_settings)) - 1) // len(layout_slots(self._preview_settings))
        self.preview_page = max(0, min(self.preview_page, self.preview_count - 1))
        self._preview_dirty = False
        self._reset_preview_view = True
        self._render_preview()

    def _render_preview(self):
        if self._closed:
            return
        if not self.preview_data or getattr(self, "_preview_dirty", False):
            if not self.items:
                self._draw_empty()
            return
        self._cancel_kind("render")
        if self._render_job:
            self.after_cancel(self._render_job)
        self._render_job = self.after(65, self._request_render)

    def _request_render(self):
        self._render_job = None
        if not self.preview_data or self._preview_dirty:
            return
        size = (max(120, self.preview_canvas.winfo_width() - 32), max(120, self.preview_canvas.winfo_height() - 32))
        highlight = None
        if self._highlight_order in self._preview_orders:
            highlight = self._preview_orders.index(self._highlight_order) * self._preview_settings.copies
        data, page, mode, zoom, settings = self.preview_data, self.preview_page, self.zoom_mode, self.zoom, replace(self._preview_settings)
        self._submit("render", lambda cancel, progress: render_pdf_preview(data, page, size, mode, zoom, highlight, settings, cancel), self._show_preview)

    def _show_preview(self, result):
        png, self.preview_count, self.preview_page, scale, highlight = result
        image = Image.open(io.BytesIO(png))
        self.preview_image = ImageTk.PhotoImage(image)
        canvas = self.preview_canvas
        old_x, old_y = canvas.xview()[0], canvas.yview()[0]
        width, height = max(canvas.winfo_width(), image.width + 32), max(canvas.winfo_height(), image.height + 32)
        left, top = (width - image.width) / 2, (height - image.height) / 2
        canvas.delete("all")
        canvas.configure(scrollregion=(0, 0, width, height))
        canvas.create_rectangle(left + 5, top + 5, left + image.width + 5, top + image.height + 5, fill="#d8dbe0", outline="")
        canvas.create_image(left, top, anchor="nw", image=self.preview_image)
        if highlight:
            x0, y0, x1, y1 = highlight
            canvas.create_rectangle(left + x0, top + y0, left + x1, top + y1, outline=COLORS["brand"], width=2, dash=(5, 3))
        reset = getattr(self, "_reset_preview_view", False)
        canvas.xview_moveto(0 if reset else old_x)
        canvas.yview_moveto(0 if reset else old_y)
        self._reset_preview_view = False
        self.page_label.configure(text=f"{self.preview_page + 1} / {self.preview_count}")
        self.zoom_var.set(f"{'整页' if self.zoom_mode == 'page' else '宽度'} · {scale * 100:.0f}% · 可拖动查看")

    def _go_page(self, page):
        if self.preview_count:
            self.preview_page = max(0, min(page, self.preview_count - 1))
            self._reset_preview_view = True
            self._render_preview()

    def _go_last(self):
        self._go_page(self.preview_count - 1)

    def _jump_to_item(self, item_index):
        if not 0 <= item_index < len(self.items):
            return
        item = self.items[item_index]
        if not item.selected:
            self.status.set("该发票未勾选，请先勾选后查看输出预览")
            return
        self._highlight_order = item.import_order
        selected = self._selected_items()
        self.preview_page = selected.index(item) * (self._copies_value() or 1) // (1 if self.layout_var.get() == "single" else 2)
        self._refresh_thumbnails(reset_scroll=False)
        self._reset_preview_view = True
        self._render_preview()

    def fit_preview(self, mode):
        self.zoom_mode, self.zoom = mode, 1.0
        self._reset_preview_view = True
        self._render_preview()

    def _zoom(self, delta):
        self.zoom = max(0.4, min(4.0, round(self.zoom + delta, 2)))
        self._render_preview()

    def _on_preview_wheel(self, event):
        if event.state & 4:
            self._zoom(0.1 if event.delta > 0 else -0.1)
        else:
            self.preview_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _on_thumb_wheel(self, event):
        widget = getattr(event, "widget", None) or self.winfo_containing(event.x_root, event.y_root)
        ancestors = []
        while widget is not None:
            ancestors.append(widget)
            widget = getattr(widget, "master", None)
        canvas = self.thumb_canvas if self.thumb_canvas in ancestors or self.thumb_inner in ancestors else self.setting_canvas if self.setting_canvas in ancestors else None
        if canvas is None:
            return None
        first, last = canvas.yview()
        if event.delta > 0:
            if first <= 0.0001:
                canvas.yview_moveto(0)
            else:
                canvas.yview_scroll(-1, "units")
        elif event.delta < 0:
            if last >= 0.9999:
                canvas.yview_moveto(max(0, 1 - (last - first)))
            else:
                canvas.yview_scroll(1, "units")
        return "break"

    def _output_snapshot(self):
        if any(k in self._jobs for k in ("import", "export", "print")):
            self.status.set("请等待当前导入或输出任务完成")
            return None
        if not self._validate_copies():
            return None
        items = [replace(x) for x in self._selected_items()]
        if not items:
            messagebox.showinfo("输出发票", "请先添加并勾选至少一张发票。", parent=self)
            return None
        self._sync_settings()
        self._schedule_save()
        return items, replace(self.settings)

    def export_pdf(self):
        snapshot = self._output_snapshot()
        if snapshot is None:
            return
        path = filedialog.asksaveasfilename(title="导出排版后的 PDF", defaultextension=".pdf",
                    initialfile=f"发票排版_{datetime.now():%Y%m%d_%H%M%S}.pdf", filetypes=[("PDF 文件", "*.pdf")])
        if path:
            self.export_to_path(path, snapshot)

    def export_to_path(self, path, snapshot=None):
        snapshot = snapshot or self._output_snapshot()
        if snapshot is None:
            return False
        sources = tuple(x.path for x in self.items)
        if canonical_path(path) in {canonical_path(p) for p in sources}:
            messagebox.showwarning("保护原始发票", "不能覆盖已导入的原始文件，请选择其他文件名。", parent=self)
            return False
        items, settings = snapshot
        def export(cancel, progress):
            data = compose_pdf(items, settings, cancel=cancel, progress=progress)
            write_export(path, data, sources, cancel)
            return str(path)
        self.status.set("正在导出 PDF…")
        self._submit("export", export, self._export_done)
        return True

    def _export_done(self, path):
        self.status.set(f"已导出：{Path(path).name}")
        if messagebox.askyesno("导出完成", f"已保存到：\n{path}\n\n是否立即打开？", parent=self):
            try:
                os.startfile(path)
            except OSError as exc:
                messagebox.showerror("打开 PDF", f"文件已保存，但无法打开：\n{exc}", parent=self)

    def printer_properties(self):
        name = self.printer_var.get()
        if name:
            try:
                subprocess.Popen(["rundll32.exe", "printui.dll,PrintUIEntry", "/e", "/n", name])
            except OSError as exc:
                messagebox.showerror("打印机属性", str(exc), parent=self)

    def print_document(self):
        snapshot = self._output_snapshot()
        if snapshot is None:
            return
        printer = self.printer_var.get()
        if not printer or not win32ui:
            messagebox.showerror("开始打印", "未找到可用的 Windows 打印机，请刷新打印机列表。", parent=self)
            return
        items, settings = snapshot
        pages = (len(items) * settings.copies + len(layout_slots(settings)) - 1) // len(layout_slots(settings))
        direction = "横向" if settings.landscape else "纵向"
        if not messagebox.askokcancel("确认打印", f"打印机：{printer}\n纸张：{settings.paper} · {direction} · 单面\n已选发票：{len(items)} 张\n每张 {settings.copies} 份，共 {len(items) * settings.copies} 张，使用 {pages} 页纸\n\n确认提交打印任务？", parent=self):
            return
        def output(cancel, progress):
            data = compose_pdf(items, settings, cancel=cancel, progress=progress)
            check_cancel(cancel)
            print_pdf_bytes(data, printer, settings, cancel=cancel, progress=progress)
        self.status.set("正在生成并提交打印任务…")
        self._submit("print", output, lambda _: self._print_done(None))

    def _print_done(self, error):
        if error:
            self.status.set("打印提交失败")
            messagebox.showerror("打印提交失败", str(error), parent=self)
        else:
            self.status.set("已提交打印任务，可通过“打印队列”检查状态")
            messagebox.showinfo("已提交打印任务", "任务已提交到 Windows 打印队列。\n实际出纸状态请通过“打印队列”或打印机检查。", parent=self)

    def _on_close(self):
        busy = any(k in self._jobs for k in ("import", "export", "print"))
        if busy and not messagebox.askokcancel("任务仍在处理", "退出将取消尚未完成的导入、导出或打印提交。\n已进入打印队列的任务请在打印队列中检查。\n\n确定退出？", parent=self):
            return
        self._save_preferences()
        self.destroy()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        for _, cancel, _ in self._jobs.values():
            cancel.set()
        self._jobs.clear()
        for name in ("_preview_job", "_render_job", "_thumb_reset_job", "_save_job", "_poll_job"):
            job = getattr(self, name, None)
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, name, None)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.unbind_all("<MouseWheel>")
        super().destroy()


def printer_devmode(printer_name: str, settings: Settings):
    """为单个任务设置纸型、方向、单面和一份；不修改打印机默认值。"""
    handle = win32print.OpenPrinter(printer_name)
    try:
        devmode = win32print.GetPrinter(handle, 2).get("pDevMode")
        if devmode is None:
            raise RuntimeError("打印机驱动未提供纸张设置，无法保证打印方向。")
        devmode.Fields |= (win32con.DM_ORIENTATION | win32con.DM_PAPERSIZE |
                           win32con.DM_DUPLEX | win32con.DM_COPIES)
        devmode.Orientation = (win32con.DMORIENT_LANDSCAPE if settings.landscape
                               else win32con.DMORIENT_PORTRAIT)
        devmode.PaperSize = PAPER_CODES[settings.paper]
        devmode.Duplex = win32con.DMDUP_SIMPLEX
        devmode.Copies = 1  # PDF 已按每张发票的份数排好，驱动不可再复制。
        result = win32print.DocumentProperties(
            0, handle, printer_name, devmode, devmode,
            win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER)
        if result <= 0:
            raise RuntimeError("打印机驱动未接受所选纸张与方向。")
        if devmode.PaperSize != PAPER_CODES[settings.paper] or devmode.Orientation != (
            win32con.DMORIENT_LANDSCAPE if settings.landscape else win32con.DMORIENT_PORTRAIT
        ):
            raise RuntimeError("打印机驱动不支持所选纸张或方向，请检查打印机属性。")
        if devmode.Duplex != win32con.DMDUP_SIMPLEX or devmode.Copies != 1:
            raise RuntimeError("打印机驱动未接受单面或单份设置，请检查打印机属性。")
        return devmode
    finally:
        win32print.ClosePrinter(handle)


def print_pdf_bytes(data: bytes, printer_name: str, settings: Settings, cancel=None, progress=None) -> None:
    """将排版后的每一页渲染到所选 Windows 打印机。"""
    doc = fitz.open(stream=data, filetype="pdf")
    dc = None
    try:
        check_cancel(cancel)
        devmode = printer_devmode(printer_name, settings)
        dc = win32ui.CreateDCFromHandle(win32gui.CreateDC("WINSPOOL", printer_name, devmode))
        physical_w = dc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        physical_h = dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        offset_x = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        offset_y = dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
        if (physical_w > physical_h) != settings.landscape:
            raise RuntimeError("打印机驱动没有切换到所选方向，请检查打印机属性。")
        dc.StartDoc(APP_NAME)
        for index, page in enumerate(doc):
            check_cancel(cancel)
            if progress:
                progress(f"正在提交打印：{index + 1} / {doc.page_count} 页")
            dc.StartPage()
            target_scale = min(physical_w / page.rect.width, physical_h / page.rect.height)
            # 以最高 300 DPI 渲染，随后由 GDI 映射到打印设备，避免高 DPI 打印机占用过多内存。
            render_scale = min(target_scale, 300 / 72)
            pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            dib = ImageWin.Dib(image)
            target_w = int(page.rect.width * target_scale)
            target_h = int(page.rect.height * target_scale)
            left = (physical_w - target_w) // 2 - offset_x
            top = (physical_h - target_h) // 2 - offset_y
            dib.draw(dc.GetHandleOutput(), (left, top, left + target_w, top + target_h))
            dc.EndPage()
        check_cancel(cancel)
        dc.EndDoc()
    except Exception:
        try:
            dc.AbortDoc()
        except Exception:
            pass
        raise
    finally:
        doc.close()
        if dc is not None:
            dc.DeleteDC()


if __name__ == "__main__":
    app = InvoicePrinterApp()
    app.mainloop()
