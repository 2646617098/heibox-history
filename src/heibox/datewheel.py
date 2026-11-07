"""滚轮式日期选择。

两个手输文本框要记格式、敲键盘，还会打错。这里换成滚轮：年 / 月 / 日各一列，
鼠标滚轮或拖动都能选，松手自动吸附到整行；日列跟着年月联动，2 月不会出现 30 日。
控件只用标准库 + tkinter，不引入第三方依赖。
"""

from __future__ import annotations

import calendar
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime
from tkinter import messagebox, ttk
from typing import Callable

from .ranges import TZ

VISIBLE_ROWS = 5
_HIGHLIGHT = "#e3edfb"
_MAX_YEAR = 2100


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def clamp_index(index: int, count: int) -> int:
    if count <= 0:
        return 0
    return max(0, min(count - 1, index))


def snap_offset(offset: float) -> int:
    """松手时吸附到最近的一行（四舍五入）。"""
    return int(round(offset))


def parse_iso_date(text: str, fallback: date) -> date:
    """把 YYYY-MM-DD 解析成 date；空串或坏值退回 fallback。"""
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return fallback


def iso_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


class WheelColumn(tk.Canvas):
    """单列滚轮：上下滚动选择整数，中间一行是当前值。"""

    def __init__(
        self,
        master: tk.Misc,
        values: list[int],
        *,
        width: int = 64,
        background: str = "#ffffff",
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._font = tkfont.nametofont("TkDefaultFont")
        self._family = self._font.actual("family")
        self._size = int(self._font.actual("size"))
        self._row_h = max(30, self._font.metrics("linespace") + 12)
        super().__init__(
            master,
            width=width,
            height=self._row_h * VISIBLE_ROWS,
            background=background,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._values = list(values) or [0]
        self._index = 0
        self._offset = 0.0
        self._anim: str | None = None
        self._press: tuple[float, float] | None = None
        self.on_change = on_change
        self.bind("<Configure>", lambda _event: self._render())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._render()

    # ---- 对外 -----------------------------------------------------

    @property
    def value(self) -> int:
        return self._values[self._index]

    @property
    def values(self) -> tuple[int, ...]:
        return tuple(self._values)

    def set_value(self, value: int) -> None:
        self._cancel_anim()
        self._offset = 0.0
        try:
            self._index = self._values.index(value)
        except ValueError:
            self._index = clamp_index(self._index, len(self._values))
        self._render()

    def set_values(self, values: list[int]) -> None:
        """换一列值（日列跟着年月变），尽量保留当前选中值。"""
        self._cancel_anim()
        current = self.value
        self._values = list(values) or [0]
        self._offset = 0.0
        if current in self._values:
            self._index = self._values.index(current)
        else:
            self._index = clamp_index(self._index, len(self._values))
        self._render()

    def destroy(self) -> None:
        self._cancel_anim()
        super().destroy()

    # ---- 绘制 -----------------------------------------------------

    def _render(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            width = int(self["width"])
        if height <= 1:
            height = int(self["height"])
        center = height / 2
        self.create_rectangle(
            0,
            center - self._row_h / 2,
            width,
            center + self._row_h / 2,
            fill=_HIGHLIGHT,
            outline="",
        )
        for i, value in enumerate(self._values):
            y = center + (i - self._index - self._offset) * self._row_h
            if y < -self._row_h or y > height + self._row_h:
                continue
            distance = abs(i - self._index - self._offset)
            if distance < 0.5:
                color = "#1a1a1a"
                font: object = (self._family, self._size, "bold")
            elif distance < 1.5:
                color = "#5a5a5a"
                font = (self._family, self._size)
            else:
                color = "#a9a9a9"
                font = (self._family, self._size)
            self.create_text(width / 2, y, text=str(value), fill=color, font=font)

    # ---- 交互 -----------------------------------------------------

    def _center(self) -> float:
        height = self.winfo_height()
        return (height if height > 1 else int(self["height"])) / 2

    def _on_wheel(self, event: tk.Event) -> None:
        delta = -1 if event.delta > 0 else 1
        target = clamp_index(self._index + delta, len(self._values))
        if target == self._index:
            return
        self._cancel_anim()
        self._index = target
        # 起点偏移一格，动画回到 0，形成“内容滑动－吸附”的滚轮手感
        self._offset = -delta
        self._settle()
        self._notify()

    def _on_press(self, event: tk.Event) -> None:
        self._cancel_anim()
        self._offset = 0.0
        self._press = (event.y, self._offset)
        self._render()

    def _on_motion(self, event: tk.Event) -> None:
        if self._press is None:
            return
        y0, _ = self._press
        offset = -(event.y - y0) / self._row_h
        # 别拖出列表首尾：不露出空白行
        offset = max(-self._index, min(len(self._values) - 1 - self._index, offset))
        self._offset = offset
        self._render()

    def _on_release(self, event: tk.Event) -> None:
        if self._press is None:
            return
        y0, _ = self._press
        self._press = None
        if abs(event.y - y0) < 3:
            # 没拖动，当作点击：点到哪一行就跳到哪一行
            row = int(round((event.y - self._center()) / self._row_h))
            target = clamp_index(self._index + row, len(self._values))
            self._offset = 0.0
            if target != self._index:
                self._index = target
                self._notify()
            self._render()
            return
        delta = snap_offset(self._offset)
        self._index = clamp_index(self._index + delta, len(self._values))
        self._offset -= delta
        self._settle()
        self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # ---- 吸附动画 -------------------------------------------------

    def _cancel_anim(self) -> None:
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except tk.TclError:
                pass
            self._anim = None

    def _settle(self) -> None:
        self._cancel_anim()
        self._step()

    def _step(self) -> None:
        if abs(self._offset) < 0.02:
            self._offset = 0.0
            self._render()
            self._anim = None
            return
        self._offset *= 0.62
        self._render()
        self._anim = self.after(16, self._step)


class DateWheelGroup(ttk.Frame):
    """年 / 月 / 日三列滚轮的组合。"""

    def __init__(
        self,
        master: tk.Misc,
        value: date,
        min_year: int,
        max_year: int,
        background: str,
    ) -> None:
        super().__init__(master)
        self.year = WheelColumn(
            self, list(range(min_year, max_year + 1)), width=76, background=background
        )
        self.month = WheelColumn(
            self, list(range(1, 13)), width=46, background=background
        )
        self.day = WheelColumn(
            self, list(range(1, 32)), width=46, background=background
        )
        for index, (column, title) in enumerate(
            ((self.year, "年"), (self.month, "月"), (self.day, "日"))
        ):
            column.grid(row=1, column=index)
            ttk.Label(self, text=title, foreground="#8a8a8a").grid(
                row=0, column=index
            )

        self.year.set_value(value.year)
        self.month.set_value(value.month)
        self.day.set_value(min(value.day, days_in_month(value.year, value.month)))
        self.year.on_change = self._sync_days
        self.month.on_change = self._sync_days
        self._sync_days()

    def _sync_days(self) -> None:
        count = days_in_month(self.year.value, self.month.value)
        self.day.set_values(list(range(1, count + 1)))

    def value(self) -> date:
        return date(self.year.value, self.month.value, self.day.value)


class RangePicker(tk.Toplevel):
    """选择时间范围：两个三列滚轮，选完回调 (start_text, end_text)。"""

    def __init__(
        self,
        master: tk.Misc,
        start_text: str,
        end_text: str,
        on_apply: Callable[[str, str], None],
    ) -> None:
        super().__init__(master)
        self.title("选择时间范围")
        self.transient(master)
        self.resizable(False, False)
        style = ttk.Style(self)
        background = style.lookup("TFrame", "background") or "#f0f0f0"
        self.configure(background=background)

        today = datetime.now(TZ).date()
        start = parse_iso_date(start_text, date(today.year, 1, 1))
        end = parse_iso_date(end_text, today)
        min_year = max(2000, min(2018, start.year, end.year, today.year))
        max_year = min(_MAX_YEAR, max(today.year, start.year, end.year))

        self._on_apply = on_apply
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="滚动选择起止日期；也可以直接点中间那一行。",
            foreground="#7a7a7a",
        ).pack(anchor="w")

        wheels = ttk.Frame(frame)
        wheels.pack(pady=(12, 14))
        left = ttk.Frame(wheels)
        left.pack(side="left")
        ttk.Label(left, text="开始", foreground="#3d3d3d").pack(anchor="w")
        self.start_group = DateWheelGroup(left, start, min_year, max_year, background)
        self.start_group.pack(anchor="w")
        ttk.Label(wheels, text="~", foreground="#5a5a5a").pack(side="left", padx=18)
        right = ttk.Frame(wheels)
        right.pack(side="left")
        ttk.Label(right, text="结束", foreground="#3d3d3d").pack(anchor="w")
        self.end_group = DateWheelGroup(right, end, min_year, max_year, background)
        self.end_group.pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="全部时间", width=10, command=self._apply_all).pack(
            side="left"
        )
        ttk.Button(buttons, text="确定", width=8, command=self._apply).pack(
            side="right"
        )
        ttk.Button(buttons, text="取消", width=8, command=self.destroy).pack(
            side="right", padx=(0, 8)
        )

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._apply())
        self._center(master)
        self.grab_set()
        self.focus_set()

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - width) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _apply_all(self) -> None:
        self._on_apply("", "")
        self.destroy()

    def _apply(self) -> None:
        start = self.start_group.value()
        end = self.end_group.value()
        if start > end:
            messagebox.showwarning(
                "时间范围", "开始日期不能晚于结束日期。", parent=self
            )
            return
        self._on_apply(iso_date(start), iso_date(end))
        self.destroy()
