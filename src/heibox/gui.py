"""tkinter 图形界面：按时间范围查询本地缓存，手动触发同步。

设计约束（对应方案里的取舍）：
- 启动只读本地 SQLite：不联网、无定时器、无自动增量；
- 只有点「同步最新 / 补全更早」才发请求，撞上人机验证只保留断点、不重试；
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
from .errors import CaptchaRequired, ConfigError, HeiboxError
from .login import QRLogin
from .models import Coverage, Post
from .ranges import TZ, format_date, format_ts, parse_range
from .store import Store
from .sync import Syncer
from .text import clip, coverage_line, empty_reason

PAGE_SIZE = 50
COLUMNS = ("time", "title", "desc", "click", "like", "comment")
COLUMN_SPECS = {
    "time": ("时间", 165, "w"),
    "title": ("标题", 280, "w"),
    "desc": ("摘要", 320, "w"),
    "click": ("浏览", 70, "e"),
    "like": ("点赞", 60, "e"),
    "comment": ("评论", 60, "e"),
}

Color = str | None


# ---------------------------------------------------------------- 纯函数（可测）


def parse_range_inputs(
    start_text: str, end_text: str, now: datetime | None = None
) -> tuple[int, int]:
    """把两个输入框的文本变成 [start, end] 两个 Unix 秒；空串表示不限。"""
    start = start_text.strip() or None
    end = end_text.strip() or None
    return parse_range(start, end, now=now)


def page_count(total: int, size: int = PAGE_SIZE) -> int:
    return max(1, (total + size - 1) // size)


def range_label(start: int, end: int) -> str:
    first, last = format_date(start), format_date(end)
    return first if first == last else f"{first} ~ {last}"


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
        self._base_status = ""

        self.root = tk.Tk()
        self.root.title("小黑盒历史查询")
        self.root.geometry("1140x680")
        self.root.minsize(900, 520)
        self._setup_style()
        self._build_ui()
        self.root.report_callback_exception = self._on_callback_error
        self._refresh_account()

        if self.config_error:
            self._say(f"配置文件损坏：{self.config_error}", error=True)
        elif self.config is None:
            self._say("还没有绑定账号：点「设置账号」填 userid。", error=True)
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
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", padding=(4, 4))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(12, 10, 12, 10))
        outer.pack(fill="both", expand=True)

        # 第一行：时间范围
        row1 = ttk.Frame(outer)
        row1.pack(fill="x")
        ttk.Label(row1, text="时间范围").pack(side="left")
        self.from_var = tk.StringVar(value=f"{datetime.now(TZ).year}-01-01")
        self.to_var = tk.StringVar(value="")
        ttk.Entry(row1, textvariable=self.from_var, width=12).pack(side="left", padx=(6, 2))
        ttk.Label(row1, text="~").pack(side="left")
        ttk.Entry(row1, textvariable=self.to_var, width=12).pack(side="left", padx=(2, 12))
        ttk.Button(row1, text="查询", width=8, command=self.on_query).pack(side="right")
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
        ttk.Label(
            row1, text="支持 2024 / 2024-03 / 2024-03-05", foreground="#7a7a7a"
        ).pack(side="left", padx=(12, 0))

        # 第二行：账号与动作
        row2 = ttk.Frame(outer)
        row2.pack(fill="x", pady=(10, 0))
        self.account_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.account_var).pack(side="left")
        ttk.Button(row2, text="设置账号", width=9, command=self.open_account_dialog).pack(
            side="left", padx=(10, 4)
        )
        self.login_btn = ttk.Button(row2, text="扫码登录", width=9, command=self.start_login)
        self.login_btn.pack(side="left", padx=4)

        ttk.Separator(row2, orient="vertical").pack(side="left", fill="y", padx=10)

        self.latest_btn = ttk.Button(
            row2, text="同步最新", width=9, command=lambda: self.start_sync("latest")
        )
        self.latest_btn.pack(side="left", padx=4)
        self.full_btn = ttk.Button(
            row2, text="补全更早", width=9, command=lambda: self.start_sync("full")
        )
        self.full_btn.pack(side="left", padx=4)
        self.rewalk_btn = ttk.Button(
            row2, text="刷新全部", width=9, command=lambda: self.start_sync("rewalk")
        )
        self.rewalk_btn.pack(side="left", padx=4)

        ttk.Button(row2, text="数据目录", width=9, command=self.open_data_dir).pack(side="right")

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(
            outer, textvariable=self.status_var, foreground="#3d3d3d", anchor="w"
        )
        self.status_label.pack(fill="x", pady=(10, 6))

        # 结果表
        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", selectmode="browse")
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

        # 底部分页
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.pager_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.pager_var).pack(side="left")
        self.next_btn = ttk.Button(
            bottom, text="下一页", width=8, command=lambda: self.flip_page(1)
        )
        self.prev_btn = ttk.Button(
            bottom, text="上一页", width=8, command=lambda: self.flip_page(-1)
        )
        self.next_btn.pack(side="right")
        self.prev_btn.pack(side="right", padx=(0, 6))
        ttk.Label(bottom, text="双击一行打开原文", foreground="#8a8a8a").pack(
            side="right", padx=12
        )

    # ---- 底部状态 -------------------------------------------------

    def _say(self, text: str, error: bool = False) -> None:
        self._base_status = text
        self.status_var.set(text)
        self.status_label.configure(foreground="#c0392b" if error else "#3d3d3d")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.latest_btn, self.full_btn, self.rewalk_btn, self.login_btn):
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
        self.from_var.set(start)
        self.to_var.set(end)
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

    def query(self) -> None:
        if self.config is None:
            self._say("还没有绑定账号：先点「设置账号」。", error=True)
            return
        try:
            start, end = parse_range_inputs(self.from_var.get(), self.to_var.get())
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
        self._update_pager(total, pages)
        if total == 0:
            self._say(empty_reason(coverage, start, end))
        else:
            self._say(f"{range_label(start, end)} · 命中 {total:,} 条 · {coverage_line(coverage)}")

    def _fill_table(self, posts: list[Post]) -> None:
        self.page_posts = posts
        self.tree.delete(*self.tree.get_children())
        for post in posts:
            self.tree.insert(
                "",
                "end",
                iid=str(post.linkid),
                values=(
                    format_ts(post.create_at),
                    post.title or "(无标题)",
                    clip(post.description, 90),
                    f"{post.click:,}",
                    "—" if post.like_num is None else f"{post.like_num:,}",
                    f"{post.comment_num:,}",
                ),
            )

    def _update_pager(self, total: int, pages: int) -> None:
        if total == 0:
            self.pager_var.set("没有结果")
        else:
            self.pager_var.set(f"共 {total:,} 条 · 第 {self.page}/{pages} 页")
        self.prev_btn.configure(state="normal" if self.page > 1 else "disabled")
        self.next_btn.configure(state="normal" if self.page < pages else "disabled")

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

    # ---- 账号绑定 -------------------------------------------------

    def open_account_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("设置账号")
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="小黑盒 userid（个人主页网址末尾的数字）").pack(anchor="w")
        value = tk.StringVar(value=str(self.config.userid) if self.config else "")
        entry = ttk.Entry(frame, textvariable=value, width=24)
        entry.pack(anchor="w", pady=(6, 12))
        entry.focus_set()

        def confirm() -> None:
            try:
                userid = int(value.get().strip())
                if userid <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("设置账号", "请填正整数 userid", parent=win)
                return
            interval = self.config.request_interval if self.config else 3.0
            cookie = self.config.cookie if self.config else ""
            self.config = cfgmod.Config(
                userid=userid, request_interval=interval, cookie=cookie
            )
            self.config_error = ""
            try:
                cfgmod.save_config(self.data_dir, self.config)
            except OSError as exc:
                messagebox.showerror("设置账号", f"保存失败：{exc}", parent=win)
                return
            self._close_store()
            self._refresh_account()
            win.destroy()
            self.page = 1
            self.query()

        ttk.Button(frame, text="保存", width=10, command=confirm).pack(anchor="e")
        win.bind("<Return>", lambda _event: confirm())

    # ---- 同步（后台线程） -----------------------------------------

    def start_sync(self, mode: str) -> None:
        if self.config is None:
            messagebox.showinfo("同步", "还没有绑定账号：先点「设置账号」。")
            return
        if self.worker is not None and self.worker.is_alive():
            return
        self._set_busy(True)
        self._say(
            {
                "full": "开始补全更早的历史…",
                "rewalk": "开始重新扫描全部页面（会更新已收录条目的数据）…",
            }.get(mode, "开始同步最新几页…")
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
        if self.config is None:
            messagebox.showinfo("扫码登录", "先点「设置账号」填 userid，再扫码登录。")
            return
        if self.worker is not None and self.worker.is_alive():
            return
        self._set_busy(True)
        self._say("正在获取二维码…")
        self.worker = threading.Thread(
            target=self._login_job, args=(self.config,), daemon=True
        )
        self.worker.start()

    def _login_job(self, config: cfgmod.Config) -> None:
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

            config.cookie = cookie
            cfgmod.save_config(self.data_dir, config)

            try:
                with HeyboxClient(interval=0, cookie=cookie) as client:
                    page = client.fetch_page(config.userid, 0)
            except CaptchaRequired as exc:
                events.put(
                    ("login_partial", f"Cookie 已保存，但接口仍要求验证（{exc}）；稍后再同步。")
                )
                return

            if page.profile_userid and page.profile_userid != config.userid:
                config.cookie = ""
                cfgmod.save_config(self.data_dir, config)
                events.put(
                    (
                        "login_fail",
                        f"扫码的是 userid={page.profile_userid}，与配置 {config.userid} 不一致；已丢弃",
                    )
                )
                return
            events.put(("login_ok", nickname or ""))
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
        elif kind == "done":
            self._set_busy(False)
            report = payload
            suffix = " · 已到最早" if getattr(report, "reached_end", False) else ""
            self._say(
                f"完成：{report.pages} 页 · 新收录 {report.inserted} 条"
                f" · 更新 {report.updated} 条{suffix}"
            )
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
            suffix = f"（{payload}）" if payload else ""
            self._say(f"登录成功{suffix}，Cookie 已保存。" if kind == "login_ok" else str(payload))
            self._refresh_account()
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
