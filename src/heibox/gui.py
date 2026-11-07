"""tkinter 图形界面：按时间范围查询本地缓存，手动触发同步。

设计约束（对应方案里的取舍）：
- 启动只读本地 SQLite：不联网、无定时器、无自动增量；
- 只有点「同步最新 / 全量同步」才发请求，撞上人机验证只保留断点、不重试；
- 同步与扫码登录都跑在后台线程，主线程只通过 queue + after() 消费事件；
- sqlite3 连接不能跨线程：后台任务自己开一条（WAL 已开，读写可并发）。

只依赖标准库 + 本项目已有模块，刻意不碰 rich/typer，方便打小体积 EXE。
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

import qrcode

from . import config as cfgmod
from .api import HeyboxClient
from .datewheel import RangePicker
from .errors import CaptchaRequired, ConfigError, HeiboxError
from .images import THUMB_SIZE, ImageCache
from .login import QRLogin, extract_userid
from .models import Coverage, Post, SyncReport
from .ranges import TZ, format_date, format_ts, parse_range
from .store import Store
from .sync import Syncer, download_thumbnails
from .text import clip, empty_reason

PAGE_SIZE = 50
COLUMNS = ("time", "title", "desc", "click", "like", "comment")
# 列表封面：下载和显示都是 96×96，参考手机端历史列表里封面占宽度的比例。
THUMB_DISPLAY = THUMB_SIZE
THUMB_COLUMN_WIDTH = THUMB_DISPLAY + 40
COVER_ROW_HEIGHT = THUMB_DISPLAY + 16
COLUMN_SPECS = {
    "time": ("时间", 165, "w"),
    "title": ("标题", 280, "w"),
    "desc": ("摘要", 320, "w"),
    "click": ("浏览", 70, "e"),
    "like": ("点赞", 60, "e"),
    "comment": ("评论", 60, "e"),
}

# 统一配色：浅灰底 + 白色内容区 + 蓝色主操作
BG = "#f4f6fa"
PANEL = "#ffffff"
LINE = "#dde3ec"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2f6fed"
STRIPE = "#f2f6fc"

Color = str | None


# ---------------------------------------------------------------- 纯函数（可测）


def parse_range_inputs(
    start_text: str, end_text: str, now: datetime | None = None
) -> tuple[int, int]:
    """把起止日期文本变成 [start, end] 两个 Unix 秒；空串表示不限。"""
    start = start_text.strip() or None
    end = end_text.strip() or None
    return parse_range(start, end, now=now)


def range_text(start_text: str, end_text: str) -> str:
    """范围按钮上的显示文字；两端都空表示不限时间。"""
    start_text = start_text.strip()
    end_text = end_text.strip()
    if not start_text and not end_text:
        return "全部时间"
    return f"{start_text or '不限'} ~ {end_text or '不限'}"


def page_count(total: int, size: int = PAGE_SIZE) -> int:
    return max(1, (total + size - 1) // size)


def range_label(start: int, end: int) -> str:
    first, last = format_date(start), format_date(end)
    return first if first == last else f"{first} ~ {last}"


def image_mode_text(mode: str) -> str:
    """按钮上的短标签；完整说明放在设置对话框里。"""
    return {"off": "不下载", "thumb": "缩略图", "full": "缩略+原图"}.get(mode, "不下载")


# ---------------------------------------------------------------- 工具


class ToolTip:
    """轻量 tooltip：鼠标悬停约 0.5 秒后，在控件下方显示说明。"""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self) -> None:
        if self._window is not None:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            window,
            text=self.text,
            justify="left",
            background="#fffbe6",
            foreground="#333333",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=5,
            wraplength=300,
            font=("Microsoft YaHei UI", 9),
        ).pack()
        self._window = window

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _cancel(self) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None


# ---------------------------------------------------------------- 应用


class HeiboxApp:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.config: cfgmod.Config | None = None
        self.config_error = ""
        try:
            self.config = cfgmod.load_config(self.data_dir)
        except ConfigError as exc:
            self.config_error = str(exc)

        self.store: Store | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.page = 1
        self.total = 0
        self.page_posts: list[Post] = []
        self.qr_window: tk.Toplevel | None = None
        self.images = ImageCache(self.data_dir)
        # Treeview 不持有 PhotoImage 的 Python 引用，必须自己留住，否则图片会变空白
        self._thumb_images: list[tk.PhotoImage] = []
        self._placeholder: tk.PhotoImage | None = None

        self.root = tk.Tk()
        self.root.title("小黑盒历史查询")
        self.root.geometry("1240x760")
        self.root.minsize(980, 600)
        self._setup_style()
        self._build_ui()
        self.root.report_callback_exception = self._on_callback_error
        self._refresh_account()

        if self.config_error:
            self._say(f"配置文件损坏：{self.config_error}", error=True)
        elif self.config is None:
            self._say("还没有绑定账号：点「扫码登录」即可自动绑定。")
        else:
            self.root.after(60, self.query)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._pump)

    # ---- 界面搭建 -------------------------------------------------

    def _setup_style(self) -> None:
        try:
            tkfont.nametofont("TkDefaultFont").configure(family="Microsoft YaHei UI", size=10)
            tkfont.nametofont("TkTextFont").configure(family="Microsoft YaHei UI", size=10)
        except tk.TclError:
            pass
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # 紧凑模式的行高跟着字体走，150% 缩放下也不会把字裁掉
        font = tkfont.nametofont("TkDefaultFont")
        family = font.actual("family")
        compact_height = max(26, font.metrics("linespace") + 10)

        style.configure(".", font=font, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("TSeparator", background=LINE)

        style.configure(
            "TButton",
            padding=(10, 5),
            background=PANEL,
            foreground=TEXT,
            bordercolor=LINE,
            lightcolor=PANEL,
            darkcolor=PANEL,
            focuscolor=ACCENT,
        )
        style.map(
            "TButton",
            background=[("active", "#e9eef8"), ("disabled", "#f1f3f6")],
            foreground=[("disabled", "#a0a8b5")],
            bordercolor=[("active", "#c6d2e6")],
        )
        style.configure("TEntry", fieldbackground=PANEL, bordercolor=LINE, padding=(6, 4))
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)])

        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=compact_height,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background="#eef2f8",
            foreground="#334155",
            padding=(6, 6),
            font=(family, 10, "bold"),
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", "#dbe7ff")],
            foreground=[("selected", "#111827")],
        )
        style.map("Treeview.Heading", background=[("active", "#e3e9f3")])

        # 封面模式：96px 图片 + 上下留白；首列没有子节点，缩进设为 0 让图片居中
        style.configure("Cover.Treeview", rowheight=COVER_ROW_HEIGHT, indent=0)
        style.configure("Compact.Treeview", rowheight=compact_height, indent=0)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(16, 12, 16, 12))
        outer.pack(fill="both", expand=True)

        # 第一行：时间范围
        row1 = ttk.Frame(outer)
        row1.pack(fill="x")
        ttk.Label(row1, text="时间范围", style="Muted.TLabel").pack(side="left")
        # 默认不限时间，打开软件看到的条数就是本地已下载的全部条数；
        # 想缩小范围时点按钮用滚轮选起止日期，或者点「今年 / 去年」快捷切换。
        self.from_text = ""
        self.to_text = ""
        self.range_var = tk.StringVar(value=range_text("", ""))
        ttk.Button(
            row1,
            textvariable=self.range_var,
            width=22,
            command=self.open_range_picker,
        ).pack(side="left", padx=(8, 14))

        for label, start, end in (
            ("全部", "", ""),
            ("去年", f"{datetime.now(TZ).year - 1}-01-01", f"{datetime.now(TZ).year - 1}-12-31"),
            ("今年", f"{datetime.now(TZ).year}-01-01", f"{datetime.now(TZ).year}-12-31"),
        ):
            ttk.Button(
                row1,
                text=label,
                width=6,
                command=lambda s=start, e=end: self._quick_range(s, e),
            ).pack(side="left", padx=2)

        # 第二行：账号与动作
        row2 = ttk.Frame(outer)
        row2.pack(fill="x", pady=(12, 0))
        show_thumbs = bool(self.config.show_thumbs) if self.config else True
        self.show_thumbs_var = tk.BooleanVar(value=show_thumbs)
        self.account_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.account_var, style="Muted.TLabel").pack(side="left")
        self.login_btn = ttk.Button(row2, text="扫码登录", width=9, command=self.start_login)
        self.login_btn.pack(side="left", padx=(10, 4))

        ttk.Separator(row2, orient="vertical").pack(side="left", fill="y", padx=12)

        self.latest_btn = ttk.Button(
            row2, text="同步最新", width=9, command=lambda: self.start_sync("latest")
        )
        self.latest_btn.pack(side="left", padx=4)
        self.full_btn = ttk.Button(
            row2, text="全量同步", width=9, command=lambda: self.start_sync("rewalk")
        )
        self.full_btn.pack(side="left", padx=4)
        ToolTip(
            self.latest_btn,
            "只刷新最近几页，检查有没有新帖子。\n请求少，适合日常打开时同步。",
        )
        ToolTip(
            self.full_btn,
            "从第一页重新扫描到账号最早一帖，补齐历史和漏掉的条目。\n"
            "已收录的条目会原地更新，请求较多、耗时较长。",
        )

        self.image_btn = ttk.Button(
            row2, text="", width=12, command=self.open_image_dialog
        )
        self.image_btn.pack(side="right", padx=(4, 8))
        self.thumbs_check = ttk.Checkbutton(
            row2,
            text="显示封面",
            variable=self.show_thumbs_var,
            command=self.on_toggle_thumbs,
        )
        self.thumbs_check.pack(side="right", padx=(4, 2))
        self._refresh_image_button()

        # 底部分页：先贴到底部，窗口不够高时也不会被表格挤出可视区
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        self.pager_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.pager_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(bottom, text="跳到", style="Muted.TLabel").pack(side="left", padx=(12, 4))
        self.page_entry = ttk.Entry(bottom, width=5, justify="center")
        self.page_entry.pack(side="left")
        self.page_entry.insert(0, "1")
        self.page_entry.bind("<Return>", self.jump_to_page)
        ttk.Button(bottom, text="跳转", width=6, command=self.jump_to_page).pack(
            side="left", padx=(4, 0)
        )
        self.next_btn = ttk.Button(
            bottom, text="下一页", width=8, command=lambda: self.flip_page(1)
        )
        self.prev_btn = ttk.Button(
            bottom, text="上一页", width=8, command=lambda: self.flip_page(-1)
        )
        self.next_btn.pack(side="right")
        self.prev_btn.pack(side="right", padx=(0, 6))
        ttk.Button(
            bottom, text="数据目录", width=9, command=self.open_data_dir
        ).pack(side="right", padx=(0, 12))
        ttk.Label(bottom, text="双击一行打开原文", foreground="#8a8a8a").pack(
            side="right", padx=12
        )

        # 结果表
        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True, pady=(10, 0))
        # 缩略图只能放在 #0（树列），所以用 tree headings 同时显示表头和数据列
        self.tree = ttk.Treeview(
            table,
            columns=COLUMNS,
            show="tree headings",
            selectmode="browse",
            style="Cover.Treeview",
        )
        self.tree.tag_configure("odd", background=STRIPE)
        self.tree.heading("#0", text="封面", anchor="center")
        self.tree.column(
            "#0",
            width=THUMB_COLUMN_WIDTH,
            anchor="center",
            stretch=False,
            minwidth=THUMB_COLUMN_WIDTH,
        )
        # 列宽按当前字体的实际像素宽度算：不同 DPI 缩放下写死的数字会截断时间
        font = tkfont.nametofont("TkDefaultFont")
        measured = {
            "time": font.measure("2026-11-03 18:22") + 28,
            "click": font.measure("99,999") + 22,
            "like": font.measure("99,999") + 22,
            "comment": font.measure("999") + 22,
        }
        for key in COLUMNS:
            title, default_width, anchor = COLUMN_SPECS[key]
            width = measured.get(key, default_width)
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(
                key, width=width, anchor=anchor, stretch=(key == "desc"), minwidth=60
            )
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_selected)

        # 按配置决定初始是「封面模式」还是「紧凑模式」
        self._apply_table_mode()

    # ---- 底部状态 -------------------------------------------------

    def _say(self, text: str, error: bool = False) -> None:
        """不在界面里占一行，操作状态只反映到窗口标题。"""
        title = "小黑盒历史查询"
        if text:
            suffix = text if len(text) <= 60 else text[:59] + "…"
            title = f"{title} · {suffix}"
            if error:
                title += "（出错）"
        self.root.title(title)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.latest_btn, self.full_btn, self.login_btn):
            button.configure(state=state)

    def _refresh_account(self) -> None:
        if self.config_error:
            self.account_var.set("账号：配置损坏")
            return
        if self.config is None:
            self.account_var.set("账号：未绑定")
            return
        has_cookie = bool(os.environ.get("HEIBOX_COOKIE") or self.config.cookie)
        self.account_var.set(
            f"账号：{self.config.userid}（{'已登录' if has_cookie else '匿名'}）"
        )

    # ---- 数据库 ---------------------------------------------------

    def _ensure_store(self) -> Store | None:
        if self.store is not None:
            return self.store
        if self.config is None:
            return None
        instance = Store(cfgmod.db_path(self.data_dir))
        try:
            instance.ensure_user(self.config.userid)
        except ConfigError:
            instance.close()
            raise
        self.store = instance
        return instance

    def _close_store(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None

    # ---- 查询 -----------------------------------------------------

    def _quick_range(self, start: str, end: str) -> None:
        self._apply_range(start, end)

    def open_range_picker(self) -> None:
        RangePicker(self.root, self.from_text, self.to_text, self._apply_range)

    def _apply_range(self, start: str, end: str) -> None:
        self.from_text = start
        self.to_text = end
        self.range_var.set(range_text(start, end))
        self.on_query()

    def on_query(self) -> None:
        self.page = 1
        self.query()

    def flip_page(self, delta: int) -> None:
        target = self.page + delta
        if target < 1 or target > page_count(self.total):
            return
        self.page = target
        self.query()

    def jump_to_page(self, _event: object = None) -> None:
        raw = self.page_entry.get().strip()
        if not raw:
            return
        try:
            target = int(raw)
        except ValueError:
            self._say("页码必须是数字", error=True)
            self._sync_page_entry()
            return

        target = min(max(1, target), page_count(self.total))
        self._sync_page_entry(target)
        if target == self.page:
            return
        self.page = target
        self.query()

    def _sync_page_entry(self, page: int | None = None) -> None:
        self.page_entry.delete(0, "end")
        self.page_entry.insert(0, str(page if page is not None else self.page))

    def query(self) -> None:
        if self.config is None:
            self._say("还没有绑定账号：先扫码登录。", error=True)
            return
        from_text = self.from_text.strip()
        to_text = self.to_text.strip()
        unbounded = not from_text and not to_text
        try:
            start, end = parse_range_inputs(from_text, to_text)
        except ValueError as exc:
            self._say(str(exc), error=True)
            return

        try:
            store = self._ensure_store()
        except (HeiboxError, OSError) as exc:
            self._say(str(exc), error=True)
            return
        if store is None:
            return

        try:
            total = store.count(self.config.userid, start, end)
            pages = page_count(total)
            self.page = min(max(1, self.page), pages)
            posts = store.query(
                self.config.userid, start, end, PAGE_SIZE, (self.page - 1) * PAGE_SIZE
            )
            coverage = store.coverage(self.config.userid)
        except Exception as exc:  # noqa: BLE001 - 界面边界，错误要显示出来而不是崩掉
            self._say(f"查询失败：{exc}", error=True)
            return

        self.total = total
        self._fill_table(posts)
        self._update_pager(total, pages, unbounded, coverage)
        if total == 0:
            self._say(empty_reason(coverage, start, end))
        else:
            # 条数与覆盖范围都在左下角，这里不显示任何常驻状态
            self._say("")

    def _fill_table(self, posts: list[Post]) -> None:
        self.page_posts = posts
        self.tree.delete(*self.tree.get_children())
        self._thumb_images.clear()
        show_thumbs = bool(self.show_thumbs_var.get())
        placeholder = self._placeholder_image() if show_thumbs else None
        for index, post in enumerate(posts):
            image = ""
            if show_thumbs and placeholder is not None:
                image = self._row_thumb(post, placeholder)
            self.tree.insert(
                "",
                "end",
                iid=str(post.linkid),
                image=image,
                tags=("odd",) if index % 2 else (),
                values=(
                    format_ts(post.create_at),
                    post.title or "(无标题)",
                    clip(post.description, 90),
                    f"{post.click:,}",
                    "—" if post.like_num is None else f"{post.like_num:,}",
                    f"{post.comment_num:,}",
                ),
            )

    def _row_thumb(self, post: Post, placeholder: tk.PhotoImage) -> tk.PhotoImage:
        """有本地缩略图就用它，没有就退回统一的占位图。"""
        path = self.images.thumb_path(post.linkid)
        try:
            if not path.is_file():
                return placeholder
            image = tk.PhotoImage(file=str(path))
        except (OSError, tk.TclError):
            return placeholder
        if image.width() != THUMB_DISPLAY or image.height() != THUMB_DISPLAY:
            # 旧版本存过别的尺寸，等下次同步重新下载
            return placeholder
        self._thumb_images.append(image)
        return image

    def _placeholder_image(self) -> tk.PhotoImage:
        """画一张灰色的“图片”占位图，只建一次。"""
        if self._placeholder is not None:
            return self._placeholder

        size = THUMB_DISPLAY
        image = tk.PhotoImage(width=size, height=size)
        bg, frame, art = "#ececf2", "#c5c5d2", "#b3b3c4"
        image.put(bg, to=(0, 0, size, size))

        pad = size // 8
        thickness = max(2, size // 32)
        image.put(frame, to=(pad, pad, size - pad, pad + thickness))
        image.put(frame, to=(pad, size - pad - thickness, size - pad, size - pad))
        image.put(frame, to=(pad, pad, pad + thickness, size - pad))
        image.put(frame, to=(size - pad - thickness, pad, size - pad, size - pad))

        # 太阳
        cx, cy, radius = int(size * 0.66), int(size * 0.36), max(2, size // 11)
        for y in range(cy - radius, cy + radius + 1):
            half = int((radius * radius - (y - cy) ** 2) ** 0.5)
            image.put(frame, to=(cx - half, y, cx + half + 1, y + 1))

        # 山
        left, right = pad + 5, size - pad - 5
        peak, base = int(size * 0.42), size - pad - 5
        for y in range(peak, base):
            ratio = (y - peak) / max(1, base - peak)
            half = int(ratio * (right - left) / 2)
            mid = (left + right) // 2 - 3
            image.put(art, to=(mid - half, y, mid + half + 1, y + 1))

        self._placeholder = image
        return image

    def _update_pager(
        self,
        total: int,
        pages: int,
        unbounded: bool = False,
        coverage: Coverage | None = None,
    ) -> None:
        """左下角：默认视图显示本地缓存条数与覆盖范围，缩小范围时只显示命中数。"""
        if total == 0:
            self.pager_var.set("本地共 0 条" if unbounded else "没有命中")
        elif unbounded:
            oldest = format_date(coverage.oldest) if coverage and coverage.oldest else "—"
            newest = format_date(coverage.newest) if coverage and coverage.newest else "—"
            self.pager_var.set(
                f"全部 · 缓存 {total:,} 条 · 覆盖 {oldest} ~ {newest} · 第 {self.page}/{pages} 页"
            )
        else:
            self.pager_var.set(f"命中 {total:,} 条 · 第 {self.page}/{pages} 页")
        self.prev_btn.configure(state="normal" if self.page > 1 else "disabled")
        self.next_btn.configure(state="normal" if self.page < pages else "disabled")
        self._sync_page_entry()

    def open_selected(self, _event: object = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        try:
            linkid = int(selection[0])
        except ValueError:
            return
        post = next((item for item in self.page_posts if item.linkid == linkid), None)
        if post is None:
            return
        if not post.url:
            messagebox.showinfo("打开原文", "这条帖子没有可用的网页地址。")
            return
        webbrowser.open(post.url)

    def open_data_dir(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(self.data_dir)  # type: ignore[attr-defined]
            else:
                messagebox.showinfo("数据目录", str(self.data_dir))
        except OSError as exc:
            messagebox.showerror("数据目录", f"打不开：{exc}")

    # ---- 图片下载设置 ---------------------------------------------

    def _apply_table_mode(self) -> None:
        """在「封面模式」和「紧凑模式」之间切换列表布局。"""
        show = bool(self.show_thumbs_var.get())
        if show:
            self.tree.configure(show="tree headings", style="Cover.Treeview")
            # 隐藏再显示后 #0 的宽度可能被主题改掉，这里重新固定
            self.tree.column(
                "#0",
                width=THUMB_COLUMN_WIDTH,
                minwidth=THUMB_COLUMN_WIDTH,
                anchor="center",
                stretch=False,
            )
            # 封面占掉宽度后，标题收窄一点，把空间让给摘要
            self.tree.column("title", width=240, minwidth=180, stretch=False)
            self.tree.column("desc", minwidth=180, stretch=True)
        else:
            self.tree.configure(show="headings", style="Compact.Treeview")
            self.tree.column("title", width=280, minwidth=180, stretch=False)
            self.tree.column("desc", minwidth=160, stretch=True)
        self._fill_table(self.page_posts)

    def on_toggle_thumbs(self) -> None:
        show = bool(self.show_thumbs_var.get())
        if self.config is not None:
            self.config.show_thumbs = show
            try:
                cfgmod.save_config(self.data_dir, self.config)
            except OSError as exc:
                self.show_thumbs_var.set(not show)
                messagebox.showerror("显示封面", f"保存失败：{exc}")
                return
        self._apply_table_mode()

    def _refresh_image_button(self) -> None:
        mode = cfgmod.normalize_image_mode(self.config.image_mode) if self.config else "off"
        self.image_btn.configure(text=f"图片：{image_mode_text(mode)}")

    def open_image_dialog(self) -> None:
        if self.config is None:
            messagebox.showinfo("图片下载", "还没有绑定账号：先扫码登录。")
            return

        try:
            store = self._ensure_store()
        except (HeiboxError, OSError) as exc:
            messagebox.showerror("图片下载", f"读取本地缓存失败：{exc}")
            return
        if store is None:
            messagebox.showerror("图片下载", "打不开本地数据库。")
            return
        sources = store.thumb_sources(self.config.userid)
        no_url = store.count_without_thumb_url(self.config.userid)
        pending = [
            (linkid, url) for linkid, url in sources if not self.images.has_thumb(linkid)
        ]
        cached = len(sources) - len(pending)

        win = tk.Toplevel(self.root)
        win.title("图片下载")
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="同步时是否顺带下载帖子封面？").pack(anchor="w")
        mode_var = tk.StringVar(
            value=cfgmod.normalize_image_mode(self.config.image_mode)
        )
        options = (
            ("off", "不下载", "列表显示默认占位图，最省流量（默认）"),
            ("thumb", "仅缩略图", "96×96 PNG，每条约 10~20 KB，够列表显示"),
            ("full", "缩略图 + 原图", "再保存每帖首张原图，每条约 100~300 KB"),
        )
        for value, label, detail in options:
            ttk.Radiobutton(
                frame, text=label, value=value, variable=mode_var
            ).pack(anchor="w", pady=(8, 0))
            ttk.Label(
                frame, text=detail, foreground="#7a7a7a", padding=(22, 0, 0, 0)
            ).pack(anchor="w")

        ttk.Separator(frame).pack(fill="x", pady=12)
        if sources:
            summary = (
                f"已有封面地址 {len(sources)} 条，其中 {cached} 张缩略图已存到本地。"
            )
        else:
            summary = "本地还没有带封面地址的帖子。"
        if no_url:
            summary += f"\n另有 {no_url} 条旧记录没有封面地址，需要重扫列表才能补。"
        ttk.Label(
            frame,
            text=summary,
            foreground="#3d3d3d",
            wraplength=380,
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="封面只存本地；查询和翻页都直接读 images\\thumb 下的文件，不会再联网。\n"
            "关掉工具栏的「显示封面」即可切到紧凑模式，一页能多看不少条目。",
            foreground="#7a7a7a",
            wraplength=380,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        def save() -> None:
            mode = cfgmod.normalize_image_mode(mode_var.get())
            self.config.image_mode = mode
            try:
                cfgmod.save_config(self.data_dir, self.config)
            except OSError as exc:
                messagebox.showerror("图片下载", f"保存失败：{exc}", parent=win)
                return
            self._refresh_image_button()
            win.destroy()
            hint = (
                "；下次同步会顺带下载；也可以点「下载全部封面」一次补齐"
                if mode != "off"
                else "；已下载的图片会保留"
            )
            self._say(f"图片下载：{cfgmod.IMAGE_MODE_LABELS[mode]}{hint}")

        def download_all() -> None:
            if self.worker is not None and self.worker.is_alive():
                messagebox.showinfo("下载封面", "后台任务正在运行，等它结束再试。", parent=win)
                return
            mode = cfgmod.normalize_image_mode(mode_var.get())
            if not pending and not no_url:
                messagebox.showinfo(
                    "下载封面", "本地封面已经齐了，列表里的图都是本地文件。", parent=win
                )
                return
            if mode == "off":
                if not messagebox.askokcancel(
                    "下载封面",
                    "当前档位是「不下载」。补全封面会把档位改为「仅缩略图」，继续？",
                    parent=win,
                ):
                    return
                mode = "thumb"
                mode_var.set(mode)

            if no_url:
                state = store.load_state(self.config.userid)
                pages = (state.total_page or max(
                    1, (store.count(self.config.userid, 0, 10**10) + 59) // 60
                ))
                message = (
                    f"有 {no_url} 条旧记录还没有封面地址，需要重新扫描整份列表"
                    f"（约 {pages} 页）才能补上。\n\n"
                    "扫描完会把缺的封面一一下载到本地，可能要几分钟。"
                    "期间窗口可以最小化，但不要关闭。继续？"
                )
            else:
                size_mb = max(1, len(pending) * 15 // 1024)
                message = (
                    f"将把 {len(pending)} 张缺失封面下载到本地，约 {size_mb} MB。\n"
                    "只请求图床，不碰帖子列表接口。继续？"
                )
            if not messagebox.askokcancel("补全封面", message, parent=win):
                return

            self.config.image_mode = mode
            try:
                cfgmod.save_config(self.data_dir, self.config)
            except OSError as exc:
                messagebox.showerror("图片下载", f"保存失败：{exc}", parent=win)
                return
            self._refresh_image_button()
            win.destroy()
            self.start_cover_job(mode, need_scan=bool(no_url))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(
            buttons, text="下载全部封面", width=14, command=download_all
        ).pack(side="left")
        ttk.Button(buttons, text="保存", width=10, command=save).pack(side="right")
        ttk.Button(buttons, text="取消", width=10, command=win.destroy).pack(
            side="right", padx=(0, 8)
        )
        win.bind("<Return>", lambda _event: save())
        win.bind("<Escape>", lambda _event: win.destroy())
        win.grab_set()

    # ---- 批量补封面（后台线程） -----------------------------------

    def start_cover_job(self, mode: str, need_scan: bool) -> None:
        if self.config is None:
            return
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("下载封面", "后台任务正在运行，等它结束再试。")
            return
        self._set_busy(True)
        self._say(
            "开始重新扫描列表并补全封面…" if need_scan else "开始下载缺失封面…"
        )
        self.worker = threading.Thread(
            target=self._cover_job,
            args=(self.config, mode, need_scan),
            daemon=True,
        )
        self.worker.start()

    def _cover_job(self, config: cfgmod.Config, mode: str, need_scan: bool) -> None:
        events = self.events
        try:
            store = Store(cfgmod.db_path(self.data_dir))
            try:
                store.ensure_user(config.userid)
                cookie = os.environ.get("HEIBOX_COOKIE") or config.cookie
                with HeyboxClient(interval=config.request_interval, cookie=cookie) as client:
                    report = SyncReport()
                    if need_scan:
                        # 旧数据没有封面地址，只能从头重扫；同步过程中会顺带下图
                        state = store.load_state(config.userid)
                        state.next_offset = 0
                        state.reached_end = False
                        store.save_state(state)
                        syncer = Syncer(
                            store,
                            client,
                            config.userid,
                            log=lambda message: events.put(("log", message)),
                            image_mode=mode,
                            images=self.images,
                        )
                        report = syncer.sync_full()

                    sources = store.thumb_sources(config.userid)
                    missing = [
                        (linkid, url)
                        for linkid, url in sources
                        if not self.images.has_thumb(linkid)
                    ]
                    saved, failed = download_thumbnails(
                        client,
                        self.images,
                        missing,
                        on_progress=lambda index, total, ok, bad: events.put(
                            ("cover_progress", (index, total, ok, bad))
                        ),
                    )
                    report.images += saved
                    report.images_failed += failed
            finally:
                store.close()
        except CaptchaRequired as exc:
            events.put(("captcha", str(exc)))
        except (HeiboxError, OSError) as exc:
            events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            events.put(("error", f"{type(exc).__name__}: {exc}"))
        else:
            events.put(("covers_done", report))

    # ---- 同步（后台线程） -----------------------------------------

    def start_sync(self, mode: str) -> None:
        if self.config is None:
            messagebox.showinfo("同步", "还没有绑定账号：先扫码登录。")
            return
        if self.worker is not None and self.worker.is_alive():
            return
        self._set_busy(True)
        self._say(
            "开始全量同步（从第一页重扫到最早）…"
            if mode == "rewalk"
            else "开始同步最新几页…"
        )
        self.worker = threading.Thread(
            target=self._sync_job, args=(self.config, mode), daemon=True
        )
        self.worker.start()

    def _sync_job(self, config: cfgmod.Config, mode: str) -> None:
        events = self.events
        try:
            store = Store(cfgmod.db_path(self.data_dir))
            try:
                store.ensure_user(config.userid)
                cookie = os.environ.get("HEIBOX_COOKIE") or config.cookie
                with HeyboxClient(interval=config.request_interval, cookie=cookie) as client:
                    syncer = Syncer(
                        store,
                        client,
                        config.userid,
                        log=lambda message: events.put(("log", message)),
                        image_mode=config.image_mode,
                        images=self.images,
                    )
                    if mode == "rewalk":
                        # 断点归零、从头重扫：已收录的条目会被原地更新（补齐点赞等新字段）
                        state = store.load_state(config.userid)
                        state.next_offset = 0
                        state.reached_end = False
                        store.save_state(state)

                    report = (
                        syncer.sync_latest()
                        if mode == "latest"
                        else syncer.sync_full()
                    )
            finally:
                store.close()
        except CaptchaRequired as exc:
            events.put(("captcha", str(exc)))
        except (HeiboxError, OSError) as exc:
            events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底，别让界面静默卡死
            events.put(("error", f"{type(exc).__name__}: {exc}"))
        else:
            events.put(("done", report))

    # ---- 扫码登录 -------------------------------------------------

    def start_login(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        self._set_busy(True)
        self._say("正在获取二维码…")
        self.worker = threading.Thread(
            target=self._login_job, args=(self.config,), daemon=True
        )
        self.worker.start()

    def _login_job(self, config: cfgmod.Config | None) -> None:
        events = self.events
        try:
            with QRLogin(
                timeout=180,
                interval=2.0,
                on_qr=lambda url: events.put(("qr", url)),
                on_state=lambda state, message: events.put(
                    ("login_state", f"{state} {message}".strip())
                ),
            ) as qr:
                cookie, nickname = qr.run()

            userid = extract_userid(cookie)
            if userid is None:
                events.put(
                    (
                        "login_fail",
                        "登录成功但没解析到 userid；请重新扫码，"
                        "或在命令行用 heibox init --userid 手动绑定",
                    )
                )
                return

            if config is not None and config.userid != userid:
                events.put(
                    (
                        "login_fail",
                        f"扫码账号 {userid} 与当前配置 {config.userid} 不一致；"
                        "如需切换请更换数据目录",
                    )
                )
                return

            if config is None:
                config = cfgmod.Config(userid=userid)

            config.cookie = cookie
            cfgmod.save_config(self.data_dir, config)

            try:
                with HeyboxClient(interval=0, cookie=cookie) as client:
                    page = client.fetch_page(userid, 0)
            except CaptchaRequired as exc:
                events.put(
                    (
                        "login_partial",
                        (f"Cookie 已保存，但接口仍要求验证（{exc}）；稍后再同步。", config),
                    )
                )
                return

            if page.profile_userid and page.profile_userid != userid:
                config.cookie = ""
                cfgmod.save_config(self.data_dir, config)
                events.put(
                    (
                        "login_fail",
                        f"扫码的是 userid={page.profile_userid}，与识别出的 {userid} 不一致；已丢弃",
                    )
                )
                return
            events.put(("login_ok", (nickname or "", config)))
        except (HeiboxError, OSError) as exc:
            events.put(("login_fail", str(exc)))
        except Exception as exc:  # noqa: BLE001
            events.put(("login_fail", f"{type(exc).__name__}: {exc}"))

    def _show_qr(self, url: str) -> None:
        self._close_qr()
        win = tk.Toplevel(self.root)
        win.title("扫码登录")
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text="用小黑盒 App 右上角「扫一扫」", padding=12).pack()

        code = qrcode.QRCode(border=2)
        code.add_data(url)
        code.make(fit=True)
        matrix = code.get_matrix()
        modules = max(len(matrix), 1)
        scale = max(2, 280 // modules)
        size = modules * scale
        canvas = tk.Canvas(win, width=size, height=size, bg="white", highlightthickness=0)
        canvas.pack(padx=12)
        for y, row in enumerate(matrix):
            for x, cell in enumerate(row):
                if cell:
                    canvas.create_rectangle(
                        x * scale, y * scale, (x + 1) * scale, (y + 1) * scale,
                        fill="black", outline="",
                    )
        ttk.Label(win, text="等待扫码…", foreground="#666666", padding=(0, 8, 0, 12)).pack()
        self.qr_window = win

    def _close_qr(self) -> None:
        if self.qr_window is not None:
            try:
                self.qr_window.destroy()
            except tk.TclError:
                pass
            self.qr_window = None

    # ---- 事件泵 ---------------------------------------------------

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    self._handle_event(kind, payload)
                except tk.TclError:
                    return
                except Exception as exc:  # noqa: BLE001 - 单个事件出错不该让整个泵停摆
                    self._say(f"内部错误：{type(exc).__name__}: {exc}", error=True)
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _on_callback_error(self, exc_type: type, exc: BaseException, _tb: object) -> None:
        """无控制台的 EXE 里，回调异常默认看不到，这里兜到状态栏上。"""
        try:
            self._say(f"内部错误：{exc_type.__name__}: {exc}", error=True)
        except Exception:  # noqa: BLE001
            pass

    def _handle_event(self, kind: str, payload: object) -> None:
        if kind == "log":
            self._say(str(payload))
        elif kind == "cover_progress":
            index, total, saved, failed = payload
            self._say(f"补封面 {index}/{total} · 成功 {saved} · 失败 {failed}")
        elif kind == "covers_done":
            self._set_busy(False)
            report = payload
            parts = ["封面补全完成"]
            if report.pages:
                parts.append(f"重扫 {report.pages} 页")
            parts.append(f"本地写入 {report.images} 张")
            if report.images_failed:
                parts.append(f"失败 {report.images_failed}")
            self._say(" · ".join(parts))
            self.query()
        elif kind == "done":
            self._set_busy(False)
            report = payload
            parts = [
                f"完成：{report.pages} 页",
                f"新收录 {report.inserted} 条",
                f"更新 {report.updated} 条",
            ]
            images = getattr(report, "images", 0)
            failed = getattr(report, "images_failed", 0)
            if images or failed:
                parts.append(f"图片 +{images}" + (f"（失败 {failed}）" if failed else ""))
            if getattr(report, "reached_end", False):
                parts.append("已到最早")
            self._say(" · ".join(parts))
            self.query()
        elif kind == "captcha":
            self._set_busy(False)
            self._say(
                f"接口要求人机验证（{payload}）：已抓取的部分已保存，"
                "可稍后重试，或点「扫码登录」后继续。",
                error=True,
            )
        elif kind == "error":
            self._set_busy(False)
            self._say(f"失败：{payload}", error=True)
        elif kind == "qr":
            self._say("请用小黑盒 App 扫码…")
            self._show_qr(str(payload))
        elif kind == "login_state":
            text = str(payload)
            if text:
                self._say(f"登录状态：{text}")
        elif kind in {"login_ok", "login_partial"}:
            self._set_busy(False)
            self._close_qr()
            if kind == "login_ok":
                nickname, config_obj = payload  # type: ignore[misc]
                self.config = config_obj
                suffix = f"（{nickname}）" if nickname else ""
                self._say(f"登录成功{suffix}，Cookie 已保存。")
                self.query()
            else:
                message, config_obj = payload  # type: ignore[misc]
                self.config = config_obj
                self._say(str(message), error=True)
            self._refresh_account()
            self._refresh_image_button()
        elif kind == "login_fail":
            self._set_busy(False)
            self._close_qr()
            self._say(f"登录失败：{payload}", error=True)

    # ---- 生命周期 -------------------------------------------------

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askokcancel("退出", "后台任务还在运行，确定退出？"):
                return
        self._close_store()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------- 入口


def _enable_dpi_awareness() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 非 Windows 或老系统，保持默认缩放即可
        pass


def _data_dir_from_argv(argv: list[str]) -> Path | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", type=Path)
    try:
        known, _unknown = parser.parse_known_args(argv)
    except SystemExit:
        return None
    return known.data_dir


def _fatal(exc: Exception) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("小黑盒历史查询", f"启动失败：{exc}")
        root.destroy()
    except Exception:  # noqa: BLE001 - 连弹窗都建不起来时只能放弃
        pass


def main(argv: list[str] | None = None) -> None:
    _enable_dpi_awareness()
    args = sys.argv[1:] if argv is None else argv
    data_dir = _data_dir_from_argv(args) or cfgmod.data_dir()
    try:
        app = HeiboxApp(data_dir)
    except Exception as exc:  # noqa: BLE001
        _fatal(exc)
        return
    app.run()


if __name__ == "__main__":
    main()
