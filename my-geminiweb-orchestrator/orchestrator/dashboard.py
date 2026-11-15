"""Dashboard Orchestrator — tối ưu cho điều phối đa tác vụ Gemini song song.

Vì sao thiết kế lại:
-------------------
Bản GUI cũ chỉ hiển thị danh sách đơn tuyến, khó theo dõi khi người dùng kích hoạt
nhiều tác vụ Gemini (Deep Research, Chat, Canvas) cùng lúc trên nhiều tài khoản.
Dashboard này phân tách rõ ràng 3 khu vực:
1. Launch bar: Cho phép phóng đơn hoặc song song nhiều tác vụ (Chạy ×N, Chạy song song).
2. Khu giữa: Theo dõi đồng thời các tiến trình đang thực thi (bên trái) và công suất
   headroom của các tài khoản (bên phải).
3. Khu dưới: Nhật ký thực thi trực tiếp (stream log) và tương tác với báo cáo đầu ra.

Nguyên tắc:
----------
- Không bao giờ chặn giao diện chính: Mọi tác vụ có I/O hoặc gọi trình duyệt đều đưa vào thread nền.
- Cập nhật thông minh: So sánh snapshot để tránh giật giao diện và bảo toàn lựa chọn của người dùng.
- Hoàn toàn độc lập với theme hệ điều hành: Sử dụng bảng màu tối nhất quán từ ui.py.
"""

from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable

from orchestrator import (
    accounts as account_registry,
    auto_login,
    board,
    chrome_profile,
    cookie_popup,
    cookie_sync,
    dispatch,
    store,
    ui,
)

# Danh sách các loại task hỗ trợ khởi chạy từ giao diện
TASK_KINDS: list[tuple[str, str]] = [
    ("chat", "Chat (Hội thoại)"),
    ("deep_research", "Deep Research (Nghiên cứu sâu)"),
    ("canvas", "Canvas (Giao diện web)"),
    ("antigravity", "Antigravity (Mã nguồn & Shell)"),
    ("canvas_api", "Canvas API (Dự phòng)"),
]

KIND_DISPLAY: dict[str, str] = {k: label.split(" ")[0] for k, label in TASK_KINDS}


class Dashboard(tk.Frame):
    """Panel điều phối chính nhúng trong cửa sổ OrchestratorGUI."""

    def __init__(self, master: tk.Misc, app: Any = None, **kw: Any) -> None:
        super().__init__(master, bg=ui.BG, **kw)
        self.app = app

        # Trạng thái điều hướng và chọn dòng
        self._selected_job_id: str | None = None
        self._selected_account_name: str | None = None
        self._active_jobs_tab: str = "active"  # 'active' hoặc 'recent'
        self._warned_low_headroom: bool = False
        self._last_rendered_event_id: int = -1
        self._last_jobs_sig: Any = None
        self._last_accounts_sig: Any = None
        self._poll_timer: str | None = None
        self._snap: dict[str, Any] = {}

        # Dựng các phân khu giao diện chính:
        # Launch ở trên (top), Log stream ở dưới (bottom), Khung giữa chiếm toàn bộ khoảng trống còn lại (expand=True)
        self._build_launch_card()
        self._build_log_card()
        self._build_middle_frame()

        # Tự động pack vào master nếu chưa được quản lý bởi geometry manager
        # giúp nhúng an toàn trong mọi môi trường (kể cả script kiểm thử tự động)
        if not self.winfo_manager():
            self.pack(fill="both", expand=True)

        # Nếu cửa sổ gốc bị withdraw (chế độ chạy ngầm / test tự động không hiển thị màn hình),
        # kích hoạt map tạm thời để Tk tính toán đầy đủ geometry và winfo_width cho các panel con.
        try:
            top = self.winfo_toplevel()
            if top and top.wm_state() == "withdrawn":
                top.deiconify()
                top.update()
                top.withdraw()
        except Exception:
            pass

        # Dọn timer định kỳ khi widget bị huỷ để tránh rò rỉ tài nguyên Tk
        self.bind("<Destroy>", self._on_destroy)

        # Nạp dữ liệu lần đầu và kích hoạt vòng lặp polling 1.5s
        self.refresh(force=False)
        self._schedule_poll()

    def _on_destroy(self, _e: tk.Event) -> None:
        """Huỷ hẹn giờ poll khi dashboard đóng."""
        if self._poll_timer:
            try:
                self.after_cancel(self._poll_timer)
            except Exception:
                pass
            self._poll_timer = None

    # ------------------------------------------------------------------
    # 1. Khu Khởi chạy (Launch Bar)
    # ------------------------------------------------------------------

    def _build_launch_card(self) -> None:
        """Thanh submit nhanh: hỗ trợ chạy đơn, chạy ×N và chạy song song trên nhiều pool."""
        self.launch_card = ui.Card(self, bg=ui.SURFACE)
        self.launch_card.pack(fill="x", padx=12, pady=(10, 6))

        # Tiêu đề và thanh chọn cấu hình
        top_row = tk.Frame(self.launch_card, bg=ui.SURFACE)
        top_row.pack(fill="x", padx=12, pady=(8, 4))

        ui.SectionTitle(
            top_row,
            "Khởi chạy tác vụ",
            sub="Phóng một hoặc nhiều Gemini song song độc lập",
            bg=ui.SURFACE,
        ).pack(side="left")

        # Cấu hình Pool
        tk.Label(
            top_row, text="Pool:", bg=ui.SURFACE, fg=ui.TEXT_DIM, font=ui._font(9)
        ).pack(side="right", padx=(10, 4))
        self.pool_combo = ttk.Combobox(
            top_row, state="readonly", width=22, style="TCombobox"
        )
        self.pool_combo.pack(side="right")
        self.pool_combo.set("auto (Tự động chọn)")

        # Cấu hình Loại task
        tk.Label(
            top_row, text="Loại:", bg=ui.SURFACE, fg=ui.TEXT_DIM, font=ui._font(9)
        ).pack(side="right", padx=(14, 4))
        self.kind_combo = ttk.Combobox(
            top_row,
            state="readonly",
            values=[label for _, label in TASK_KINDS],
            width=24,
            style="TCombobox",
        )
        self.kind_combo.pack(side="right")
        self.kind_combo.current(0)

        # Ô nhập prompt đa dòng
        body_row = tk.Frame(self.launch_card, bg=ui.SURFACE)
        body_row.pack(fill="x", padx=12, pady=(2, 6))

        self.prompt_text = tk.Text(
            body_row,
            height=3,
            bg=ui.SURFACE_2,
            fg=ui.TEXT,
            insertbackground=ui.TEXT,
            font=ui._font(9),
            relief="flat",
            highlightthickness=1,
            highlightbackground=ui.BORDER,
            highlightcolor=ui.ACCENT,
            padx=8,
            pady=6,
        )
        self.prompt_text.pack(fill="x", side="left", expand=True)

        # Thiết lập placeholder hỗ trợ người dùng nhận diện nhanh
        self._placeholder_text = "Nhập nội dung prompt cần xử lý (hỗ trợ nhiều dòng, ví dụ: Phân tích báo cáo tài chính...)"
        self._placeholder_active = True
        self.prompt_text.insert("1.0", self._placeholder_text)
        self.prompt_text.config(fg=ui.TEXT_FAINT)
        self.prompt_text.bind("<FocusIn>", self._on_prompt_focus_in)
        self.prompt_text.bind("<FocusOut>", self._on_prompt_focus_out)

        # Thanh nút hành động bên dưới prompt
        btn_row = tk.Frame(self.launch_card, bg=ui.SURFACE)
        btn_row.pack(fill="x", padx=12, pady=(0, 8))

        # Nhãn gợi ý điều phối
        tk.Label(
            btn_row,
            text="⚡ Đa luồng: Chạy cùng prompt trên nhiều tài khoản để tăng tốc và tránh rate-limit.",
            bg=ui.SURFACE,
            fg=ui.TEXT_FAINT,
            font=ui._font(8),
        ).pack(side="left")

        # Nút Chạy đơn
        self.btn_run = ui.Button(
            btn_row,
            "Chạy",
            self._on_submit_single,
            variant="primary",
            width=85,
            height=28,
            parent_bg=ui.SURFACE,
        )
        self.btn_run.pack(side="right", padx=(8, 0))

        # Bộ chọn N cho Chạy ×N
        self.spin_n = tk.Spinbox(
            btn_row,
            from_=2,
            to=10,
            width=3,
            bg=ui.SURFACE_2,
            fg=ui.TEXT,
            buttonbackground=ui.SURFACE_3,
            relief="flat",
            font=ui._font(9, "bold"),
        )
        self.spin_n.pack(side="right", padx=(4, 0))

        tk.Label(
            btn_row, text="Số lượng:", bg=ui.SURFACE, fg=ui.TEXT_DIM, font=ui._font(8)
        ).pack(side="right", padx=(8, 0))

        # Nút Chạy ×N
        self.btn_run_n = ui.Button(
            btn_row,
            "Chạy ×N",
            self._on_submit_n,
            variant="ghost",
            width=85,
            height=28,
            parent_bg=ui.SURFACE,
        )
        self.btn_run_n.pack(side="right", padx=(8, 0))

        # Nút Chạy song song (chọn danh sách pool tuỳ ý)
        self.btn_run_parallel = ui.Button(
            btn_row,
            "Chạy song song...",
            self._on_submit_parallel_modal,
            variant="outline",
            width=135,
            height=28,
            parent_bg=ui.SURFACE,
        )
        self.btn_run_parallel.pack(side="right", padx=(8, 0))

    def _on_prompt_focus_in(self, _e: tk.Event) -> None:
        """Xoá placeholder khi người dùng nhấn vào ô prompt."""
        if self._placeholder_active:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.config(fg=ui.TEXT)
            self._placeholder_active = False

    def _on_prompt_focus_out(self, _e: tk.Event) -> None:
        """Hiện lại placeholder nếu người dùng rời ô mà không nhập gì."""
        raw = self.prompt_text.get("1.0", "end-1c").strip()
        if not raw:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", self._placeholder_text)
            self.prompt_text.config(fg=ui.TEXT_FAINT)
            self._placeholder_active = True

    def _get_prompt(self) -> str:
        """Lấy chuỗi prompt thực tế, bỏ qua placeholder."""
        if self._placeholder_active:
            return ""
        return self.prompt_text.get("1.0", "end-1c").strip()

    def _clear_prompt(self) -> None:
        """Xoá sạch prompt sau khi submit thành công."""
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", self._placeholder_text)
        self.prompt_text.config(fg=ui.TEXT_FAINT)
        self._placeholder_active = True

    def _get_selected_kind(self) -> str:
        """Chuyển đổi nhãn hiển thị sang mã kind tương ứng trong hệ thống dispatch."""
        idx = self.kind_combo.current()
        if 0 <= idx < len(TASK_KINDS):
            return TASK_KINDS[idx][0]
        return "chat"

    def _get_selected_pool(self) -> str | None:
        """Lấy pool được chọn từ dropdown, None nghĩa là auto."""
        val = self.pool_combo.get().strip()
        if not val or val.startswith("auto"):
            return None
        return val

    # ------------------------------------------------------------------
    # 2. Khu giữa: 2 cột (Đang chạy & Account công suất)
    # ------------------------------------------------------------------

    def _build_middle_frame(self) -> None:
        """Khung giữa chứa 2 cột tỷ lệ ~62% trái (Job) và ~38% phải (Account)."""
        self.middle_frame = tk.Frame(self, bg=ui.BG)
        self.middle_frame.pack(side="top", fill="both", expand=True, padx=12, pady=2)

        # Sử dụng grid weight để phân chia tỷ lệ 62/38 co giãn mượt mà
        self.middle_frame.columnconfigure(0, weight=62)
        self.middle_frame.columnconfigure(1, weight=38)
        self.middle_frame.rowconfigure(0, weight=1)

        self._build_jobs_card()
        self._build_accounts_card()

    def _build_jobs_card(self) -> None:
        """Khu vực hiển thị danh sách các tác vụ đang chạy hoặc gần đây."""
        self.jobs_card = ui.Card(self.middle_frame, bg=ui.SURFACE)
        self.jobs_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        # Tiêu đề bảng công việc + bộ lọc tab
        header_row = tk.Frame(self.jobs_card, bg=ui.SURFACE)
        header_row.pack(fill="x", padx=12, pady=(10, 4))

        self.jobs_title = ui.SectionTitle(
            header_row,
            "Tiến trình công việc",
            sub="0 đang chạy · 0 xếp hàng",
            bg=ui.SURFACE,
        )
        self.jobs_title.pack(side="left")

        # Nút chuyển đổi giữa 'Đang chạy' và 'Lịch sử gần đây'
        btn_box = tk.Frame(header_row, bg=ui.SURFACE)
        btn_box.pack(side="right")

        self.btn_tab_active = ui.Button(
            btn_box,
            "Đang chạy",
            lambda: self._set_jobs_tab("active"),
            variant="primary",
            width=90,
            height=24,
            parent_bg=ui.SURFACE,
        )
        self.btn_tab_active.pack(side="left", padx=(0, 4))

        self.btn_tab_recent = ui.Button(
            btn_box,
            "Lịch sử (40)",
            lambda: self._set_jobs_tab("recent"),
            variant="ghost",
            width=90,
            height=24,
            parent_bg=ui.SURFACE,
        )
        self.btn_tab_recent.pack(side="left")

        # Banner báo hiệu khi chạy song song nhiều task
        self.concurrency_banner = tk.Label(
            self.jobs_card,
            text="",
            bg=ui.SURFACE_2,
            fg=ui.RUN,
            font=ui._font(8, "bold"),
            anchor="w",
            padx=10,
            pady=4,
        )

        # Bảng dữ liệu tác vụ tự vẽ bằng Canvas
        # Cột 'Đang làm gì' hiển thị last_event trực tiếp trên từng hàng để dễ quét nhanh
        self.jobs_table = ui.Table(
            self.jobs_card,
            parent_bg=ui.SURFACE,
            row_height=32,
            on_select=self._on_job_row_selected,
            empty_text="Không có tiến trình nào đang thực thi",
        )
        self.jobs_table.set_columns(
            [
                ("kind_label", "Loại", 95, "w"),
                ("state", "Trạng thái", 85, "c"),
                ("title", "Tiêu đề", 150, "w"),
                ("pool", "Pool", 70, "c"),
                ("duration_str", "Thời lượng", 75, "e"),
                ("what", "Đang làm gì", 230, "w"),
            ],
            stretch_last=True,
        )
        self.jobs_table.set_badge_column("state")
        self.jobs_table.pack(fill="both", expand=True, padx=10, pady=(4, 8))

    def _build_accounts_card(self) -> None:
        """Khu vực hiển thị tài khoản, phiên đăng nhập, quota và thanh headroom."""
        self.accounts_card = ui.Card(self.middle_frame, bg=ui.SURFACE)
        self.accounts_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # Tiêu đề card tài khoản
        header_row = tk.Frame(self.accounts_card, bg=ui.SURFACE)
        header_row.pack(fill="x", padx=12, pady=(10, 4))

        self.acc_title = ui.SectionTitle(
            header_row,
            "Tài khoản & Công suất",
            sub="Quản lý quota & phiên",
            bg=ui.SURFACE,
        )
        self.acc_title.pack(side="left")

        # Banner cảnh báo tài khoản mất phiên
        self.dead_acc_banner = tk.Label(
            self.accounts_card,
            text="",
            bg="#3b2211",
            fg=ui.WARN,
            font=ui._font(8, "bold"),
            anchor="w",
            padx=10,
            pady=3,
        )

        # Banner cảnh báo cạn kiệt Quota (<10%)
        self.low_quota_banner = tk.Label(
            self.accounts_card,
            text="",
            bg="#3b1111",
            fg=ui.ERR,
            font=ui._font(8, "bold"),
            anchor="w",
            padx=10,
            pady=3,
        )

        # Bảng tài khoản: hiển thị trực tiếp L1/L2 và trạng thái Session
        self.accounts_table = ui.Table(
            self.accounts_card,
            parent_bg=ui.SURFACE,
            row_height=30,
            on_select=self._on_account_row_selected,
            empty_text="Chưa có tài khoản nào trong hệ thống",
        )
        self.accounts_table.set_columns(
            [
                ("account", "Account", 85, "w"),
                ("pool", "Pool", 65, "w"),
                ("auth_badge", "Loại", 45, "c"),
                ("session_badge", "Session", 80, "c"),
                ("q_5h", "5h", 55, "e"),
                ("q_week", "Tuần", 55, "e"),
                ("headroom_str", "Còn", 50, "e"),
                ("last_seen", "Lần cuối", 70, "e"),
            ],
            stretch_last=True,
        )
        self.accounts_table.set_badge_column("session_badge")
        self.accounts_table.pack(fill="both", expand=True, padx=10, pady=(4, 6))

        # Panel chi tiết tài khoản đã chọn + nút hành động
        self.acc_actions_card = tk.Frame(
            self.accounts_card,
            bg=ui.SURFACE_2,
            highlightthickness=1,
            highlightbackground=ui.BORDER,
            padx=10,
            pady=8,
        )
        self.acc_actions_card.pack(fill="x", padx=10, pady=(0, 8))

        # Dòng thông tin chi tiết: Email & Loại tài khoản
        info_row = tk.Frame(self.acc_actions_card, bg=ui.SURFACE_2)
        info_row.pack(fill="x", pady=(0, 4))

        self.lbl_acc_name = tk.Label(
            info_row,
            text="Chọn tài khoản để thao tác",
            bg=ui.SURFACE_2,
            fg=ui.TEXT,
            font=ui._font(9, "bold"),
        )
        self.lbl_acc_name.pack(side="left")

        self.lbl_acc_meta = tk.Label(
            info_row, text="", bg=ui.SURFACE_2, fg=ui.TEXT_DIM, font=ui._font(8)
        )
        self.lbl_acc_meta.pack(side="right")

        # Thanh Headroom ProgressBar trực quan cho tài khoản đang chọn
        bar_row = tk.Frame(self.acc_actions_card, bg=ui.SURFACE_2)
        bar_row.pack(fill="x", pady=(2, 6))

        tk.Label(
            bar_row,
            text="Công suất còn lại:",
            bg=ui.SURFACE_2,
            fg=ui.TEXT_DIM,
            font=ui._font(8),
        ).pack(side="left", padx=(0, 6))

        self.acc_headroom_bar = ui.ProgressBar(
            bar_row,
            width=140,
            height=8,
            color=ui.OK,
            parent_bg=ui.SURFACE_2,
            show_label=True,
        )
        self.acc_headroom_bar.pack(side="left")

        # Hàng nút hành động theo tài khoản (R4)
        btn_action_row = tk.Frame(self.acc_actions_card, bg=ui.SURFACE_2)
        btn_action_row.pack(fill="x", pady=(4, 0))

        self.btn_paste_cookie = ui.Button(
            btn_action_row,
            "Nhập cookie",
            self._on_paste_cookie_clicked,
            variant="ghost",
            width=90,
            height=26,
            parent_bg=ui.SURFACE_2,
        )
        self.btn_paste_cookie.pack(side="left", padx=(0, 6))

        self.btn_auto_login = ui.Button(
            btn_action_row,
            "Đăng nhập tự động",
            self._on_auto_login_clicked,
            variant="ghost",
            width=120,
            height=26,
            parent_bg=ui.SURFACE_2,
        )
        self.btn_auto_login.pack(side="left", padx=(0, 6))

        self.btn_sync_cookie = ui.Button(
            btn_action_row,
            "Đồng bộ cookie",
            self._on_sync_cookie_clicked,
            variant="ghost",
            width=100,
            height=26,
            parent_bg=ui.SURFACE_2,
        )
        self.btn_sync_cookie.pack(side="left", padx=(0, 6))

        self.btn_chrome_profile = ui.Button(
            btn_action_row,
            "Tạo hồ sơ Chrome",
            self._on_chrome_profile_clicked,
            variant="ghost",
            width=110,
            height=26,
            parent_bg=ui.SURFACE_2,
        )
        self.btn_chrome_profile.pack(side="left")

    # ------------------------------------------------------------------
    # 3. Khu dưới: Nhật ký thực thi (Log Stream & Report)
    # ------------------------------------------------------------------

    def _build_log_card(self) -> None:
        """Panel chi tiết và stream log thời gian thực của tác vụ đang chọn."""
        self.log_card = ui.Card(self, bg=ui.SURFACE)
        self.log_card.pack(side="bottom", fill="x", padx=12, pady=(4, 10))

        # Header panel log
        top_row = tk.Frame(self.log_card, bg=ui.SURFACE)
        top_row.pack(fill="x", padx=12, pady=(8, 4))

        self.log_title = ui.SectionTitle(
            top_row,
            "Nhật ký thực thi (Log stream)",
            sub="Chọn một tác vụ ở trên để xem chi tiết",
            bg=ui.SURFACE,
        )
        self.log_title.pack(side="left")

        # Nút tương tác với job đang chọn
        btn_box = tk.Frame(top_row, bg=ui.SURFACE)
        btn_box.pack(side="right")

        self.btn_copy_id = ui.Button(
            btn_box,
            "Sao chép ID",
            self._on_copy_job_id,
            variant="ghost",
            width=90,
            height=26,
            parent_bg=ui.SURFACE,
        )
        self.btn_copy_id.pack(side="left", padx=(0, 6))

        self.btn_open_report = ui.Button(
            btn_box,
            "Mở report",
            self._on_open_report,
            variant="success",
            width=95,
            height=26,
            parent_bg=ui.SURFACE,
        )
        self.btn_open_report.pack(side="left", padx=(0, 6))

        self.btn_cancel_job = ui.Button(
            btn_box,
            "Huỷ job",
            self._on_cancel_job,
            variant="danger",
            width=85,
            height=26,
            parent_bg=ui.SURFACE,
        )
        self.btn_cancel_job.pack(side="left")

        # Khu vực Text hiển thị dòng log cuộn được
        text_container = tk.Frame(self.log_card, bg=ui.SURFACE)
        text_container.pack(fill="x", padx=12, pady=(4, 10))

        self.log_text = tk.Text(
            text_container,
            height=7,
            bg=ui.SURFACE_2,
            fg=ui.TEXT,
            insertbackground=ui.TEXT,
            font=ui.monospace(9),
            relief="flat",
            highlightthickness=1,
            highlightbackground=ui.BORDER,
            wrap="word",
            padx=10,
            pady=8,
        )
        self.log_scroll = ui.scrollbar(text_container, self.log_text.yview)
        self.log_text.config(yscrollcommand=self.log_scroll.set)

        self.log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # Định cấu hình màu sắc từng cấp độ log
        self.log_text.tag_configure(
            "ts", foreground=ui.TEXT_FAINT, font=ui.monospace(9)
        )
        self.log_text.tag_configure(
            "level_info", foreground=ui.ACCENT, font=ui.monospace(9)
        )
        self.log_text.tag_configure(
            "level_warn", foreground=ui.WARN, font=ui.monospace(9)
        )
        self.log_text.tag_configure(
            "level_error", foreground=ui.ERR, font=ui.monospace(9)
        )
        self.log_text.tag_configure(
            "level_ok", foreground=ui.OK, font=ui.monospace(9)
        )
        self.log_text.tag_configure(
            "msg", foreground=ui.TEXT, font=ui.monospace(9)
        )

        # Chặn gõ tay vào log stream
        self.log_text.config(state="disabled")

    # ------------------------------------------------------------------
    # 4. Polling & Cập nhật trạng thái thông minh (R6)
    # ------------------------------------------------------------------

    def _schedule_poll(self) -> None:
        """Lên lịch poll tiếp theo sau 1.5 giây."""
        try:
            self._poll_timer = self.after(1500, self._on_poll_tick)
        except Exception:
            pass

    def _on_poll_tick(self) -> None:
        """Tick định kỳ nhẹ: lấy snapshot không gọi mạng."""
        if not self.winfo_exists():
            return
        try:
            self.refresh(force=False)
        except Exception as e:
            # Bắt ngoại lệ để poll không bao giờ làm sập tiến trình UI
            print(f"[Dashboard Poll Error] {e}", file=sys.stderr)
        self._schedule_poll()

    def refresh(self, force: bool = False) -> None:
        """Cập nhật dữ liệu từ board.snapshot().

        force=True chỉ gọi khi người dùng bấm 'Làm mới quota' để ép quét mạng.
        """
        snap = board.snapshot(refresh_quota=force)
        self._snap = snap

        # Đồng bộ header shell nếu được nhúng vào OrchestratorGUI
        if self.app and hasattr(self.app, "update_summary"):
            try:
                self.app.update_summary(snap.get("summary") or {})
            except Exception:
                pass

        # Cập nhật danh sách pool ở Combobox nếu có thay đổi
        self._update_pool_combobox(snap)

        # Cập nhật bảng công việc
        self._update_jobs_ui(snap)

        # Cập nhật bảng tài khoản
        self._update_accounts_ui(snap)

        # Cập nhật panel log cho tác vụ đang chọn
        self._render_job_logs()

        # Kiểm tra ngưỡng cảnh báo quota và session (R8)
        self._check_alerts(snap)

    def _update_pool_combobox(self, snap: dict[str, Any]) -> None:
        """Nạp danh sách các pool khả dụng vào combobox."""
        accounts = snap.get("accounts") or []
        pools = sorted(
            list(
                {
                    str(acc.get("pool") or acc.get("account"))
                    for acc in accounts
                    if acc.get("pool") or acc.get("account")
                }
            )
        )
        current = self.pool_combo.get()
        new_values = ["auto (Tự động chọn)"] + pools
        if list(self.pool_combo["values"]) != new_values:
            self.pool_combo["values"] = new_values
            if current in new_values:
                self.pool_combo.set(current)
            else:
                self.pool_combo.set("auto (Tự động chọn)")

    def _set_jobs_tab(self, tab: str) -> None:
        """Chuyển đổi giữa tab 'Đang chạy' và 'Lịch sử gần đây'."""
        self._active_jobs_tab = tab
        if tab == "active":
            self.btn_tab_active._bg = ui.ACCENT
            self.btn_tab_active._fg = "#0b1220"
            self.btn_tab_recent._bg = ui.SURFACE_2
            self.btn_tab_recent._fg = ui.TEXT
        else:
            self.btn_tab_active._bg = ui.SURFACE_2
            self.btn_tab_active._fg = ui.TEXT
            self.btn_tab_recent._bg = ui.ACCENT
            self.btn_tab_recent._fg = "#0b1220"
        self.btn_tab_active._draw()
        self.btn_tab_recent._draw()
        self._update_jobs_ui(self._snap, force_redraw=True)

    def _update_jobs_ui(
        self, snap: dict[str, Any], force_redraw: bool = False
    ) -> None:
        """Cập nhật dữ liệu bảng công việc và banner song song."""
        jobs_dict = snap.get("jobs") or {}
        active_list = jobs_dict.get("active") or []
        recent_list = jobs_dict.get("recent") or []
        summary = snap.get("summary") or {}

        running_cnt = int(summary.get("running") or 0)
        queued_cnt = int(summary.get("queued") or 0)

        # Cập nhật tiêu đề phụ
        sub_text = f"{running_cnt} đang chạy · {queued_cnt} xếp hàng"
        for widget in self.jobs_title.winfo_children():
            if isinstance(widget, tk.Label) and widget.cget("fg") == ui.TEXT_FAINT:
                widget.config(text=sub_text)

        # Banner trạng thái chạy song song (R7)
        if running_cnt >= 3:
            active_pools = {j.get("pool") for j in active_list if j.get("pool")}
            self.concurrency_banner.config(
                text=f"⚡ ĐỒNG THỜI CAO: Đang thực thi {running_cnt} tác vụ song song trên {len(active_pools)} pool độc lập!",
                bg="#132742",
                fg=ui.RUN,
            )
            self.concurrency_banner.pack(
                fill="x", padx=10, pady=(2, 4), before=self.jobs_table
            )
        elif running_cnt > 0:
            self.concurrency_banner.config(
                text=f"⚡ Đang chạy song song {running_cnt} tác vụ ({queued_cnt} đang chờ)",
                bg=ui.SURFACE_2,
                fg=ui.ACCENT,
            )
            self.concurrency_banner.pack(
                fill="x", padx=10, pady=(2, 4), before=self.jobs_table
            )
        else:
            self.concurrency_banner.pack_forget()

        # Chọn danh sách nguồn theo tab
        source_jobs = (
            active_list if self._active_jobs_tab == "active" else recent_list
        )

        # Xây dựng chữ ký kiểm tra thay đổi dữ liệu để tránh vẽ lại không cần thiết
        jobs_sig = tuple(
            (
                j.get("id"),
                j.get("state"),
                j.get("pool"),
                j.get("last_event"),
                int(j.get("duration") or 0),
            )
            for j in source_jobs
        )
        if not force_redraw and jobs_sig == self._last_jobs_sig:
            return
        self._last_jobs_sig = jobs_sig

        table_rows: list[dict[str, Any]] = []
        for j in source_jobs:
            kind_raw = j.get("kind") or "chat"
            kind_txt = KIND_DISPLAY.get(kind_raw, kind_raw)
            dur = float(j.get("duration") or 0.0)
            what_raw = j.get("last_event") or (
                "Đang chạy..."
                if j.get("state") == "RUNNING"
                else (
                    "Đang xếp hàng"
                    if j.get("state") == "QUEUED"
                    else "Đã kết thúc"
                )
            )

            table_rows.append(
                {
                    "id": j.get("id"),
                    "kind_label": kind_txt,
                    "state": j.get("state") or "UNKNOWN",
                    "title": ui.truncate(j.get("title") or "Không tiêu đề", 24),
                    "pool": j.get("pool") or "—",
                    "duration_str": ui.fmt_duration(dur),
                    "what": ui.truncate(what_raw, 45),
                    "raw_job": j,
                }
            )

        # Bảo toàn hàng được chọn qua lần refresh (R6)
        prev_id = self._selected_job_id
        target_idx: int | None = None
        for i, row in enumerate(table_rows):
            if row.get("id") == prev_id:
                target_idx = i
                break

        # Nếu chưa chọn gì hoặc job cũ biến mất, chọn dòng đầu tiên để user luôn thấy log
        if target_idx is None and table_rows:
            target_idx = 0
            self._selected_job_id = table_rows[0].get("id")

        self.jobs_table.set_rows(table_rows)
        if target_idx is not None:
            self.jobs_table.select_row(target_idx)
            self._on_job_row_selected(table_rows[target_idx])

    def _on_job_row_selected(self, row: dict[str, Any] | None) -> None:
        """Xử lý khi người dùng chọn một hàng trong bảng tác vụ."""
        if not row:
            self._selected_job_id = None
            self._update_log_header(None)
            return

        self._selected_job_id = row.get("id")
        raw_job = row.get("raw_job") or {}
        self._update_log_header(raw_job)
        self._render_job_logs(force=True)

    def _update_log_header(self, job: dict[str, Any] | None) -> None:
        """Cập nhật tiêu đề và trạng thái nút hành động của log stream."""
        if not job:
            sub_text = "Chọn một tác vụ ở trên để xem chi tiết"
            self.btn_cancel_job.set_enabled(False)
            self.btn_open_report.set_enabled(False)
            self.btn_copy_id.set_enabled(False)
        else:
            jid = job.get("id") or ""
            state = job.get("state") or "UNKNOWN"
            dur = ui.fmt_duration(float(job.get("duration") or 0.0))
            pool = job.get("pool") or "auto"
            sub_text = f"[{jid}] {state} · {dur} · Pool: {pool}"

            # Cho phép huỷ nếu job đang xếp hàng hoặc đang chạy
            self.btn_cancel_job.set_enabled(state in ("QUEUED", "RUNNING"))
            self.btn_open_report.set_enabled(bool(job.get("has_report")))
            self.btn_copy_id.set_enabled(True)

        for widget in self.log_title.winfo_children():
            if isinstance(widget, tk.Label) and widget.cget("fg") == ui.TEXT_FAINT:
                widget.config(text=sub_text)

    def _render_job_logs(self, force: bool = False) -> None:
        """Nạp các sự kiện log thời gian thực từ SQLite store mà không làm giật văn bản."""
        if not self._selected_job_id:
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert(
                "1.0",
                "Chưa chọn tác vụ nào. Hãy nhấp vào một dòng ở bảng tiến trình phía trên.",
                "ts",
            )
            self.log_text.config(state="disabled")
            self._last_rendered_event_id = -1
            return

        job_id = self._selected_job_id
        events = store.get_events(job_id, limit=500)

        # Nếu không có sự kiện mới và không yêu cầu vẽ lại ép buộc → bỏ qua để user có thể bôi đen văn bản
        last_id = events[-1]["id"] if events else 0
        if not force and last_id == self._last_rendered_event_id:
            return
        self._last_rendered_event_id = last_id

        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")

        if not events:
            self.log_text.insert(
                "end",
                f"[{job_id}] Đang chuẩn bị môi trường khởi chạy hoặc chờ tiến trình runner ghi sự kiện...\n",
                "ts",
            )
        else:
            for ev in events:
                ts_val = ev.get("ts")
                ts_str = (
                    datetime.datetime.fromtimestamp(ts_val).strftime("%H:%M:%S")
                    if ts_val
                    else "--:--:--"
                )
                level = (ev.get("level") or "info").lower()
                lvl_tag = (
                    "level_error"
                    if "err" in level
                    else (
                        "level_warn"
                        if "warn" in level
                        else ("level_ok" if "ok" in level else "level_info")
                    )
                )
                lvl_disp = f" {level.upper():<5} "
                msg = ev.get("message") or ""

                self.log_text.insert("end", ts_str, "ts")
                self.log_text.insert("end", lvl_disp, lvl_tag)
                self.log_text.insert("end", f"{msg}\n", "msg")

        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ------------------------------------------------------------------
    # 5. Cập nhật Bảng Tài khoản & Công suất (R1.3, R2, R4)
    # ------------------------------------------------------------------

    def _update_accounts_ui(self, snap: dict[str, Any]) -> None:
        """Cập nhật bảng danh sách tài khoản, quota và thông tin chi tiết."""
        accounts = snap.get("accounts") or []

        # Chữ ký kiểm tra thay đổi tài khoản
        acc_sig = tuple(
            (
                a.get("account"),
                a.get("session_ok"),
                a.get("account_status"),
                a.get("headroom"),
                a.get("cookies"),
            )
            for a in accounts
        )
        if acc_sig == self._last_accounts_sig:
            return
        self._last_accounts_sig = acc_sig

        table_rows: list[dict[str, Any]] = []
        for acc in accounts:
            name = acc.get("account") or "unknown"
            pool = acc.get("pool") or name
            auth_type = acc.get("auth") or "oauth"
            auth_badge = "L2" if auth_type == "password" else "L1"

            # Phân định trạng thái session rõ ràng (nổi bật nếu UNAUTH)
            session_ok = acc.get("session_ok")
            status_raw = acc.get("account_status")
            if session_ok is False or status_raw == "UNAUTHENTICATED":
                session_badge = "UNAUTH"
            elif session_ok is True or status_raw in ("AVAILABLE", "ACTIVE"):
                session_badge = "AVAILABLE"
            else:
                session_badge = status_raw or "UNKNOWN"

            # Đọc số liệu quota nếu có
            quota = acc.get("quota") or {}
            windows = quota.get("windows") or {}
            w_5h = windows.get("current_5h") or {}
            w_week = windows.get("weekly") or {}

            q_5h_pct = (
                f"{w_5h.get('remaining_percentage', 0):.0f}%"
                if "remaining_percentage" in w_5h
                else "—"
            )
            q_week_pct = (
                f"{w_week.get('remaining_percentage', 0):.0f}%"
                if "remaining_percentage" in w_week
                else "—"
            )

            headroom = acc.get("headroom")
            headroom_str = f"{headroom:.0f}%" if headroom is not None else "—"

            # Định dạng thời gian lần cuối
            last_ok = acc.get("last_ok_at")
            last_seen = (
                last_ok.split("T")[-1][:5]
                if last_ok and "T" in str(last_ok)
                else "—"
            )

            table_rows.append(
                {
                    "account": name,
                    "pool": pool,
                    "auth_badge": auth_badge,
                    "session_badge": session_badge,
                    "q_5h": q_5h_pct,
                    "q_week": q_week_pct,
                    "headroom_str": headroom_str,
                    "last_seen": last_seen,
                    "raw_acc": acc,
                }
            )

        # Giữ selection tài khoản qua các nhịp poll
        prev_acc = self._selected_account_name
        target_idx: int | None = None
        for i, row in enumerate(table_rows):
            if row.get("account") == prev_acc:
                target_idx = i
                break

        if target_idx is None and table_rows:
            target_idx = 0
            self._selected_account_name = table_rows[0].get("account")

        self.accounts_table.set_rows(table_rows)
        if target_idx is not None:
            self.accounts_table.select_row(target_idx)
            self._on_account_row_selected(table_rows[target_idx])

    def _on_account_row_selected(self, row: dict[str, Any] | None) -> None:
        """Cập nhật card chi tiết khi người dùng chọn một tài khoản."""
        if not row:
            self._selected_account_name = None
            self.lbl_acc_name.config(text="Chọn tài khoản để thao tác")
            self.lbl_acc_meta.config(text="")
            self.acc_headroom_bar.set_value(0.0)
            self.btn_paste_cookie.set_enabled(False)
            self.btn_auto_login.set_enabled(False)
            self.btn_sync_cookie.set_enabled(False)
            self.btn_chrome_profile.set_enabled(False)
            return

        acc_data = row.get("raw_acc") or {}
        name = acc_data.get("account") or ""
        email = acc_data.get("email") or "Chưa có email"
        auth = acc_data.get("auth") or "oauth"
        self._selected_account_name = name

        auth_desc = "Loại 2 (Mật khẩu tự động)" if auth == "password" else "Loại 1 (OAuth thủ công)"
        self.lbl_acc_name.config(text=f"{name} ({email})")
        self.lbl_acc_meta.config(text=auth_desc)

        # Cập nhật thanh tiến trình Headroom theo ngưỡng màu
        headroom = acc_data.get("headroom")
        if headroom is not None:
            val = float(headroom)
            self.acc_headroom_bar.set_value(val)
            if val >= 60.0:
                self.acc_headroom_bar.set_color(ui.OK)
            elif val >= 30.0:
                self.acc_headroom_bar.set_color(ui.WARN)
            else:
                self.acc_headroom_bar.set_color(ui.ERR)
        else:
            self.acc_headroom_bar.set_value(0.0)
            self.acc_headroom_bar.set_color(ui.TEXT_FAINT)

        # Cập nhật trạng thái các nút hành động tài khoản
        self.btn_paste_cookie.set_enabled(True)
        # Nút tự động đăng nhập chỉ sáng khi tài khoản là Loại 2 (có mật khẩu)
        self.btn_auto_login.set_enabled(auth == "password")
        self.btn_sync_cookie.set_enabled(True)
        self.btn_chrome_profile.set_enabled(True)

    def _get_account_data(self, account_name: str) -> dict[str, Any] | None:
        """Tra cứu dict thông tin của một tài khoản trong snapshot hiện thời."""
        for acc in self._snap.get("accounts") or []:
            if acc.get("account") == account_name:
                return acc
        return None

    # ------------------------------------------------------------------
    # 6. Cảnh báo Quota và Session (R8)
    # ------------------------------------------------------------------

    def _check_alerts(self, snap: dict[str, Any]) -> None:
        """Hiển thị cảnh báo trực quan khi tài khoản chết phiên hoặc sắp cạn quota."""
        accounts = snap.get("accounts") or []
        summary = snap.get("summary") or {}
        total = int(summary.get("accounts_total") or len(accounts))
        alive = int(summary.get("accounts_alive") or 0)

        # 1. Cảnh báo tài khoản chết phiên
        if total > 0 and alive < total:
            self.dead_acc_banner.config(
                text=f"⚠️ Cảnh báo: Chỉ {alive}/{total} account còn sống! Các account chết session sẽ khiến task thất bại."
            )
            self.dead_acc_banner.pack(
                fill="x", padx=10, pady=(2, 2), before=self.accounts_table
            )
        else:
            self.dead_acc_banner.pack_forget()

        # 2. Cảnh báo cạn kiệt Quota (<10%)
        low_quota_accs = [
            a.get("account")
            for a in accounts
            if a.get("headroom") is not None and float(a.get("headroom")) < 10.0
        ]
        if low_quota_accs:
            self.low_quota_banner.config(
                text=f"🔴 Cạn quota (<10%): {', '.join(low_quota_accs)}. Hãy đổi pool hoặc chờ reset."
            )
            self.low_quota_banner.pack(
                fill="x", padx=10, pady=(2, 4), before=self.accounts_table
            )
            if not self._warned_low_headroom:
                ui.toast(
                    self,
                    f"Cảnh báo: Tài khoản {low_quota_accs[0]} sắp hết quota (<10%)!",
                    kind="err",
                )
                self._warned_low_headroom = True
        else:
            self.low_quota_banner.pack_forget()
            self._warned_low_headroom = False

    # ------------------------------------------------------------------
    # 7. Xử lý Khởi chạy Task (R1.1)
    # ------------------------------------------------------------------

    def _on_submit_single(self) -> None:
        """Gửi 1 task đơn tới pool được chỉ định hoặc auto."""
        prompt = self._get_prompt()
        if not prompt:
            ui.toast(
                self, "Vui lòng nhập nội dung prompt trước khi chạy.", kind="warn"
            )
            return

        kind = self._get_selected_kind()
        pool = self._get_selected_pool()
        title = ui.truncate(prompt, 40)

        try:
            out = dispatch.submit(kind, prompt, pool=pool, title=title)
            jid = out.get("job_id") or "job"
            target_pool = out.get("pool") or pool or "auto"
            ui.toast(
                self, f"Đã submit {jid} [{kind}] tới pool {target_pool}", kind="ok"
            )
            self._clear_prompt()
            self.refresh(force=False)
        except Exception as e:
            ui.toast(self, f"Lỗi khởi chạy: {e}", kind="err")

    def _on_submit_n(self) -> None:
        """Gửi cùng prompt tới N pool độc lập để chạy song song thật sự."""
        prompt = self._get_prompt()
        if not prompt:
            ui.toast(
                self,
                "Vui lòng nhập nội dung prompt trước khi chạy ×N.",
                kind="warn",
            )
            return

        try:
            n_target = int(self.spin_n.get())
            if n_target < 1:
                n_target = 1
        except ValueError:
            n_target = 2

        kind = self._get_selected_kind()
        accounts = self._snap.get("accounts") or []

        # Ưu tiên các pool còn sống (session_ok != False) và có headroom cao nhất
        alive_accs = [
            a
            for a in accounts
            if a.get("session_ok") is not False
            and a.get("account_status") != "UNAUTHENTICATED"
        ]
        # Sắp xếp theo headroom giảm dần
        alive_accs.sort(key=lambda a: float(a.get("headroom") or 0.0), reverse=True)

        target_pools: list[str] = []
        for acc in alive_accs:
            p = acc.get("pool") or acc.get("account")
            if p and p not in target_pools:
                target_pools.append(p)
            if len(target_pools) >= n_target:
                break

        # Nếu không có đủ pool sống, dùng None (auto) để dispatch tự phân phối
        if not target_pools:
            target_pools = [None] * n_target

        base_title = ui.truncate(prompt, 30)
        submitted = 0
        for i, pool_name in enumerate(target_pools):
            sub_title = (
                f"{base_title} [{pool_name}]" if pool_name else f"{base_title} #{i+1}"
            )
            try:
                dispatch.submit(kind, prompt, pool=pool_name, title=sub_title)
                submitted += 1
            except Exception as e:
                print(f"[Submit N error] {e}", file=sys.stderr)

        ui.toast(
            self,
            f"Đã kích hoạt {submitted} tác vụ song song trên {len(set(filter(None, target_pools)))} pool!",
            kind="ok",
        )
        self._clear_prompt()
        self.refresh(force=False)

    def _on_submit_parallel_modal(self) -> None:
        """Hộp thoại chọn cụ thể các pool muốn phóng tác vụ song song."""
        prompt = self._get_prompt()
        if not prompt:
            ui.toast(
                self,
                "Vui lòng nhập nội dung prompt trước khi mở cấu hình song song.",
                kind="warn",
            )
            return

        accounts = self._snap.get("accounts") or []
        if not accounts:
            ui.toast(
                self,
                "Chưa có tài khoản nào trong hệ thống để chạy song song.",
                kind="warn",
            )
            return

        kind = self._get_selected_kind()
        modal = ui.Modal(
            self,
            title="Khởi chạy song song trên nhiều Pool",
            subtitle=f"Loại: {KIND_DISPLAY.get(kind, kind)}",
            width=520,
            height=420,
        )

        tk.Label(
            modal.body,
            text=f"Prompt: {ui.truncate(prompt, 75)}",
            bg=ui.SURFACE,
            fg=ui.TEXT_DIM,
            font=ui._font(9, "italic"),
            anchor="w",
        ).pack(fill="x", pady=(0, 10))

        tk.Label(
            modal.body,
            text="Chọn các pool muốn kích hoạt tác vụ:",
            bg=ui.SURFACE,
            fg=ui.TEXT,
            font=ui._font(9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        # Khung chứa danh sách checkbox
        list_frame = tk.Frame(
            modal.body,
            bg=ui.SURFACE_2,
            highlightthickness=1,
            highlightbackground=ui.BORDER,
            padx=10,
            pady=8,
        )
        list_frame.pack(fill="both", expand=True)

        pool_vars: list[tuple[str, tk.BooleanVar]] = []
        for acc in accounts:
            p = acc.get("pool") or acc.get("account")
            email = acc.get("email") or ""
            headroom = acc.get("headroom")
            hd_str = f"{headroom:.0f}%" if headroom is not None else "—"
            status = acc.get("account_status") or "UNKNOWN"
            is_alive = acc.get("session_ok") is not False and status != "UNAUTHENTICATED"

            var = tk.BooleanVar(value=is_alive)
            pool_vars.append((p, var))

            lbl_txt = f"Pool '{p}' ({email}) — Còn: {hd_str} — [{status}]"
            fg_col = ui.TEXT if is_alive else ui.ERR
            chk = ttk.Checkbutton(
                list_frame, text=lbl_txt, variable=var, style="TCheckbutton"
            )
            chk.pack(fill="x", pady=2, anchor="w")

        def on_run() -> None:
            chosen = [p for p, v in pool_vars if v.get()]
            if not chosen:
                ui.toast(modal, "Vui lòng chọn ít nhất 1 pool.", kind="warn")
                return

            modal.destroy()
            base_t = ui.truncate(prompt, 30)
            count = 0
            for p in chosen:
                try:
                    dispatch.submit(kind, prompt, pool=p, title=f"{base_t} [{p}]")
                    count += 1
                except Exception as e:
                    print(f"[Parallel submit err] {e}", file=sys.stderr)

            ui.toast(
                self, f"Đã submit {count} tác vụ song song đồng thời!", kind="ok"
            )
            self._clear_prompt()
            self.refresh(force=False)

        modal.add_button("Bắt đầu chạy", on_run, variant="primary")
        modal.add_button("Huỷ", modal.destroy, variant="ghost")

    # ------------------------------------------------------------------
    # 8. Xử lý Thao tác Tài khoản (R4)
    # ------------------------------------------------------------------

    def _open_cookie_popup(self, account: str, email: str, pool: str) -> None:
        """Mở popup dán cookie JSON (mẫu code chuẩn từ brief)."""

        def on_save(acc: str, raw: str) -> None:
            # Chạy nền — cấy cookie mất vài giây, mở Chrome.
            self._run_bg(
                lambda: account_registry.adopt_cookie_json(
                    acc, raw, email=email, pool=pool
                ),
                lambda out: self._toast_result(out, "Cấy cookie"),
            )

        cookie_popup.CookiePastePopup(
            self, account=account, email=email, pool=pool, on_save=on_save
        )

    def _on_paste_cookie_clicked(self) -> None:
        """Nút 'Nhập cookie' cho tài khoản đang chọn."""
        if not self._selected_account_name:
            ui.toast(self, "Chưa chọn tài khoản nào.", kind="warn")
            return
        acc_data = self._get_account_data(self._selected_account_name) or {}
        email = acc_data.get("email") or ""
        pool = acc_data.get("pool") or self._selected_account_name
        self._open_cookie_popup(self._selected_account_name, email, pool)

    def _on_auto_login_clicked(self) -> None:
        """Đăng nhập tự động cho tài khoản Loại 2 (email + mật khẩu)."""
        acc = self._selected_account_name
        if not acc:
            ui.toast(self, "Chưa chọn tài khoản nào.", kind="warn")
            return

        acc_data = self._get_account_data(acc) or {}
        if acc_data.get("auth") != "password":
            ui.toast(
                self,
                "Tài khoản Loại 1 (OAuth) yêu cầu đăng nhập tay trong Chrome.",
                kind="warn",
            )
            return

        ui.toast(
            self, f"Đang tiến hành đăng nhập tự động cho {acc}...", kind="info"
        )

        def do_login() -> dict[str, Any]:
            return auto_login.auto_login(acc)

        def on_done(out: dict[str, Any]) -> None:
            state = out.get("state")
            reason = out.get("reason") or ""
            steps = out.get("steps") or []
            step_summary = (
                f" ({len(steps)} bước: {', '.join(steps[-2:])})"
                if steps
                else ""
            )

            if out.get("ok") or state == "ok":
                ui.toast(
                    self,
                    f"Đăng nhập thành công cho {acc}!{step_summary}",
                    kind="ok",
                )
            elif state == "needs_human":
                hint = (
                    out.get("hint")
                    or "Vui lòng mở Chrome để giải quyết CAPTCHA/2FA thủ công."
                )
                ui.toast(self, f"Cần can thiệp tay: {hint}", kind="warn")
            else:
                ui.toast(
                    self, f"Đăng nhập thất bại: {reason}{step_summary}", kind="err"
                )
            self.refresh(force=False)

        self._run_bg(do_login, on_done)

    def _on_sync_cookie_clicked(self) -> None:
        """Đồng bộ cookie từ Chrome riêng của orchestrator sang file pool."""
        acc = self._selected_account_name
        if not acc:
            ui.toast(self, "Chưa chọn tài khoản nào.", kind="warn")
            return

        acc_data = self._get_account_data(acc) or {}
        pool = acc_data.get("pool") or acc

        ui.toast(self, f"Đang đọc cookie từ Chrome riêng cho {acc}...", kind="info")

        def do_sync() -> dict[str, Any]:
            return cookie_sync.sync(acc, pool_name=pool)

        def on_done(out: dict[str, Any]) -> None:
            if out.get("ok"):
                cookies_cnt = out.get("cookies", 0)
                status = out.get("account_status") or "AVAILABLE"
                ui.toast(
                    self,
                    f"Đã đồng bộ {cookies_cnt} cookies cho {acc} (Session: {status})",
                    kind="ok",
                )
            else:
                err = out.get("error") or "Không thể đồng bộ cookie"
                ui.toast(self, f"Đồng bộ thất bại: {err}", kind="err")
            self.refresh(force=False)

        self._run_bg(do_sync, on_done)

    def _on_chrome_profile_clicked(self) -> None:
        """Tạo hồ sơ Chrome mới cho account."""
        acc = self._selected_account_name or "Acc_New"
        ui.toast(self, f"Đang tạo hồ sơ Chrome cho '{acc}'...", kind="info")

        def do_create() -> dict[str, Any]:
            return chrome_profile.create_profile(acc)

        def on_done(out: dict[str, Any]) -> None:
            if out.get("directory"):
                d = out.get("directory")
                ui.toast(self, f"Đã khởi tạo hồ sơ Chrome tại: {d}", kind="ok")
            else:
                err = out.get("error") or "Không thể tạo profile"
                ui.toast(self, f"Lỗi tạo profile: {err}", kind="err")
            self.refresh(force=False)

        self._run_bg(do_create, on_done)

    # ------------------------------------------------------------------
    # 9. Xử lý Thao tác Tác vụ (R3)
    # ------------------------------------------------------------------

    def _on_cancel_job(self) -> None:
        """Huỷ tác vụ đang chạy hoặc đang xếp hàng."""
        if not self._selected_job_id:
            return
        jid = self._selected_job_id

        def do_cancel() -> dict[str, Any]:
            return dispatch.cancel(jid)

        def on_done(out: dict[str, Any]) -> None:
            if out.get("ok"):
                ui.toast(self, f"Đã gửi tín hiệu dừng {jid}", kind="ok")
            else:
                ui.toast(
                    self,
                    f"Không thể dừng {jid}: {out.get('error', 'Lỗi')}",
                    kind="err",
                )
            self.refresh(force=False)

        self._run_bg(do_cancel, on_done)

    def _on_open_report(self) -> None:
        """Mở file báo cáo kết quả của tác vụ bằng trình xem mặc định."""
        if not self._selected_job_id:
            return
        jid = self._selected_job_id
        job_data = store.get_job(jid)
        if not job_data:
            ui.toast(self, f"Không tìm thấy dữ liệu của {jid}", kind="warn")
            return

        report_p = job_data.get("report_path")
        if not report_p or not os.path.isfile(report_p):
            ui.toast(
                self, f"File report chưa được tạo hoặc không tồn tại: {report_p}", kind="warn"
            )
            return

        try:
            # Mở file trên Windows bằng chương trình liên kết mặc định
            os.startfile(report_p)
            ui.toast(
                self, f"Đang mở báo cáo: {Path(report_p).name}", kind="ok"
            )
        except Exception as e:
            ui.toast(self, f"Không thể mở file: {e}", kind="err")

    def _on_copy_job_id(self) -> None:
        """Sao chép mã job_id vào clipboard hệ điều hành."""
        if not self._selected_job_id:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self._selected_job_id)
            ui.toast(
                self, f"Đã sao chép mã {self._selected_job_id}", kind="ok"
            )
        except Exception as e:
            ui.toast(self, f"Lỗi clipboard: {e}", kind="err")

    # ------------------------------------------------------------------
    # 10. Trợ thủ chạy nền & Toast (R5)
    # ------------------------------------------------------------------

    def _run_bg(self, fn: Callable[[], Any], done: Callable[[Any], None]) -> None:
        """Mẫu chuẩn: chạy tác vụ lâu trong luồng riêng để giao diện không bị treo."""

        def worker() -> None:
            try:
                out = fn()
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if self.winfo_exists():
                self.after(0, lambda: done(out))

        threading.Thread(target=worker, daemon=True).start()

    def _toast_result(self, out: dict[str, Any], action: str = "") -> None:
        """Hiện thông báo kết quả trả về từ tác vụ nền."""
        if out.get("ok"):
            msg = f"{action} thành công" if action else "Thao tác thành công"
            if out.get("warning"):
                msg += f" (Cảnh báo: {out['warning']})"
            ui.toast(self, msg, kind="ok")
        else:
            err = out.get("error") or out.get("reason") or "Lỗi không xác định"
            ui.toast(self, f"{action} thất bại: {err}", kind="err")
        self.refresh(force=False)
