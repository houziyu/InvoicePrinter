from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import pymupdf as fitz
from PIL import Image, ImageTk, ImageWin
from tkinterdnd2 import DND_FILES, TkinterDnD

try:
    import win32con
    import win32gui
    import win32print
    import win32ui
except ImportError:  # 便于在非 Windows 环境中预览界面
    win32con = win32gui = win32print = win32ui = None


APP_NAME = "简印 · PDF 发票打印"
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


@dataclass
class Settings:
    paper: str = "A4"
    landscape: bool = False
    layout: str = "single"  # single / vertical / horizontal
    copies: int = 1
    crop_marks: bool = False
    margin_mm: float = 8.0


def extract_amount(page: fitz.Page) -> float | None:
    """尽量从价税合计附近提取金额；失败时取带两位小数的合理最大值。"""
    text = page.get_text("text") or ""
    compact = re.sub(r"\s+", "", text)
    patterns = [
        r"价税合计[^¥￥\d]{0,20}[¥￥]?([0-9,]+\.\d{2})",
        r"小写[^¥￥\d]{0,20}[¥￥]?([0-9,]+\.\d{2})",
        r"[¥￥]\s*([0-9,]+\.\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                pass
    values = []
    for raw in re.findall(r"(?<!\d)([0-9]{1,9}(?:,[0-9]{3})*\.\d{2})(?!\d)", text):
        try:
            val = float(raw.replace(",", ""))
            if 0 < val < 100_000_000:
                values.append(val)
        except ValueError:
            pass
    return max(values) if values else None


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


def compose_pdf(items: list[InvoicePage], settings: Settings, copies: int | None = None) -> bytes:
    output = fitz.open()
    opened: dict[str, fitz.Document] = {}
    slots = layout_slots(settings)
    copy_count = settings.copies if copies is None else copies
    # “份数”属于每张发票：A、B 各 2 份应排成 A、A、B、B，
    # 而不是把整份清单排成 A、B、A、B。
    repeated = [item for item in items for _ in range(copy_count)]
    try:
        for start in range(0, len(repeated), len(slots)):
            w, h = page_size(settings)
            target_page = output.new_page(width=w, height=h)
            for slot, item in zip(slots, repeated[start : start + len(slots)]):
                source_doc = opened.get(item.path)
                if source_doc is None:
                    source_doc = fitz.open(item.path)
                    opened[item.path] = source_doc
                source_page = source_doc[item.page_no]
                dest = fit_rect(source_page.rect, slot)
                target_page.show_pdf_page(dest, source_doc, item.page_no, keep_proportion=True)
                if settings.crop_marks:
                    draw_crop_marks(target_page, dest)
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
        except ValueError:
            messagebox.showwarning("页边距", "请输入 0 到 50 之间的数字。", parent=self)
            return
        self.result = value
        self.destroy()


class InvoicePrinterApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1380x850")
        self.minsize(1100, 720)
        self.configure(bg=COLORS["canvas"])
        self.settings = Settings()
        self.items: list[InvoicePage] = []
        self.preview_doc: fitz.Document | None = None
        self.preview_image: ImageTk.PhotoImage | None = None
        self.thumb_images: list[ImageTk.PhotoImage] = []
        self.preview_page = 0
        self.zoom = 0.86
        self._preview_job = None
        self._thumb_reset_job = None
        self._thumb_hover = False
        self.status = tk.StringVar(value="添加 PDF 发票后即可预览和打印")
        self.count_var = tk.StringVar(value="0 张")
        self.output_var = tk.StringVar(value="A4 · 纵向 · 每页 1 张")
        self._configure_style()
        self._build_ui()
        self._setup_drag_and_drop()
        self._load_printers()
        self._refresh_thumbnails()
        self._update_summary()
        self._schedule_preview()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        tk.Label(title_box, text="PDF 批量排版与打印", fg=COLORS["muted"], bg=COLORS["panel"], font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        badge = tk.Label(header, text="●  本地离线", fg="#15803D", bg="#ECFDF3", font=("Microsoft YaHei UI", 9), padx=12, pady=5)
        badge.pack(side="right", padx=22)
        tk.Frame(self, bg=COLORS["border"], height=1).pack(fill="x")

        body = tk.Frame(self, bg=COLORS["canvas"])
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        self.left = tk.Frame(body, width=246, bg=COLORS["sidebar"])
        self.center = tk.Frame(body, bg=COLORS["canvas"])
        self.right = tk.Frame(body, width=370, bg=COLORS["panel"])
        self.left.grid(row=0, column=0, sticky="nsew")
        tk.Frame(body, width=1, bg=COLORS["border"]).grid(row=0, column=0, sticky="nse")
        self.center.grid(row=0, column=1, sticky="nsew")
        tk.Frame(body, width=1, bg=COLORS["border"]).grid(row=0, column=2, sticky="nsw")
        self.right.grid(row=0, column=2, sticky="nsew")
        self.left.grid_propagate(False)
        self.right.grid_propagate(False)
        self._build_left()
        self._build_center()
        self._build_right()

    def _setup_drag_and_drop(self):
        """允许从资源管理器拖入一个或多个 PDF（含空格和中文路径）。"""
        targets = (self, self.left, self.center, self.thumb_canvas, self.preview_canvas)
        for widget in targets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._drop_leave)
            widget.dnd_bind("<<Drop>>", self._drop_files)

    def _drop_enter(self, event):
        self.status.set("松开鼠标即可添加 PDF 发票")
        return event.action

    def _drop_leave(self, event):
        self.status.set("添加 PDF 发票后即可预览和打印" if not self.items else "预览已更新")
        return event.action

    def _drop_files(self, event):
        try:
            paths = list(self.tk.splitlist(event.data))
        except (tk.TclError, TypeError):
            paths = [event.data]
        pdfs = [path for path in paths if Path(path).is_file() and Path(path).suffix.lower() == ".pdf"]
        if not pdfs:
            self.status.set("拖入的文件中没有 PDF")
            messagebox.showinfo("拖放文件", "请拖入一个或多个 PDF 文件。", parent=self)
            return event.action
        self.add_paths(pdfs)
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
        sort = ttk.Combobox(sort_row, textvariable=self.sort_var, values=("添加顺序", "文件名", "金额从高到低"), state="readonly", width=13)
        sort.pack(side="right")
        sort.bind("<<ComboboxSelected>>", lambda _: self._sort_items())

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
        ttk.Button(buttons, text="＋  添加 PDF 发票", style="Primary.TButton", command=self.add_files).pack(fill="x", pady=(0, 8))
        ttk.Button(buttons, text="清空列表", style="Secondary.TButton", command=self.clear_items).pack(fill="x")
        tk.Label(buttons, text="移除只影响清单，原 PDF 会保留", bg=COLORS["sidebar"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 8)).pack(pady=(8, 0))

    def _build_center(self):
        bar = tk.Frame(self.center, bg=COLORS["canvas"], height=58)
        bar.pack(fill="x", padx=24, pady=(8, 0))
        bar.pack_propagate(False)
        tk.Label(bar, text="打印预览", bg=COLORS["canvas"], fg=COLORS["text"], font=("Microsoft YaHei UI", 12, "bold")).pack(side="left", pady=15)
        self.status_label = tk.Label(bar, textvariable=self.status, bg="#E2E8F0", fg=COLORS["muted"], font=("Microsoft YaHei UI", 8), padx=10, pady=4)
        self.status_label.pack(side="right", pady=15)
        tk.Label(self.center, textvariable=self.output_var, bg=COLORS["canvas"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=24, pady=(0, 2))

        preview_wrap = tk.Frame(self.center, bg=COLORS["canvas"])
        preview_wrap.pack(fill="both", expand=True, padx=24)
        self.preview_canvas = tk.Canvas(preview_wrap, bg=COLORS["canvas"], highlightthickness=0, bd=0)
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.bind("<Configure>", lambda _: self._render_preview())

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
        pane = ttk.Frame(self.right)
        pane.pack(fill="both", expand=True, padx=24, pady=20)
        ttk.Label(pane, text="打印设置", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(pane, text="选择设备并调整输出方式", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 18))

        ttk.Label(pane, text="打印机").grid(row=2, column=0, sticky="w", pady=6)
        self.printer_var = tk.StringVar()
        self.printer_box = ttk.Combobox(pane, textvariable=self.printer_var, state="readonly", width=21)
        self.printer_box.grid(row=2, column=1, sticky="ew", padx=(12, 7), pady=6)
        ttk.Button(pane, text="属性", style="Secondary.TButton", width=6, command=self.printer_properties).grid(row=2, column=2, pady=6)

        ttk.Label(pane, text="打印方式").grid(row=3, column=0, sticky="w", pady=6)
        simplex = tk.Label(pane, text="✓  单面打印", bg="#ECFDF3", fg="#15803D", font=("Microsoft YaHei UI", 8, "bold"), padx=9, pady=4)
        simplex.grid(row=3, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=6)

        self.copies_var = tk.IntVar(value=1)
        copies = ttk.Spinbox(pane, from_=1, to=99, textvariable=self.copies_var, width=12, command=self._settings_changed)
        copies.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6)
        copies.bind("<KeyRelease>", lambda _: self._settings_changed())

        ttk.Label(pane, text="份数").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Label(pane, text="纸张").grid(row=5, column=0, sticky="w", pady=6)
        self.paper_var = tk.StringVar(value="A4")
        paper = ttk.Combobox(pane, textvariable=self.paper_var, values=tuple(PAPER_SIZES), state="readonly")
        paper.grid(row=5, column=1, sticky="ew", padx=(12, 7), pady=6)
        paper.bind("<<ComboboxSelected>>", lambda _: self._settings_changed())
        ttk.Button(pane, text="页边距", style="Secondary.TButton", width=6, command=self.change_margin).grid(row=5, column=2, pady=6)

        ttk.Separator(pane).grid(row=6, column=0, columnspan=3, sticky="ew", pady=16)
        ttk.Label(pane, text="打印排版", style="Section.TLabel").grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(pane, text="方向").grid(row=8, column=0, sticky="w", pady=6)
        self.orientation_var = tk.StringVar(value="portrait")
        orient = ttk.Frame(pane)
        orient.grid(row=8, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=6)
        ttk.Radiobutton(orient, text="纵向", variable=self.orientation_var, value="portrait", command=self._settings_changed).pack(side="left")
        ttk.Radiobutton(orient, text="横向", variable=self.orientation_var, value="landscape", command=self._settings_changed).pack(side="left", padx=16)

        ttk.Label(pane, text="版式").grid(row=9, column=0, sticky="nw", pady=9)
        self.layout_var = tk.StringVar(value="single")
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

        self.crop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pane, text="添加裁剪线", variable=self.crop_var, command=self._settings_changed).grid(row=11, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=10)

        ttk.Label(pane, text="分页").grid(row=12, column=0, sticky="w", pady=6)
        ttk.Label(pane, text="按发票顺序自动分页", style="Muted.TLabel").grid(row=12, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=6)
        pane.columnconfigure(1, weight=1)

        bottom = ttk.Frame(self.right)
        bottom.pack(fill="x", padx=24, pady=(0, 20))
        ttk.Separator(bottom).pack(fill="x", pady=(0, 12))
        self.summary_var = tk.StringVar(value="共 0 张发票，发票金额合计：—")
        ttk.Label(bottom, textvariable=self.summary_var, style="Title.TLabel").pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(bottom)
        actions.pack(fill="x")
        ttk.Button(actions, text="导出 PDF", style="Secondary.TButton", command=self.export_pdf).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.print_btn = ttk.Button(actions, text="开始打印", style="Primary.TButton", command=self.print_document)
        self.print_btn.pack(side="left", fill="x", expand=True)

    def _load_printers(self):
        if not win32print:
            self.printer_box["values"] = ("未检测到 Windows 打印组件",)
            self.printer_var.set("未检测到 Windows 打印组件")
            return
        try:
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            names = [p[2] for p in win32print.EnumPrinters(flags)]
            self.printer_box["values"] = names
            default = win32print.GetDefaultPrinter()
            self.printer_var.set(default if default in names else (names[0] if names else ""))
        except Exception:
            self.printer_box["values"] = ()

    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择 PDF 发票", filetypes=[("PDF 文件", "*.pdf")])
        if not paths:
            return
        self.add_paths(paths)

    def add_paths(self, paths):
        self.status.set("正在读取发票…")
        self.update_idletasks()
        errors = []
        added = 0
        duplicates = 0
        existing = {(str(Path(item.path).resolve()).casefold(), item.page_no) for item in self.items}
        for path in paths:
            try:
                doc = fitz.open(path)
                if doc.needs_pass:
                    errors.append(f"{Path(path).name}（已加密）")
                    doc.close()
                    continue
                for page_no, page in enumerate(doc):
                    key = (str(Path(path).resolve()).casefold(), page_no)
                    if key in existing:
                        duplicates += 1
                        continue
                    suffix = f" · 第 {page_no + 1} 页" if doc.page_count > 1 else ""
                    self.items.append(InvoicePage(path, page_no, Path(path).name + suffix, extract_amount(page), make_thumbnail(page)))
                    existing.add(key)
                    added += 1
                doc.close()
            except Exception as exc:
                errors.append(f"{Path(path).name}（{exc}）")
        self._sort_items()
        if added:
            detail = f"已添加 {added} 张发票"
            if duplicates:
                detail += f"，跳过 {duplicates} 张重复内容"
            self.status.set(detail)
        elif duplicates and not errors:
            self.status.set("这些 PDF 已经在列表中")
        if errors:
            messagebox.showwarning("部分文件未添加", "\n".join(errors[:8]), parent=self)

    def _sort_items(self):
        mode = self.sort_var.get()
        if mode == "文件名":
            self.items.sort(key=lambda x: (x.label.lower(), x.page_no))
        elif mode == "金额从高到低":
            self.items.sort(key=lambda x: x.amount if x.amount is not None else -1, reverse=True)
        self._refresh_thumbnails()
        self._update_summary()
        self._schedule_preview()

    def _refresh_thumbnails(self, reset_scroll: bool = True):
        if not reset_scroll and self._thumb_reset_job:
            self.after_cancel(self._thumb_reset_job)
            self._thumb_reset_job = None
        for child in self.thumb_inner.winfo_children():
            child.destroy()
        self.thumb_images.clear()
        if not self.items:
            empty = tk.Frame(self.thumb_inner, bg=COLORS["sidebar"])
            empty.pack(fill="x", padx=14, pady=34)
            tk.Label(empty, text="PDF", bg=COLORS["brand_soft"], fg=COLORS["brand"],
                     font=("Segoe UI", 10, "bold"), padx=12, pady=8).pack()
            tk.Label(empty, text="暂无发票", bg=COLORS["sidebar"], fg=COLORS["text"],
                     font=("Microsoft YaHei UI", 10, "bold")).pack(pady=(12, 3))
            tk.Label(empty, text="拖入 PDF，或点击下方按钮添加", bg=COLORS["sidebar"], fg=COLORS["muted"],
                     font=("Microsoft YaHei UI", 8), wraplength=160).pack()
            if reset_scroll:
                self._queue_thumb_scroll_reset()
            return
        for index, item in enumerate(self.items):
            card = tk.Frame(self.thumb_inner, bg=COLORS["panel"], bd=0, highlightthickness=1, highlightbackground=COLORS["border"])
            card.pack(fill="x", padx=7, pady=6)
            card_top = tk.Frame(card, bg="#ffffff")
            card_top.pack(fill="x", padx=9, pady=(7, 3))
            tk.Label(card_top, text=f"发票 {index + 1}", bg="#ffffff", fg=COLORS["brand"],
                     font=("Microsoft YaHei UI", 8, "bold")).pack(side="left")
            delete = tk.Button(card_top, text="移除", command=lambda i=index: self.remove_item(i),
                               bg="#ffffff", fg="#dc2626", activebackground="#fee2e2",
                               activeforeground="#b91c1c", relief="flat", bd=0, cursor="hand2",
                               font=("Microsoft YaHei UI", 8), padx=4, pady=1)
            delete.pack(side="right")
            image = ImageTk.PhotoImage(Image.open(io.BytesIO(item.thumb_png)))
            self.thumb_images.append(image)
            label = tk.Label(card, image=image, bg="#F8FAFC", bd=0, highlightthickness=1,
                             highlightbackground="#CBD5E1", cursor="hand2", padx=5, pady=5)
            label.pack(pady=(2, 6))
            label.bind("<Button-1>", lambda _, i=index: self._jump_to_item(i))
            tk.Label(card, text=item.label, bg="#ffffff", fg=COLORS["text"],
                     font=("Microsoft YaHei UI", 8), wraplength=174).pack(padx=8)
            amount = "金额未识别" if item.amount is None else f"¥ {item.amount:,.2f}"
            tk.Label(card, text=amount, bg="#ffffff",
                     fg=COLORS["muted"] if item.amount is None else "#0F766E",
                     font=("Microsoft YaHei UI", 8, "bold" if item.amount is not None else "normal")).pack(pady=(3, 9))
        if reset_scroll:
            self._queue_thumb_scroll_reset()

    def _queue_thumb_scroll_reset(self):
        if self._thumb_reset_job:
            try:
                self.after_cancel(self._thumb_reset_job)
            except tk.TclError:
                pass
        self._thumb_reset_job = self.after_idle(self._reset_thumb_scroll)

    def _reset_thumb_scroll(self):
        self._thumb_reset_job = None
        if self.winfo_exists():
            self.thumb_canvas.yview_moveto(0)

    def _jump_to_item(self, item_index: int):
        per_page = 1 if self.layout_var.get() == "single" else 2
        try:
            copies = max(1, min(99, int(self.copies_var.get())))
        except (ValueError, tk.TclError):
            copies = 1
        self._go_page((item_index * copies) // per_page)

    def remove_item(self, item_index: int):
        if not 0 <= item_index < len(self.items):
            return
        removed = self.items.pop(item_index)
        self.preview_page = max(0, min(self.preview_page, self._preview_page_count() - 1))
        self._refresh_thumbnails(reset_scroll=False)
        self._update_summary()
        self._schedule_preview()
        self.status.set(f"已从列表移除：{removed.label}")

    def _preview_page_count(self) -> int:
        per_page = 1 if self.layout_var.get() == "single" else 2
        try:
            copies = max(1, min(99, int(self.copies_var.get())))
        except (ValueError, tk.TclError):
            copies = 1
        return (len(self.items) * copies + per_page - 1) // per_page

    def clear_items(self):
        if not self.items:
            return
        if messagebox.askyesno("清空列表", "确定移除列表中的全部发票吗？\n原始 PDF 文件不会被删除。", parent=self):
            self.items.clear()
            self.preview_page = 0
            self._refresh_thumbnails()
            self._update_summary()
            self._schedule_preview()

    def _update_summary(self):
        amounts = [x.amount for x in self.items if x.amount is not None]
        total = sum(amounts)
        suffix = f"¥ {total:,.2f}" if amounts else "—"
        unknown = len(self.items) - len(amounts)
        extra = f"（{unknown} 张未识别金额）" if unknown else ""
        self.summary_var.set(f"共 {len(self.items)} 张发票，金额合计：{suffix}{extra}")
        self.count_var.set(f"{len(self.items)} 张")
        self._sync_settings()

    def _sync_settings(self):
        self.settings.paper = self.paper_var.get()
        self.settings.landscape = self.orientation_var.get() == "landscape"
        self.settings.layout = self.layout_var.get()
        self.settings.crop_marks = self.crop_var.get()
        try:
            self.settings.copies = max(1, min(99, int(self.copies_var.get())))
        except (ValueError, tk.TclError):
            self.settings.copies = 1
        per_page = len(layout_slots(self.settings))
        pages = (len(self.items) * self.settings.copies + per_page - 1) // per_page
        layout_name = {"single": "每页 1 张", "vertical": "上下 2 张", "horizontal": "左右 2 张"}[self.settings.layout]
        orientation = "横向" if self.settings.landscape else "纵向"
        self.output_var.set(f"{self.settings.paper} · {orientation} · {layout_name} · 预计 {pages} 页")
        self.layout_hint.set("左右排版的单张发票会较窄，建议查看预览。" if self.settings.layout == "horizontal" else
                             "上下排版通常适合常见的横版发票。" if self.settings.layout == "vertical" else "每页放置一张发票。")

    def _settings_changed(self):
        self._sync_settings()
        self.preview_page = 0
        self._schedule_preview()

    def change_margin(self):
        dialog = MarginDialog(self, self.settings.margin_mm)
        self.wait_window(dialog)
        if dialog.result is not None:
            self.settings.margin_mm = dialog.result
            self._schedule_preview()

    def _schedule_preview(self):
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(130, self._rebuild_preview)

    def _rebuild_preview(self):
        self._preview_job = None
        if self.preview_doc:
            self.preview_doc.close()
            self.preview_doc = None
        if not self.items:
            self.preview_canvas.delete("all")
            x, y = self.preview_canvas.winfo_width() / 2, self.preview_canvas.winfo_height() / 2
            self.preview_canvas.create_rectangle(x - 190, y - 92, x + 190, y + 92,
                                                 fill="#F8FAFC", outline=COLORS["border"], width=1)
            self.preview_canvas.create_text(x, y - 35, text="PDF", fill=COLORS["brand"], font=("Segoe UI", 17, "bold"))
            self.preview_canvas.create_text(x, y + 2, text="将 PDF 发票拖到这里", fill=COLORS["text"], font=("Microsoft YaHei UI", 12, "bold"))
            self.preview_canvas.create_text(x, y + 34, text="也可以点击左下角“添加 PDF 发票”", fill=COLORS["muted"], font=("Microsoft YaHei UI", 9))
            self.page_label.configure(text="0 / 0")
            self.status.set("添加 PDF 发票后即可预览和打印")
            return
        try:
            self._sync_settings()
            data = compose_pdf(self.items, self.settings)
            self.preview_doc = fitz.open(stream=data, filetype="pdf")
            self.preview_page = min(self.preview_page, self.preview_doc.page_count - 1)
            self.status.set("预览已更新")
            self._render_preview()
        except Exception as exc:
            self.status.set("预览生成失败")
            messagebox.showerror("预览失败", str(exc), parent=self)

    def _render_preview(self):
        if not self.preview_doc or self.preview_doc.page_count == 0:
            return
        page = self.preview_doc[self.preview_page]
        canvas_w = max(300, self.preview_canvas.winfo_width() - 40)
        canvas_h = max(300, self.preview_canvas.winfo_height() - 30)
        scale = min(canvas_w / page.rect.width, canvas_h / page.rect.height) * self.zoom
        scale = max(0.15, min(3.0, scale))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png")))
        self.preview_image = ImageTk.PhotoImage(image)
        self.preview_canvas.delete("all")
        x, y = self.preview_canvas.winfo_width() / 2, self.preview_canvas.winfo_height() / 2
        self.preview_canvas.create_rectangle(x - image.width / 2 + 5, y - image.height / 2 + 5,
                                             x + image.width / 2 + 5, y + image.height / 2 + 5,
                                             fill="#d8dbe0", outline="")
        self.preview_canvas.create_image(x, y, image=self.preview_image)
        self.page_label.configure(text=f"{self.preview_page + 1} / {self.preview_doc.page_count}")

    def _go_page(self, page: int):
        if not self.preview_doc:
            return
        self.preview_page = max(0, min(page, self.preview_doc.page_count - 1))
        self._render_preview()

    def _go_last(self):
        if self.preview_doc:
            self._go_page(self.preview_doc.page_count - 1)

    def _zoom(self, delta: float):
        self.zoom = max(0.4, min(1.8, self.zoom + delta))
        self._render_preview()

    def _on_thumb_wheel(self, event):
        widget = getattr(event, "widget", None) or self.winfo_containing(event.x_root, event.y_root)
        inside_list = False
        while widget is not None:
            if widget in (self.thumb_canvas, self.thumb_inner):
                inside_list = True
                break
            widget = getattr(widget, "master", None)
        if not inside_list:
            return None
        first, last = self.thumb_canvas.yview()
        if event.delta > 0:
            if first <= 0.0001:
                self.thumb_canvas.yview_moveto(0)
                return "break"
            self.thumb_canvas.yview_scroll(-1, "units")
        elif event.delta < 0:
            if last >= 0.9999:
                self.thumb_canvas.yview_moveto(max(0, 1 - (last - first)))
                return "break"
            self.thumb_canvas.yview_scroll(1, "units")
        return "break"

    def export_pdf(self):
        if not self.items:
            messagebox.showinfo("导出 PDF", "请先添加至少一张 PDF 发票。", parent=self)
            return
        self._sync_settings()
        path = filedialog.asksaveasfilename(title="导出排版后的 PDF", defaultextension=".pdf",
                                            initialfile="发票打印.pdf", filetypes=[("PDF 文件", "*.pdf")])
        if not path:
            return
        try:
            data = compose_pdf(self.items, self.settings)
            Path(path).write_bytes(data)
            self.status.set(f"已导出：{Path(path).name}")
            if messagebox.askyesno("导出完成", f"已保存到：\n{path}\n\n是否立即打开？", parent=self):
                os.startfile(path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)

    def printer_properties(self):
        name = self.printer_var.get()
        if not name:
            return
        try:
            subprocess.Popen(["rundll32.exe", "printui.dll,PrintUIEntry", "/e", "/n", name])
        except Exception as exc:
            messagebox.showerror("打印机属性", str(exc), parent=self)

    def print_document(self):
        if not self.items:
            messagebox.showinfo("开始打印", "请先添加至少一张 PDF 发票。", parent=self)
            return
        printer = self.printer_var.get()
        if not printer or not win32ui:
            messagebox.showerror("开始打印", "未找到可用的 Windows 打印机。", parent=self)
            return
        self._sync_settings()
        items = tuple(self.items)
        settings = replace(self.settings)
        pages = (len(items) * settings.copies + len(layout_slots(settings)) - 1) // len(layout_slots(settings))
        direction = "横向" if settings.landscape else "纵向"
        if not messagebox.askokcancel("确认打印", f"打印机：{printer}\n纸张：{settings.paper} · {direction}\n发票：{len(items)} 张 · {pages} 页\n份数：{settings.copies}\n\n确认开始打印？", parent=self):
            return
        self.print_btn.state(["disabled"])
        self.status.set("正在生成打印任务…")
        thread = threading.Thread(target=self._print_worker, args=(printer, items, settings), daemon=True)
        thread.start()

    def _print_worker(self, printer: str, items: tuple[InvoicePage, ...], settings: Settings):
        try:
            data = compose_pdf(list(items), settings)
            print_pdf_bytes(data, printer, settings)
            self.after(0, lambda: self._print_done(None))
        except Exception as exc:
            self.after(0, lambda e=exc: self._print_done(e))

    def _print_done(self, error: Exception | None):
        self.print_btn.state(["!disabled"])
        if error:
            self.status.set("打印失败")
            messagebox.showerror("打印失败", f"打印机未能接收任务：\n{error}", parent=self)
        else:
            self.status.set("打印任务已发送")
            messagebox.showinfo("打印完成", "打印任务已发送到打印机队列。", parent=self)

    def _on_close(self):
        self.destroy()

    def destroy(self):
        if self.preview_doc:
            self.preview_doc.close()
            self.preview_doc = None
        for job_name in ("_preview_job", "_thumb_reset_job"):
            job = getattr(self, job_name, None)
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, job_name, None)
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
        return devmode
    finally:
        win32print.ClosePrinter(handle)


def print_pdf_bytes(data: bytes, printer_name: str, settings: Settings) -> None:
    """将排版后的每一页渲染到所选 Windows 打印机。"""
    doc = fitz.open(stream=data, filetype="pdf")
    dc = None
    try:
        devmode = printer_devmode(printer_name, settings)
        dc = win32ui.CreateDCFromHandle(win32gui.CreateDC("WINSPOOL", printer_name, devmode))
        physical_w = dc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        physical_h = dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        offset_x = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        offset_y = dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
        if (physical_w > physical_h) != settings.landscape:
            raise RuntimeError("打印机驱动没有切换到所选方向，请检查打印机属性。")
        dc.StartDoc(APP_NAME)
        for page in doc:
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
