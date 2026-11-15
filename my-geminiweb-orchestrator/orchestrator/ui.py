"""UI toolkit cho Orchestrator — theme tokens + widget primitives.

Vì sao không dùng thư viện UI sẵn có
-----------------------------------
`customtkinter`/`ttkbootstrap` đều chưa có trong môi trường, và kéo thêm
dependency vào bản `.exe` (mục tiêu: nhẹ + khởi động nhanh) là đánh đổi
không đáng. Tkinter có sẵn trong Python và **đủ** — vấn đề của GUI cũ không
phải toolkit, mà là:
  - dùng `ttk` theme `vista` → không tôi màu được, phải trông như Windows 95
  - bố cục 2 cột cố định → không mở rộng được khi chạy 8 job song song
  - mọi thứ đổi trong 1 `Treeview` → không lọc, không nhóm, không đo được

Toolkit này giải quyết đúng 3 điều đó: theme tối **tôi được**, widget vẽ
bằng Canvas (bo góc, progress, badge), và bảng dữ liệu tự vẽ (cột, badge,
hover, sort).

Nguyên tắc
----------
1. **Không phụ thuộc ngoài** — chỉ `tkinter`. Bản `.exe` không phình thêm.
2. Mọi widget tự vẽ bằng `Canvas` → không phụ thuộc theme của OS.
3. Widget nào đã có sẵn ở OS (Entry, Text, Combobox, Scrollbar) thì vẫn dùng
   bản gốc, chỉ tôi màu qua `fg`/`bg`/`insertbackground` — nhẹ hơn tự vẽ.
4. **Contract ổn định**: layout code (trong `gui.py`) chỉ dùng API ở đây.

Bảng màu — dark, tương phản ≥ 4.5:1 với nền
---------------------------------------------
    bg           #0f1115   nền app
    surface      #171a21   card / panel
    surface_2    #1f232c   hàng hover, input
    border       #2a2f3a   viền
    text         #e6e9ef   chữ chính
    text_dim     #98a0b0   chữ phụ
    text_faint   #6b7280   chữ rất mờ
    accent       #5b8cff   hành động chính
    ok           #34d399   thành công / session sống
    warn         #fbbf24   cảnh báo
    err          #f87171   lỗi
    run          #60a5fa   đang chạy
    queue        #a78bfa   xếp hàng
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG = "#0f1115"
SURFACE = "#171a21"
SURFACE_2 = "#1f232c"
SURFACE_3 = "#262b36"
BORDER = "#2a2f3a"
BORDER_SOFT = "#22262f"

TEXT = "#e6e9ef"
TEXT_DIM = "#98a0b0"
TEXT_FAINT = "#6b7280"

ACCENT = "#5b8cff"
ACCENT_DIM = "#2f4a8f"
ACCENT_HOVER = "#7099ff"
OK = "#34d399"
WARN = "#fbbf24"
ERR = "#f87171"
RUN = "#60a5fa"
QUEUE = "#a78bfa"

#: Màu theo trạng thái job.
STATE_COLORS = {
    "QUEUED": QUEUE,
    "RUNNING": RUN,
    "DONE": OK,
    "FAILED": ERR,
    "CANCELLED": WARN,
    "ACTIVE": OK,
    "AVAILABLE": OK,
    "UNAUTHENTICATED": ERR,
    "UNKNOWN": TEXT_FAINT,
}

#: Font — Segoe UI có sẵn trên Windows. `size` theo điểm.
FONT_FAMILY = "Segoe UI"
FONT_MONO = "Consolas"


def _font(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    return (FONT_FAMILY, size, weight)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Themed(tk.Misc):
    """Mixin: style một widget Tk cũ theo theme tối."""

    def _themed_entry(self, widget: tk.Entry, *, placeholder: str = "") -> tk.Entry:
        widget.configure(
            bg=SURFACE_2,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            borderwidth=0,
        )
        if placeholder:
            self._placeholder(widget, placeholder)
        return widget

    def _placeholder(self, widget: tk.Entry, text: str) -> None:
        """Placeholder đơn giản: focus vào → ẩn, blur về rỗng → hiện lại."""
        def show() -> None:
            if not widget.get():
                widget.insert(0, text)
                widget.configure(fg=TEXT_FAINT)

        def on_focus_in(_e: tk.Event) -> None:
            if widget.get() == text:
                widget.delete(0, "end")
                widget.configure(fg=TEXT)

        def on_focus_out(_e: tk.Event) -> None:
            if not widget.get():
                show()

        widget.bind("<FocusIn>", on_focus_in)
        widget.bind("<FocusOut>", on_focus_out)
        show()


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

class Card(tk.Frame):
    """Panel nền tối, viền mảnh, bo góc (vẽ bằng Canvas bên dưới)."""

    def __init__(self, master: tk.Misc, **kw: Any) -> None:
        self._bg = kw.pop("bg", SURFACE)
        super().__init__(master, bg=self._bg, highlightthickness=1,
                         highlightbackground=BORDER, bd=0, **kw)


class SectionTitle(tk.Frame):
    """Tiêu đề section + phụ tùy chọn."""

    def __init__(self, master: tk.Misc, text: str, sub: str = "", **kw: Any) -> None:
        super().__init__(master, bg=kw.pop("bg", SURFACE), **kw)
        tk.Label(
            self, text=text, bg=self["bg"], fg=TEXT,
            font=_font(10, "bold"),
        ).pack(side="left")
        if sub:
            tk.Label(
                self, text=sub, bg=self["bg"], fg=TEXT_FAINT,
                font=_font(9),
            ).pack(side="left", padx=(8, 0))


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

class Button(tk.Canvas):
    """Nút phẳng bo góc. Không phụ thuộc theme OS.

    Variants: `primary` | `ghost` | `danger` | `success`
    """

    _VARIANTS: dict[str, tuple[str, str, str]] = {
        # variant -> (bg, fg, bg_hover)
        "primary": (ACCENT, "#0b1220", ACCENT_HOVER),
        "success": (OK, "#06281c", "#4ade80"),
        "danger": (ERR, "#2b0b0b", "#fb923c"),
        "ghost": (SURFACE_2, TEXT, SURFACE_3),
        "outline": (SURFACE, TEXT_DIM, SURFACE_2),
    }

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        variant: str = "ghost",
        width: int = 120,
        height: int = 30,
        font: tuple[str, int, str] | None = None,
        radius: int = 7,
        **kw: Any,
    ) -> None:
        bg, fg, hover = self._VARIANTS.get(variant, self._VARIANTS["ghost"])
        super().__init__(
            master, width=width, height=height, highlightthickness=0,
            bd=0, bg=kw.pop("parent_bg", SURFACE), cursor="hand2", **kw
        )
        self._label = text
        self._cmd = command
        self._bg = bg
        self._fg = fg
        self._hover = hover
        self._radius = radius
        self._enabled = True
        self._font = font or _font(9, "bold" if variant == "primary" else "normal")
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._draw()

    # -- vẽ ---------------------------------------------------------------

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        bg = self._bg if self._enabled else SURFACE_2
        fg = self._fg if self._enabled else TEXT_FAINT
        self.create_rectangle(
            0, 0, w, h,
            fill=bg, outline="", width=0,
            tags=("btn",),
        )
        if self._radius > 0:
            # Che 4 góc bằng màu nền cha để tạo cảm giác bo góc.
            for x0, y0, x1, y1 in (
                (0, 0, self._radius, self._radius),
                (w - self._radius, 0, w, self._radius),
                (0, h - self._radius, self._radius, h),
                (w - self._radius, h - self._radius, w, h),
            ):
                self.create_oval(x0, y0, x1, y1, fill=self["bg"], outline="")
        self.create_text(
            w // 2, h // 2, text=self._label, fill=fg, font=self._font,
        )

    def _on_enter(self, _e: tk.Event) -> None:
        if not self._enabled:
            return
        self._bg, self._bg = self._hover, self._hover
        self._draw()

    def _on_leave(self, _e: tk.Event) -> None:
        variant_bg = {
            "primary": ACCENT, "success": OK, "danger": ERR,
            "ghost": SURFACE_2, "outline": SURFACE,
        }.get(self._variant_name(), SURFACE_2)
        self._bg = variant_bg
        self._draw()

    def _variant_name(self) -> str:
        for name, (bg, _fg, _h) in self._VARIANTS.items():
            if bg == self._bg or _fg == self._fg:
                return name
        return "ghost"

    def _on_click(self, _e: tk.Event) -> None:
        if self._enabled and self._cmd:
            self._cmd()

    # -- API ---------------------------------------------------------------

    def set_text(self, text: str) -> None:
        self._label = text
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_command(self, cmd: Callable[[], None] | None) -> None:
        self._cmd = cmd


class IconButton(tk.Canvas):
    """Nút tròn nhỏ, dùng cho hành động cột (×, ⟳, ▸)."""

    def __init__(
        self,
        master: tk.Misc,
        glyph: str,
        command: Callable[[], None] | None = None,
        *,
        size: int = 24,
        fg: str = TEXT_DIM,
        fg_hover: str = TEXT,
        tooltip: str = "",
        **kw: Any,
    ) -> None:
        super().__init__(
            master, width=size, height=size, highlightthickness=0, bd=0,
            bg=kw.pop("parent_bg", SURFACE), cursor="hand2", **kw
        )
        self._glyph = glyph
        self._cmd = command
        self._size = size
        self._fg = fg
        self._fg_hover = fg_hover
        self._tooltip = tooltip
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def _draw(self, fg: str | None = None) -> None:
        self.delete("all")
        c = fg or self._fg
        s = self._size
        self.create_text(
            s // 2, s // 2, text=self._glyph, fill=c, font=_font(9),
        )

    def _on_enter(self, _e: tk.Event) -> None:
        self._draw(self._fg_hover)

    def _on_leave(self, _e: tk.Event) -> None:
        self._draw()

    def _on_click(self, _e: tk.Event) -> None:
        if self._cmd:
            self._cmd()

    def set_command(self, cmd: Callable[[], None] | None) -> None:
        self._cmd = cmd


# ---------------------------------------------------------------------------
# Badge / Pill
# ---------------------------------------------------------------------------

def badge_color(text: str) -> str:
    """Màu badge theo nội dung (dùng cho trạng thái)."""
    t = (text or "").upper()
    if t in STATE_COLORS:
        return STATE_COLORS[t]
    if "OK" in t or "ALIVE" in t or "DONE" in t or "SỐNG" in t:
        return OK
    if "FAIL" in t or "ERROR" in t or "LỖI" in t or "DEAD" in t:
        return ERR
    if "RUN" in t or "ĐANG" in t:
        return RUN
    if "WARN" in t or "CẢNH" in t:
        return WARN
    if "QUEUE" in t or "HÀNG" in t:
        return QUEUE
    return TEXT_DIM


class Badge(tk.Canvas):
    """Pill trạng thái: nền tô nhạt + chữ màu, bo tròn."""

    def __init__(
        self,
        master: tk.Misc,
        text: str = "",
        *,
        color: str | None = None,
        parent_bg: str = SURFACE,
        font: tuple[str, int, str] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(master, height=22, highlightthickness=0, bd=0,
                         bg=parent_bg, **kw)
        self._text = text
        self._color = color or badge_color(text)
        self._font = font or _font(8, "bold")
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        if not self._text:
            self.configure(width=1)
            return
        c = self._color
        # Nền pill: màu pha 18% với nền app → tự tính để không cần alpha.
        pad = 8
        w = max(
            self.winfo_width(),
            tkfont.Font(font=self._font).measure(self._text) + pad * 2,
        )
        h = 20
        self.configure(width=w, height=h)
        self.create_rectangle(0, 0, w, h, fill=_mix(c, BG, 0.82), outline="")
        self.create_text(
            w // 2, h // 2, text=self._text, fill=c, font=self._font,
        )

    def set(self, text: str, color: str | None = None) -> None:
        self._text = text
        if color:
            self._color = color
        else:
            self._color = badge_color(text)
        self._draw()


def _mix(color: str, other: str, ratio: float) -> str:
    """Trộn 2 màu hex. `ratio` = trọng số của `color` (0..1)."""
    def parse(c: str) -> tuple[int, int, int]:
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    r1, g1, b1 = parse(color)
    r2, g2, b2 = parse(other)
    f = ratio
    return "#%02x%02x%02x" % (
        int(r1 * f + r2 * (1 - f)),
        int(g1 * f + g2 * (1 - f)),
        int(b1 * f + b2 * (1 - f)),
    )


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

class ProgressBar(tk.Canvas):
    """Thanh tiến trình ngang, bo tròn, có nhãn %."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        width: int = 160,
        height: int = 8,
        color: str = ACCENT,
        parent_bg: str = SURFACE,
        show_label: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(
            master, width=width, height=height, highlightthickness=0, bd=0,
            bg=parent_bg, **kw
        )
        self._color = color
        self._value = 0.0
        self._show_label = show_label
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def set_value(self, pct: float) -> None:
        self._value = max(0.0, min(100.0, float(pct)))
        self._draw()

    def set_color(self, color: str) -> None:
        self._color = color
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w = int(self["width"])
            h = int(self["height"])
        r = h // 2
        self.create_rectangle(0, 0, w, h, fill=SURFACE_3, outline="")
        fill_w = int(w * self._value / 100.0)
        if fill_w > 0:
            self.create_rectangle(0, 0, max(fill_w, h), h, fill=self._color, outline="")
        if self._show_label:
            self.create_text(
                w + 8, h // 2, text=f"{self._value:.0f}%", fill=TEXT_DIM,
                font=_font(8), anchor="w",
            )


# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------

class Sparkline(tk.Canvas):
    """Đường nhỏ để vẽ headroom quota theo thời gian."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        width: int = 120,
        height: int = 28,
        color: str = ACCENT,
        parent_bg: str = SURFACE,
        **kw: Any,
    ) -> None:
        super().__init__(
            master, width=width, height=height, highlightthickness=0, bd=0,
            bg=parent_bg, **kw
        )
        self._points: list[float] = []
        self._color = color
        self.bind("<Configure>", lambda _e: self._draw())

    def set_points(self, points: list[float]) -> None:
        self._points = list(points)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        if len(self._points) < 2:
            return
        pad = 2
        lo, hi = min(self._points), max(self._points)
        span = (hi - lo) or 1.0
        step = (w - pad * 2) / (len(self._points) - 1)
        coords: list[float] = []
        for i, v in enumerate(self._points):
            x = pad + i * step
            y = h - pad - ((v - lo) / span) * (h - pad * 2)
            coords += [x, y]
        self.create_line(*coords, fill=self._color, width=1.5, smooth=True)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

class Table(tk.Frame):
    """Bảng dữ liệu tự vẽ.

    Vì sao không dùng `ttk.Treeview`: ta cần badge bo tròn trong ô, cột
    co giãn theo nội dung, hover theo hàng, và bảng **cao bằng 0** để các
    card bên dưới không bị đẩy. `Treeview` không làm được.

    API:
        set_columns([(key, title, width, align)], stretch_last=True)
        set_rows([{key: value, ...}])
        value_col = "state"  # cột dùng để tô màu chữ
        badge_col = "state"  # cột dùng để vẽ pill
        on_select(row_dict | None)
        on_row_action(key, row_dict)   # khi bấm nút ở cuối hàng
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        parent_bg: str = SURFACE,
        row_height: int = 34,
        on_select: Callable[[dict[str, Any] | None], None] | None = None,
        on_row_action: Callable[[str, dict[str, Any]], None] | None = None,
        empty_text: str = "Chưa có dữ liệu",
        **kw: Any,
    ) -> None:
        super().__init__(master, bg=parent_bg, **kw)
        self._parent_bg = parent_bg
        self._row_height = row_height
        self._columns: list[tuple[str, str, int, str]] = []
        self._rows: list[dict[str, Any]] = []
        self._selected: int | None = None
        self._hover: int | None = None
        self._on_select = on_select
        self._on_row_action = on_row_action
        self._empty_text = empty_text
        self._value_col: str | None = None
        self._badge_col: str | None = None

        self._header = tk.Canvas(
            self, height=32, highlightthickness=0, bd=0, bg=parent_bg
        )
        self._header.pack(fill="x")
        self._body = tk.Canvas(
            self, highlightthickness=0, bd=0, bg=parent_bg
        )
        self._body.pack(fill="both", expand=True)

        self._header.bind("<Configure>", lambda _e: self._draw_header())
        self._body.bind("<Configure>", lambda _e: self._draw_body())
        self._body.bind("<Motion>", self._on_motion)
        self._body.bind("<Leave>", lambda _e: self._set_hover(None))
        self._body.bind("<Button-1>", self._on_click)

    # -- cấu hình ----------------------------------------------------------

    def set_columns(
        self,
        columns: list[tuple[str, str, int, str]],
        *,
        stretch_last: bool = True,
    ) -> None:
        """`columns` = [(key, title, width, align)] — align: 'w' | 'e' | 'c'."""
        self._columns = columns
        self._stretch_last = stretch_last
        self._draw_header()
        self._draw_body()

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        if self._selected is not None and self._selected >= len(rows):
            self._selected = None
        self._draw_body()

    def set_value_column(self, key: str | None) -> None:
        self._value_col = key

    def set_badge_column(self, key: str | None) -> None:
        self._badge_col = key

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self._draw_body()

    def selected_row(self) -> dict[str, Any] | None:
        if self._selected is None or self._selected >= len(self._rows):
            return None
        return self._rows[self._selected]

    def select_row(self, index: int) -> None:
        self._selected = index if 0 <= index < len(self._rows) else None
        self._draw_body()
        if self._on_select:
            self._on_select(self.selected_row())

    # -- vẽ ---------------------------------------------------------------

    def _col_x(self, index: int) -> tuple[int, int]:
        """Trả về (x0, x1) của cột thứ `index`."""
        x = 0
        for i, (_k, _t, w, _a) in enumerate(self._columns):
            if i == index:
                return x, x + w
            x += w
        return x, x

    def _draw_header(self) -> None:
        c = self._header
        c.delete("all")
        w = c.winfo_width()
        if w <= 1:
            return
        c.create_line(0, 31, w, 31, fill=BORDER)
        for i, (key, title, cw, align) in enumerate(self._columns):
            x0, x1 = self._col_x(i)
            if align == "e":
                c.create_text(x1 - 10, 16, text=title, fill=TEXT_FAINT,
                              font=_font(8, "bold"), anchor="e")
            elif align == "c":
                c.create_text((x0 + x1) // 2, 16, text=title, fill=TEXT_FAINT,
                              font=_font(8, "bold"), anchor="center")
            else:
                c.create_text(x0 + 10, 16, text=title, fill=TEXT_FAINT,
                              font=_font(8, "bold"), anchor="w")

    def _row_badge(self, row: dict[str, Any]) -> str | None:
        if not self._badge_col:
            return None
        v = row.get(self._badge_col)
        return str(v) if v else None

    def _draw_body(self) -> None:
        c = self._body
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1:
            return
        if not self._rows:
            c.create_text(
                w // 2, h // 2, text=self._empty_text, fill=TEXT_FAINT,
                font=_font(9),
            )
            return

        visible = max(1, h // self._row_height)
        start = 0
        if self._selected is not None and self._selected >= visible:
            start = self._selected - visible + 1

        for i in range(start, min(len(self._rows), start + visible)):
            row = self._rows[i]
            y0 = i * self._row_height
            y1 = y0 + self._row_height
            if i == self._selected:
                c.create_rectangle(0, y0, w, y1, fill=SURFACE_2, outline="")
                c.create_rectangle(0, y0, 3, y1, fill=ACCENT, outline="")
            elif i == self._hover:
                c.create_rectangle(0, y0, w, y1, fill=SURFACE_2, outline="")
            c.create_line(0, y1, w, y1, fill=BORDER_SOFT)

            badge = self._row_badge(row)
            badge_key = self._badge_col
            for j, (key, _t, cw, align) in enumerate(self._columns):
                x0, x1 = self._col_x(j)
                val = row.get(key)
                text = "" if val is None else str(val)
                if badge is not None and key == badge_key:
                    col = badge_color(text)
                    bw = tkfont.Font(font=_font(8, "bold")).measure(text) + 16
                    bx = x0 + 10
                    c.create_rectangle(
                        bx, y0 + 7, bx + bw, y1 - 7,
                        fill=_mix(col, BG, 0.82), outline="",
                    )
                    c.create_text(
                        bx + bw // 2, (y0 + y1) // 2, text=text, fill=col,
                        font=_font(8, "bold"),
                    )
                    continue

                color = TEXT
                if self._value_col == key:
                    color = badge_color(text)
                elif j == 0:
                    color = TEXT
                else:
                    color = TEXT_DIM

                max_chars = max(4, int((cw - 20) / 7))
                if len(text) > max_chars:
                    text = text[: max_chars - 1] + "…"

                if align == "e":
                    c.create_text(x1 - 10, (y0 + y1) // 2, text=text, fill=color,
                                  font=_font(9), anchor="e")
                elif align == "c":
                    c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=text,
                                  fill=color, font=_font(9), anchor="center")
                else:
                    c.create_text(x0 + 10, (y0 + y1) // 2, text=text, fill=color,
                                  font=_font(9), anchor="w")

    # -- tương tác --------------------------------------------------------

    def _set_hover(self, index: int | None) -> None:
        if index != self._hover:
            self._hover = index
            self._draw_body()

    def _on_motion(self, event: tk.Event) -> None:
        idx = event.y // self._row_height
        if idx < 0 or idx >= len(self._rows):
            self._set_hover(None)
        else:
            self._set_hover(idx)

    def _on_click(self, event: tk.Event) -> None:
        idx = event.y // self._row_height
        if idx < 0 or idx >= len(self._rows):
            return
        self.select_row(idx)


# ---------------------------------------------------------------------------
# Scrollable frame
# ---------------------------------------------------------------------------

class ScrollFrame(tk.Frame):
    """Container cuộn dọc (Canvas + Frame + Scrollbar)."""

    def __init__(self, master: tk.Misc, *, bg: str = BG, **kw: Any) -> None:
        super().__init__(master, bg=bg, **kw)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._bar = tk.Scrollbar(
            self, orient="vertical", command=self._canvas.yview,
            bg=SURFACE_3, troughcolor=BG, activebackground=SURFACE_3,
            relief="flat", width=10, bd=0,
        )
        self._bar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._inner = tk.Frame(self._canvas, bg=bg)
        self._win = self._canvas.create_window((0, 0), window=self._inner,
                                               anchor="nw")
        self._inner.bind("<Configure>", self._on_inner)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.configure(yscrollcommand=self._bar.set)

        self._canvas.bind("<MouseWheel>", self._on_wheel)
        self._inner.bind("<MouseWheel>", self._on_wheel)

    def _on_inner(self, _e: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        self._canvas.itemconfigure(self._win, width=event.width)

    def _on_wheel(self, event: tk.Event) -> None:
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    @property
    def inner(self) -> tk.Frame:
        return self._inner


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------

class Modal(tk.Toplevel):
    """Hộp thoại modal tối, căn giữa cửa sổ cha."""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        *,
        width: int = 560,
        height: int = 420,
        subtitle: str = "",
        **kw: Any,
    ) -> None:
        super().__init__(master, **kw)
        self.withdraw()
        self.title(title)
        self.configure(bg=SURFACE)
        self.resizable(True, True)
        self.transient(master)

        self._shell = tk.Frame(self, bg=SURFACE)
        self._shell.pack(fill="both", expand=True)

        head = tk.Frame(self._shell, bg=SURFACE)
        head.pack(fill="x", padx=20, pady=(18, 0))
        tk.Label(head, text=title, bg=SURFACE, fg=TEXT,
                 font=_font(13, "bold")).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, bg=SURFACE, fg=TEXT_DIM,
                     font=_font(9)).pack(side="left", padx=(10, 0))

        self.body = tk.Frame(self._shell, bg=SURFACE)
        self.body.pack(fill="both", expand=True, padx=20, pady=(14, 0))

        self.footer = tk.Frame(self._shell, bg=SURFACE)
        self.footer.pack(fill="x", padx=20, pady=(0, 18))

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(width, height)
        self.deiconify()
        self.grab_set()
        self.focus_set()

    def _center(self, width: int, height: int) -> None:
        try:
            px, py = self.master.winfo_rootx(), self.master.winfo_rooty()
            pw, ph = self.master.winfo_width(), self.master.winfo_height()
        except tk.TclError:
            px = py = 0
            pw, ph = 1200, 800
        if pw <= 1:
            pw, ph = 1200, 800
        x = px + max(0, (pw - width) // 2)
        y = py + max(0, (ph - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def add_button(
        self, text: str, command: Callable[[], None], *, variant: str = "ghost"
    ) -> Button:
        b = Button(self.footer, text, command, variant=variant,
                   width=110, height=32, parent_bg=SURFACE)
        b.pack(side="right", padx=(8, 0))
        return b


# ---------------------------------------------------------------------------
# Toast
# ---------------------------------------------------------------------------

class Toast(tk.Toplevel):
    """Thông báo nhỏ góc dưới-phải, tự tắt."""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        *,
        kind: str = "ok",
        duration: int = 3200,
        **kw: Any,
    ) -> None:
        super().__init__(master, **kw)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        color = {"ok": OK, "err": ERR, "warn": WARN, "info": ACCENT}.get(kind, ACCENT)

        f = tk.Frame(self, bg=SURFACE, highlightthickness=1,
                     highlightbackground=BORDER)
        f.pack(fill="both", expand=True)
        tk.Frame(f, bg=color, width=4).pack(side="left", fill="y")
        tk.Label(f, text=text, bg=SURFACE, fg=TEXT, font=_font(9),
                 justify="left", wraplength=340).pack(side="left", padx=12, pady=10)

        self.update_idletasks()
        w = f.winfo_reqwidth()
        h = f.winfo_reqheight()
        try:
            px, py = master.winfo_rootx(), master.winfo_rooty()
            pw, ph = master.winfo_width(), master.winfo_height()
        except tk.TclError:
            px = py = 0
            pw, ph = 1200, 800
        x = px + max(0, pw - w - 24)
        y = py + max(0, ph - h - 24)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.after(duration, self.destroy)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def toast(master: tk.Misc, text: str, kind: str = "ok") -> None:
    """Hiện toast (không chặn)."""
    try:
        Toast(master, text, kind=kind)
    except tk.TclError:
        pass


def scrollbar(master: tk.Misc, command: Callable[[], None]) -> tk.Scrollbar:
    """Scrollbar tối chuẩn."""
    return tk.Scrollbar(
        master, orient="vertical", command=command,
        bg=SURFACE_3, troughcolor=BG, activebackground=SURFACE_3,
        relief="flat", width=10, bd=0,
    )


def monospace(size: int = 9) -> tuple[str, int, str]:
    return (FONT_MONO, size, "normal")


def truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_duration(seconds: float) -> str:
    """Định dạng thời lượng gọn: `45s`, `3m20s`, `1h05m`."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def fmt_ago(ts: float | None) -> str:
    """Thời gian tương đối: `12s trước`, `3m trước`, `2h trước`."""
    import time

    if not ts:
        return "—"
    d = time.time() - ts
    if d < 60:
        return f"{int(d)}s trước"
    if d < 3600:
        return f"{int(d // 60)}m trước"
    if d < 86400:
        return f"{int(d // 3600)}h trước"
    return f"{int(d // 86400)}d trước"
