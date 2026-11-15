"""Popup nhập cookie JSON — dán trực tiếp, không cần file tạm.

Vì sao popup riêng
------------------
Trước đây luồng nhập cookie là: mở Cookie-Editor → export file .json → trỏ
vào hộp thoại chọn file → adopt. Nhiều bước, dễ chọn nhầm file, và không có
gì để xác nhận trước khi ghi.

Popup này: user copy JSON trong clipboard → bấm "Nhập cookie" → popup mở
sẵn **nội dung clipboard** → bấm "Lưu" → cấy thẳng vào profile Chrome riêng
của account + ghi pool file.

Nút "Dán từ clipboard" đọc clipboard Windows (tkinter `clipboard_get`).

Ràng buộc
---------
- Không ghi mật khẩu/cookie vào log.
- Text trong textarea là secret → xoá khi đóng popup.
- Validate ngay khi paste: parse JSON, kiểm tra có `__Secure-1PSID` không,
  báo rõ thiếu gì — **trước** khi đụng Chrome.
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from orchestrator import ui

#: Cookie bắt buộc cho phiên Gemini.
REQUIRED = "__Secure-1PSID"


class CookiePastePopup(ui.Modal):
    """Popup dán cookie JSON cho 1 account.

    Args:
        master: cửa sổ cha.
        account: tên account.
        email: email (để ghi vào pool file).
        pool: tên pool (mặc định suy ra từ account).
        on_save: callback `(account, raw_text) -> None`, chạy **ngoài** popup
            (popup tự đóng trước) vì thao tác cấy cookie mất vài giây và
            có thể mở Chrome.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        account: str,
        email: str = "",
        pool: str = "",
        on_save: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(
            master,
            "Nhập cookie Gemini",
            width=640,
            height=520,
            subtitle=f"account: {account}",
        )
        self.account = account
        self.email = email
        self.pool = pool
        self._on_save = on_save

        self._build_body()
        self._build_footer()

        # Tự động dán clipboard nếu có nội dung JSON hợp lệ.
        self.after(120, self._prefill_from_clipboard)

    # -- UI ---------------------------------------------------------------

    def _build_body(self) -> None:
        body = self.body

        hint = tk.Frame(body, bg=ui.SURFACE)
        hint.pack(fill="x")
        tk.Label(
            hint,
            text=(
                "Cách lấy: mở tab gemini.google.com đã đăng nhập → Cookie-Editor → "
                "Export → copy toàn bộ JSON → dán vào ô bên dưới."
            ),
            bg=ui.SURFACE, fg=ui.TEXT_DIM, font=ui._font(9),
            wraplength=580, justify="left",
        ).pack(anchor="w")

        # Textarea
        wrap = tk.Frame(body, bg=ui.SURFACE)
        wrap.pack(fill="both", expand=True, pady=(12, 0))

        self.text = tk.Text(
            wrap, height=14, wrap="none", bg=ui.SURFACE_2, fg=ui.TEXT,
            insertbackground=ui.TEXT, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=ui.BORDER,
            highlightcolor=ui.ACCENT, font=ui.monospace(9),
            undo=True,
        )
        vbar = ui.scrollbar(wrap, self.text.yview)
        hbar = tk.Scrollbar(wrap, orient="horizontal", command=self.text.xview,
                             bg=ui.SURFACE_3, troughcolor=ui.SURFACE_2,
                             activebackground=ui.SURFACE_3, relief="flat",
                             width=10, bd=0)
        self.text.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        # Thanh trạng thái validate
        status = tk.Frame(body, bg=ui.SURFACE)
        status.pack(fill="x", pady=(10, 0))

        self.status_dot = tk.Canvas(status, width=10, height=10, bg=ui.SURFACE,
                                    highlightthickness=0)
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(status, text="Chưa có nội dung.",
                                    bg=ui.SURFACE, fg=ui.TEXT_DIM, font=ui._font(9))
        self.status_label.pack(side="left", padx=(8, 0))

        # Nút dán clipboard
        ui.Button(
            status, "Dán từ clipboard", self._paste_from_clipboard,
            variant="outline", width=140, height=28, parent_bg=ui.SURFACE,
        ).pack(side="right")

        # Ô email (tuỳ chọn)
        form = tk.Frame(body, bg=ui.SURFACE)
        form.pack(fill="x", pady=(12, 0))
        tk.Label(form, text="Email:", bg=ui.SURFACE, fg=ui.TEXT_DIM,
                 font=ui._font(9)).pack(side="left")
        self.entry_email = tk.Entry(form, bg=ui.SURFACE_2, fg=ui.TEXT,
                                    insertbackground=ui.TEXT, relief="flat",
                                    bd=0, highlightthickness=1,
                                    highlightbackground=ui.BORDER,
                                    highlightcolor=ui.ACCENT)
        self.entry_email.insert(0, self.email)
        self.entry_email.pack(side="left", fill="x", expand=True, padx=(8, 12))
        tk.Label(form, text="Pool:", bg=ui.SURFACE, fg=ui.TEXT_DIM,
                 font=ui._font(9)).pack(side="left")
        self.entry_pool = tk.Entry(form, bg=ui.SURFACE_2, fg=ui.TEXT,
                                   insertbackground=ui.TEXT, relief="flat",
                                   bd=0, highlightthickness=1,
                                   highlightbackground=ui.BORDER,
                                   highlightcolor=ui.ACCENT, width=14)
        self.entry_pool.insert(0, self.pool or self.account)
        self.entry_pool.pack(side="left", padx=(8, 0))

        self.text.bind("<<Modified>>", self._on_modified)

    def _build_footer(self) -> None:
        self.btn_save = ui.Button(
            self.footer, "Lưu & cấy cookie", self._on_save_click,
            variant="primary", width=150, height=34, parent_bg=ui.SURFACE,
        )
        self.btn_save.pack(side="right")

        ui.Button(
            self.footer, "Huỷ", self.destroy,
            variant="ghost", width=90, height=34, parent_bg=ui.SURFACE,
        ).pack(side="right", padx=(8, 0))

        tk.Label(
            self.footer,
            text=(
                "Cookie được cấy vào profile Chrome riêng của account và lưu "
                "bản gốc vào thư mục cookie_files/."
            ),
            bg=ui.SURFACE, fg=ui.TEXT_FAINT, font=ui._font(8),
        ).pack(side="left")

    # -- logic ------------------------------------------------------------

    def _on_modified(self, _e: tk.Event) -> None:
        self.text.edit_modified(False)
        self._validate()

    def _validate(self) -> tuple[bool, str]:
        """Kiểm tra JSON trong textarea. Trả `(ok, message)`."""
        raw = self.text.get("1.0", "end").strip()
        if not raw:
            self._set_status(ui.TEXT_FAINT, "Chưa có nội dung.")
            return False, ""
        try:
            data = json.loads(raw)
        except ValueError as e:
            self._set_status(ui.ERR, f"JSON không hợp lệ: {e}")
            return False, ""

        items = data
        if isinstance(data, dict):
            for k in ("data", "items", "cookies", "result"):
                if isinstance(data.get(k), (list, dict)):
                    items = data[k]
                    break

        names: set[str] = set()
        if isinstance(items, dict):
            names = {k for k, v in items.items() if isinstance(v, str)}
        elif isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("name"):
                    names.add(str(it["name"]))

        if REQUIRED not in names:
            self._set_status(
                ui.ERR,
                f"Thiếu cookie '{REQUIRED}'. Cần export từ tab gemini.google.com.",
            )
            return False, ""

        extra = [n for n in names if n.startswith("__Secure-") or n in ("SAPISID", "SSID")]
        self._set_status(
            ui.OK, f"Hợp lệ — {len(names)} cookie, có {REQUIRED} (+{len(extra)} khác)."
        )
        return True, raw

    def _set_status(self, color: str, message: str) -> None:
        self.status_label.config(text=message, fg=color)
        self.status_dot.delete("all")
        self.status_dot.create_oval(
            1, 1, 9, 9, fill=color if message else ui.TEXT_FAINT, outline=""
        )

    def _paste_from_clipboard(self) -> None:
        try:
            content = self.clipboard_get()
        except tk.TclError:
            ui.toast(self, "Clipboard trống hoặc không đọc được.", "warn")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_modified(False)
        self._validate()

    def _prefill_from_clipboard(self) -> None:
        """Tự dán clipboard nếu hợp lệ (tiết kiệm 1 click)."""
        try:
            content = self.clipboard_get()
        except tk.TclError:
            return
        if not content or "__Secure-1PSID" not in content:
            return
        self.text.insert("1.0", content)
        self.text.edit_modified(False)
        self._validate()

    def _on_save_click(self) -> None:
        ok, raw = self._validate()
        if not ok:
            if not raw:
                ui.toast(self, "Hãy dán JSON cookie trước.", "warn")
            else:
                ui.toast(self, "JSON chưa hợp lệ — xem ghi chú bên dưới.", "err")
            return
        email = self.entry_email.get().strip()
        pool = self.entry_pool.get().strip()
        account = self.account
        # Đóng trước để callback chạy nền không bị chặn bởi grab_set.
        self.destroy()
        if self._on_save:
            self._on_save(account, raw)
