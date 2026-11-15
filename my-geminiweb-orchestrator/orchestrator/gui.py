"""GUI của Orchestrator — Tkinter, chạy nhẹ, mở khi cần quản lý.

Thiết kế cốt lõi
----------------
GUI là *view* của cùng 1 SQLite store mà runner ghi vào. Đóng GUI không ảnh
hưởng job đang chạy.

Cấu trúc sau đợt làm lại (2026-11-14):

    OrchestratorGUI          ← cửa sổ chính, chỉ là shell
      ├── Header             ← tên app + summary chips + nút làm mới quota
      ├── Dashboard          ← layout do Gemini thiết kế (orchestrator/dashboard.py)
      │     ├── Launch       ← submit task (1 hoặc N pool song song)
      │     ├── Jobs         ← bảng job đang chạy
      │     ├── Accounts     ← bảng account + công suất (quota)
      │     └── Log          ← log stream của job đang chọn
      └── AccountsWindow     ← cửa sổ quản lý account (loại 1 / loại 2)

Vì sao tách `Dashboard` ra file riêng: layout phức tạp (~700 dòng) và là
phần được làm lại nhiều nhất — tách ra để sửa không đụng phần ổn định.

Phụ thuộc
---------
Chỉ stdlib + `tkinter`. Bản `.exe` không phình thêm (xem `orchestrator.spec`).
Theme tokens và widget nằm ở `orchestrator/ui.py`.

Chạy:
    python -m orchestrator.gui
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import board, dispatch, store, ui  # noqa: E402

#: Chu kỳ tự chữa session (giây). Mỗi lần dashboard phát hiện account chết
#: session → tự đồng bộ cookie lại (và đăng nhập lại nếu là loại 2).
AUTO_HEAL_INTERVAL = 180.0

#: Tối thiểu bao lâu giữa 2 lần nhắc "cần đăng nhập tay" (giây) — tránh spam.
NEEDS_HUMAN_NOTIFY_INTERVAL = 600.0

STATE_COLORS = {
    store.QUEUED: ui.QUEUE,
    store.RUNNING: ui.RUN,
    store.DONE: ui.OK,
    store.FAILED: ui.ERR,
    store.CANCELLED: ui.WARN,
}

KIND_LABELS = {
    "deep_research": "Deep Research",
    "chat": "Chat",
    "canvas": "Canvas",
    "canvas_api": "Canvas (API)",
    "antigravity": "Antigravity",
    "chrome_profile": "Chrome Profile",
}


class OrchestratorGUI:
    """Cửa sổ chính — shell mỏng, phần lớn logic nằm ở `Dashboard`."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Orchestrator")
        self.root.geometry("1360x820")
        self.root.minsize(1100, 700)
        self.root.configure(bg=ui.BG)

        self._build_style()
        self._build_widgets()

        # Reaper: dọn job mồ côi (runner chết) để bảng không hiện job ma.
        # Chạy 1 lần lúc mở, sau đó để Dashboard lo phần định kỳ.
        try:
            store.reap_orphans()
        except Exception:
            pass

        # Tự chữa session: giữ mọi account đã đăng nhập ở trạng thái AVAILABLE.
        self._heal_running = False
        self._last_needs_human_at = 0.0
        self.root.after(5000, self._auto_heal_tick)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_style(self) -> None:
        """Style các widget Tk gốc (tkinter không có dark theme sẵn)."""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox",
            fieldbackground=ui.SURFACE_2,
            background=ui.SURFACE_2,
            foreground=ui.TEXT,
            arrowcolor=ui.TEXT_DIM,
            bordercolor=ui.BORDER,
            lightcolor=ui.SURFACE_2,
            darkcolor=ui.SURFACE_2,
            selectbackground=ui.ACCENT,
            selectforeground=ui.TEXT,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", ui.SURFACE_2)],
            foreground=[("readonly", ui.TEXT)],
            selectbackground=[("readonly", ui.SURFACE_2)],
        )
        style.configure(
            "TCheckbutton",
            background=ui.SURFACE,
            foreground=ui.TEXT,
            indicatorcolor=ui.SURFACE_2,
        )
        style.map(
            "TCheckbutton",
            background=[("active", ui.SURFACE)],
            foreground=[("active", ui.TEXT)],
        )

    def _build_widgets(self) -> None:
        self._build_header()

        # Dashboard — phần layout chính (do Gemini thiết kế).
        from orchestrator import dashboard

        self.dashboard = dashboard.Dashboard(self.root, app=self)
        self.dashboard.pack(fill="both", expand=True)

        self.status = tk.Label(
            self.root, text="", bg=ui.BG, fg=ui.TEXT_FAINT,
            font=ui._font(8), anchor="w", padx=14, pady=4,
        )
        self.status.pack(fill="x", side="bottom")

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=ui.SURFACE, height=56)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Orchestrator", bg=ui.SURFACE, fg=ui.TEXT,
            font=ui._font(13, "bold"),
        ).pack(side="left", padx=(16, 14))

        # Summary chips — cập nhật theo snapshot.
        self.chip_running = self._chip(header, "0 đang chạy", ui.RUN)
        self.chip_queued = self._chip(header, "0 xếp hàng", ui.QUEUE)
        self.chip_accounts = self._chip(header, "0/0 account", ui.TEXT_DIM)
        self.chip_failed = self._chip(header, "0 lỗi", ui.TEXT_FAINT)

        ui.Button(
            header, "Tài khoản", self._open_accounts_window,
            variant="ghost", width=100, height=30, parent_bg=ui.SURFACE,
        ).pack(side="right", padx=(6, 0))
        ui.Button(
            header, "Làm mới quota", self._refresh_quota,
            variant="ghost", width=120, height=30, parent_bg=ui.SURFACE,
        ).pack(side="right", padx=6)
        ui.Button(
            header, "Dọn job hỏng", self._reap,
            variant="ghost", width=120, height=30, parent_bg=ui.SURFACE,
        ).pack(side="right", padx=6)

        self._tick()

    def _chip(self, parent: tk.Misc, text: str, color: str) -> tk.Label:
        lbl = tk.Label(
            parent, text=text, bg=ui.SURFACE, fg=color,
            font=ui._font(9), padx=10,
        )
        lbl.pack(side="left", padx=(0, 6))
        return lbl

    def update_summary(self, summary: dict) -> None:
        """Cập nhật chip header từ `board.snapshot()['summary']`."""
        try:
            running = int(summary.get("running") or 0)
            queued = int(summary.get("queued") or 0)
            total = int(summary.get("accounts_total") or 0)
            alive = int(summary.get("accounts_alive") or 0)
            failed = int(summary.get("failed_recent") or 0)

            self.chip_running.config(
                text=f"{running} đang chạy", fg=ui.RUN if running else ui.TEXT_FAINT
            )
            self.chip_queued.config(
                text=f"{queued} xếp hàng", fg=ui.QUEUE if queued else ui.TEXT_FAINT
            )
            self.chip_accounts.config(
                text=f"{alive}/{total} account sống",
                fg=ui.OK if alive == total and total else (
                    ui.WARN if alive else ui.ERR
                ),
            )
            self.chip_failed.config(
                text=f"{failed} lỗi", fg=ui.ERR if failed else ui.TEXT_FAINT
            )
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Hành động
    # ------------------------------------------------------------------

    def _refresh_quota(self) -> None:
        """Ép đọc quota mới (gọi mạng → chạy nền)."""
        self.status.config(text="Đang đọc quota... (2-5 giây)")
        self._run_bg(
            lambda: board.snapshot(refresh_quota=True),
            lambda snap: self._after_quota(snap),
        )

    def _after_quota(self, snap: dict) -> None:
        self.update_summary(snap.get("summary") or {})
        self.status.config(text="Đã cập nhật quota.")
        try:
            self.dashboard.refresh()
        except Exception:
            pass

    def _reap(self) -> None:
        cleaned = store.reap_orphans()
        self.status.config(
            text=f"Đã dọn {len(cleaned)} job hỏng." if cleaned else "Không có job hỏng."
        )
        try:
            self.dashboard.refresh()
        except Exception:
            pass

    def _run_bg(self, fn, done) -> None:
        """Chạy hàm lâu trong thread (GUI không bao giờ treo)."""
        import threading

        def worker() -> None:
            try:
                out = fn()
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self.root.after(0, lambda: done(out))

        threading.Thread(target=worker, daemon=True).start()

    def _open_accounts_window(self) -> None:
        AccountsWindow(self.root, self)

    def _tick(self) -> None:
        """Vòng lặp nhẹ: chỉ cập nhật chip header, dashboard tự refresh."""
        try:
            snap = board.snapshot()
            self.update_summary(snap.get("summary") or {})
        except Exception:
            pass
        self.root.after(3000, self._tick)

    def _auto_heal_tick(self) -> None:
        """Tự đồng bộ cookie định kỳ → mọi account luôn AVAILABLE.

        Cơ chế
        ------
        Mỗi `AUTO_HEAL_INTERVAL` giây:
          1. Đọc snapshot (cache quota, không gọi mạng nếu còn hạn).
          2. Nếu có account nào `session_ok != True` → chạy `harvest_all()`
             (mở Chrome riêng → đọc cookie → ghi pool → kiểm tra API thật).
          3. Account loại 2 chết session → `auto_login` đăng nhập lại tự động.
          4. Account loại 1 chết session → báo `needs_human`, **không** spam
             lại (chỉ nhắc 1 lần / 10 phút).

        Đây là nơi "mọi acc đã đăng nhập luôn AVAILABLE" được thực thi.
        """
        try:
            snap = board.snapshot()
            accounts = snap.get("accounts") or []
            broken = [
                a
                for a in accounts
                if a.get("session_ok") is not True
                and a.get("account_status") != "AVAILABLE"
            ]
            if broken:
                self._run_heal(broken)
        except Exception:
            pass
        self.root.after(AUTO_HEAL_INTERVAL * 1000, self._auto_heal_tick)

    def _run_heal(self, broken: list[dict]) -> None:
        """Chạy harvest để chữa các account session chết (nền)."""
        if getattr(self, "_heal_running", False):
            return
        self._heal_running = True
        names = ", ".join(a.get("account") or "?" for a in broken)
        self.status.config(text=f"Đang tự chữa session: {names}...")

        from orchestrator import harvest

        def worker() -> dict:
            try:
                return harvest.harvest_all(heal=True)
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        def done(out: dict) -> None:
            self._heal_running = False
            board.invalidate_quota_cache()
            self._refresh()
            alive = out.get("alive") or 0
            total = out.get("total") or 0
            needs = out.get("needs_human") or 0
            if total:
                self.status.config(text=f"Tự chữa xong: {alive}/{total} account AVAILABLE.")
            if needs:
                self._notify_needs_human(out)
            else:
                self.status.config(text="Xong")

        def run() -> None:
            out = worker()
            if self.winfo_exists():
                self.after(0, lambda: done(out))

        threading.Thread(target=run, daemon=True).start()

    def _notify_needs_human(self, out: dict) -> None:
        """Nhắc user đăng nhập tay cho account loại 1 (giới hạn tần suất)."""
        now = time.time()
        last = getattr(self, "_last_needs_human_at", 0.0)
        if now - last < NEEDS_HUMAN_NOTIFY_INTERVAL:
            return
        self._last_needs_human_at = now
        need = [
            r
            for r in (out.get("results") or [])
            if (r.get("heal") or {}).get("state") == "needs_human"
        ]
        if not need:
            return
        names = "\n".join(
            f"• {r.get('account')} — {(r.get('heal') or {}).get('reason', '')[:90]}"
            for r in need
        )
        messagebox.showwarning(
            "Cần đăng nhập tay",
            f"Các tài khoản sau cần bạn đăng nhập lại trong cửa sổ Chrome:\n\n"
            f"{names}\n\n"
            "Cửa sổ Chrome đã được mở. Đăng nhập xong bấm 'Đồng bộ cookie'.",
        )

    def run(self) -> None:
        self.root.mainloop()


class AccountsWindow:
    """Cửa sổ quản lý tài khoản Gemini — 2 loại, dùng account registry.

    Cơ chế cũ có 3 nguồn trạng thái rời rạc (pool file, mapping account→pool,
    credential) nên thêm account phải làm nhiều thao tác và dễ sót. Giờ **1
    lệnh `account add`** làm hết: tạo entry, tạo pool rỗng, lưu mật khẩu (DPAPI),
    và tự đăng nhập nếu là tài khoản loại 2.

    2 loại tài khoản
    ----------------
    - **Loại 1 — có xác thực (2FA)**: bấm "Đăng nhập tay (Chrome)" → user đăng
      nhập tay 1 lần → cookie sync tự động mãi.
    - **Loại 2 — chỉ email + password**: nhập email + mật khẩu → bấm "Thêm &
      đăng nhập" → orchestrator tự điền form. Gặp CAPTCHA/2FA thì dừng lại để
      user xử lý tay (không bypass).
    """

    def __init__(self, parent: tk.Misc, app: OrchestratorGUI) -> None:
        self.app = app
        self.win = tk.Toplevel(parent)
        self.win.title("Tài khoản Gemini")
        self.win.geometry("1060x660")
        self.win.minsize(900, 560)
        self.win.configure(bg=ui.BG)

        self._build()
        self._refresh()

    # -- UI ------------------------------------------------------------

    def _build(self) -> None:
        top = tk.Frame(self.win, bg=ui.SURFACE)
        top.pack(fill="x")
        tk.Label(
            top, text="Tài khoản Gemini", bg=ui.SURFACE, fg=ui.TEXT,
            font=ui._font(12, "bold"),
        ).pack(side="left", padx=16, pady=12)
        ui.Button(
            top, "Làm mới", self._refresh, variant="ghost",
            width=90, height=28, parent_bg=ui.SURFACE,
        ).pack(side="right", padx=14)

        # Bảng tài khoản
        cols = ("account", "pool", "email", "auth", "session", "archive", "last")
        self.tree = ttk.Treeview(
            self.win, columns=cols, show="headings", selectmode="browse",
            style="Treeview",
        )
        heads = {
            "account": ("Account", 120),
            "pool": ("Pool", 80),
            "email": ("Email", 190),
            "auth": ("Loại", 165),
            "session": ("Session", 175),
            "archive": ("File cookie", 105),
            "last": ("Cập nhật lần cuối", 150),
        }
        for c, (h, w) in heads.items():
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(10, 8))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        # Form thêm account
        form = tk.Frame(self.win, bg=ui.SURFACE)
        form.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(form, text="Thêm tài khoản mới", bg=ui.SURFACE, fg=ui.TEXT,
                 font=ui._font(10, "bold")).pack(anchor="w", pady=(4, 6))

        row1 = tk.Frame(form, bg=ui.SURFACE)
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="Email:", bg=ui.SURFACE, fg=ui.TEXT_DIM,
                 font=ui._font(9)).pack(side="left")
        self.entry_email = tk.Entry(row1, width=26, bg=ui.SURFACE_2,
                                    fg=ui.TEXT, insertbackground=ui.TEXT,
                                    relief="flat", highlightthickness=1,
                                    highlightbackground=ui.BORDER,
                                    highlightcolor=ui.ACCENT, bd=0)
        self.entry_email.pack(side="left", padx=(4, 12))
        tk.Label(row1, text="Tên account:", bg=ui.SURFACE, fg=ui.TEXT_DIM,
                 font=ui._font(9)).pack(side="left")
        self.entry_account = tk.Entry(row1, width=16, bg=ui.SURFACE_2,
                                      fg=ui.TEXT, insertbackground=ui.TEXT,
                                      relief="flat", highlightthickness=1,
                                      highlightbackground=ui.BORDER,
                                      highlightcolor=ui.ACCENT, bd=0)
        self.entry_account.pack(side="left", padx=(4, 12))
        tk.Label(row1, text="Pool:", bg=ui.SURFACE, fg=ui.TEXT_DIM,
                 font=ui._font(9)).pack(side="left")
        self.entry_pool = tk.Entry(row1, width=12, bg=ui.SURFACE_2,
                                   fg=ui.TEXT, insertbackground=ui.TEXT,
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=ui.BORDER,
                                   highlightcolor=ui.ACCENT, bd=0)
        self.entry_pool.pack(side="left", padx=(4, 8))
        ui.Button(
            row1, "Thêm & đăng nhập", self._add_account, variant="primary",
            width=140, height=30, parent_bg=ui.SURFACE,
        ).pack(side="left")

        # Form mật khẩu (loại 2)
        row2 = tk.Frame(form, bg=ui.SURFACE)
        row2.pack(fill="x", pady=2)
        tk.Label(row2, text="Mật khẩu", bg=ui.SURFACE, fg=ui.TEXT_DIM,
                 font=ui._font(9)).pack(side="left")
        tk.Label(row2, text="(loại 2 — email + mật khẩu)", bg=ui.SURFACE,
                 fg=ui.TEXT_FAINT, font=ui._font(8)).pack(side="left", padx=(4, 12))
        self.entry_pw_account = tk.Entry(row2, width=16, bg=ui.SURFACE_2,
                                         fg=ui.TEXT, insertbackground=ui.TEXT,
                                         relief="flat", highlightthickness=1,
                                         highlightbackground=ui.BORDER,
                                         highlightcolor=ui.ACCENT, bd=0)
        self.entry_pw_account.pack(side="left", padx=(0, 12))
        self.entry_password = tk.Entry(row2, width=26, show="*", bg=ui.SURFACE_2,
                                       fg=ui.TEXT, insertbackground=ui.TEXT,
                                       relief="flat", highlightthickness=1,
                                       highlightbackground=ui.BORDER,
                                       highlightcolor=ui.ACCENT, bd=0)
        self.entry_password.pack(side="left", padx=(0, 12))
        ui.Button(
            row2, "Lưu & đăng nhập", self._do_auto_login, variant="success",
            width=140, height=30, parent_bg=ui.SURFACE,
        ).pack(side="left")

        tk.Label(
            form,
            text=(
                "Mật khẩu được mã hoá bằng DPAPI của Windows (chỉ user này, trên máy này "
                "giải mã được). Không bao giờ ghi plaintext."
            ),
            bg=ui.SURFACE, fg=ui.TEXT_FAINT, font=ui._font(8),
        ).pack(anchor="w", pady=(4, 8))

        # Hành động
        btns = tk.Frame(self.win, bg=ui.BG)
        btns.pack(fill="x", padx=10, pady=(0, 6))
        ui.Button(btns, "Đăng nhập tay (Chrome)", self._do_login,
                  variant="ghost", width=150, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Nhập cookie (dán JSON)", self._do_paste_cookie,
                  variant="ghost", width=160, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Nhập cookie (file)", self._do_adopt,
                  variant="ghost", width=140, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Đồng bộ cookie", self._do_sync,
                  variant="ghost", width=130, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Kiểm tra session", self._do_check,
                  variant="ghost", width=130, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Thu hoạch tất cả", self._do_harvest_all,
                  variant="primary", width=140, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Lấy cookie từ browser", self._do_bridge,
                  variant="ghost", width=160, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Xoá tài khoản", self._do_remove,
                  variant="danger", width=130, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))
        ui.Button(btns, "Mở thư mục cookie", self._open_cookie_dir,
                  variant="ghost", width=140, height=30, parent_bg=ui.BG).pack(side="left", padx=(0, 6))

        self.status = tk.Label(self.win, text="", bg=ui.BG, fg=ui.TEXT_DIM,
                               font=ui._font(9), anchor="w", padx=12, pady=4)
        self.status.pack(fill="x")

    # -- Helpers -------------------------------------------------------

    def _selected_account(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return str(self.tree.item(sel[0], "values")[0])

    def _selected_pool(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return str(self.tree.item(sel[0], "values")[1])

    def _on_select(self) -> None:
        acc = self._selected_account()
        if not acc:
            return
        # Điền sẵn account vào form mật khẩu để tiện.
        try:
            self.entry_pw_account.delete(0, "end")
            self.entry_pw_account.insert(0, acc)
        except tk.TclError:
            pass
        self.status.config(text=f"Đang chọn: {acc}")

    def _run_bg(self, fn, done) -> None:
        """Chạy hàm lâu (mở Chrome, gọi API) trong thread, không treo GUI."""
        import threading

        def worker() -> None:
            try:
                out = fn()
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self.win.after(0, lambda: done(out))

        threading.Thread(target=worker, daemon=True).start()

    # -- Actions -------------------------------------------------------

    def _refresh(self) -> None:
        from orchestrator import accounts as account_registry
        from orchestrator import cookie_sync

        rows: list[tuple[str, str, str, str, str, str]] = []

        registry = {a["account"]: a for a in account_registry.list_accounts()}

        # Bản cookie gốc đã lưu (cookie_files/<pool>.json)
        archived = {
            str(item.get("name")): item
            for item in cookie_sync.list_cookie_archives()
            if item.get("name")
        }

        # Pool file có cookie nhưng chưa khai báo trong registry → hiện luôn
        # để user không bỏ sót (trạng thái legacy).
        for pf in sorted(cookie_sync.POOLS_DIR.glob("*.json")):
            if pf.name in ("example.json", cookie_sync.ACCOUNT_POOLS_FILE):
                continue
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            acc = str(data.get("profile") or pf.stem)
            if acc not in registry:
                registry[acc] = {
                    "account": acc,
                    "email": data.get("account_email") or "",
                    "pool": pf.stem,
                    "auth": "oauth",
                    "has_password": False,
                    "last_ok_at": None,
                    "last_error": None,
                }

        for acc in sorted(registry):
            info = registry[acc]
            pool = info.get("pool") or acc

            pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
            n_cookies = 0
            if pool_file.is_file():
                try:
                    pdata = json.loads(pool_file.read_text(encoding="utf-8"))
                    n_cookies = len(pdata.get("cookies") or [])
                except (OSError, ValueError):
                    n_cookies = 0

            if info.get("last_error"):
                session = f"LỖI: {str(info['last_error'])[:80]}"
            elif n_cookies:
                session = f"có cookie ({n_cookies})"
            else:
                session = "chưa có cookie"

            auth = {
                "password": "loại 2 (email+mật khẩu)",
                "oauth": "loại 1 (đăng nhập tay)",
            }.get(str(info.get("auth")), str(info.get("auth") or ""))

            arch = archived.get(pool)
            archive_txt = "có" if arch else "-"
            if arch and arch.get("saved_at"):
                archive_txt = arch["saved_at"].replace("T", " ")[:16]

            rows.append(
                (
                    acc,
                    pool,
                    info.get("email") or "",
                    auth,
                    session,
                    archive_txt,
                    info.get("last_ok_at") or info.get("last_error_at") or "-",
                )
            )

        for jid in self.tree.get_children():
            self.tree.delete(jid)
        for r in rows:
            self.tree.insert("", "end", values=r)
        self.status.config(text=f"{len(rows)} tài khoản")

    def _add_account(self) -> None:
        """Thêm account mới (tài khoản loại 2: có email + mật khẩu)."""
        from orchestrator import accounts as account_registry

        email = self.entry_email.get().strip()
        if not email:
            ui.toast(self.win, "Nhập email trước đã.", "warn")
            return
        acc = self.entry_account.get().strip() or None
        pool = self.entry_pool.get().strip() or None
        password = self.entry_password.get().strip() or None

        if not password and not messagebox.askyesno(
            "Thiếu mật khẩu",
            "Không nhập mật khẩu -> đây là tài khoản LOẠI 1.\n"
            "Bạn sẽ phải đăng nhập tay trong cửa sổ Chrome.\n\n"
            "Vẫn muốn tiếp tục?",
        ):
            return

        try:
            out = account_registry.add_account(
                email, account=acc, pool=pool, password=password
            )
        except Exception as e:
            ui.toast(self.win, f"{type(e).__name__}: {e}", "err")
            return

        acc_name = out["account"]
        self.entry_email.delete(0, "end")
        self.entry_account.delete(0, "end")
        self.entry_pool.delete(0, "end")
        self.entry_password.delete(0, "end")

        if not password:
            self._refresh()
            ui.toast(
                self.win,
                f"Đã thêm account {acc_name!r} (loại 1) — đăng nhập tay trong Chrome.",
                "info",
            )
            return

        self.status.config(text=f"Đang đăng nhập {acc_name}...")
        from orchestrator import auto_login

        self._run_bg(
            lambda: auto_login.auto_login(acc_name),
            lambda res: self._after_login(res, acc_name),
        )

    def _do_auto_login(self) -> None:
        """Lưu mật khẩu (DPAPI) rồi đăng nhập tự động cho account đã chọn."""
        from orchestrator import accounts as account_registry
        from orchestrator import auto_login

        acc = self.entry_pw_account.get().strip() or self._selected_account()
        if not acc:
            ui.toast(self.win, "Chọn một tài khoản trước đã.", "warn")
            return
        password = self.entry_password.get().strip()
        if not password:
            ui.toast(self.win, "Nhập mật khẩu trước đã.", "warn")
            return

        try:
            account_registry.set_password(acc, password)
        except Exception as e:
            ui.toast(self.win, f"{type(e).__name__}: {e}", "err")
            return

        self.entry_password.delete(0, "end")
        self.status.config(text=f"Đang đăng nhập {acc}...")
        self._run_bg(
            lambda: auto_login.auto_login(acc),
            lambda res: self._after_login(res, acc),
        )

    def _do_login(self) -> None:
        from orchestrator import cookie_sync

        acc = self._selected_account()
        if not acc:
            ui.toast(self.win, "Chọn một tài khoản trước đã.", "warn")
            return
        self.status.config(text=f"Đang mở Chrome cho {acc}...")
        self._run_bg(
            lambda: cookie_sync.login(acc),
            lambda out: self._after_action(out, "Chrome đã mở"),
        )

    def _open_cookie_dir(self) -> None:
        """Mở Explorer tại thư mục lưu file JSON cookie."""
        import os
        import subprocess

        from orchestrator import cookie_sync

        d = cookie_sync.cookie_archive_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(d))  # noqa: S606 - Windows shell open
        except Exception:
            try:
                subprocess.Popen(["explorer", str(d)])
            except Exception as e:
                ui.toast(self.win, f"Không mở được thư mục: {e}", "err")

    def _do_paste_cookie(self) -> None:
        """Mở popup dán cookie JSON trực tiếp."""
        from orchestrator import accounts as account_registry
        from orchestrator import cookie_popup

        acc = self._selected_account()
        if not acc:
            ui.toast(self.win, "Chọn một tài khoản trước đã.", "warn")
            return
        info = account_registry.get(acc) or {}
        pool = self._selected_pool() or info.get("pool") or acc
        email = info.get("email") or ""

        def on_save(account: str, raw: str) -> None:
            self.status.config(text=f"Đang cấy cookie cho {account}...")
            self._run_bg(
                lambda: account_registry.adopt_cookie_json(
                    account, raw, email=email, pool=pool
                ),
                lambda out: self._after_action(out, "Đã nhận cookie"),
            )

        cookie_popup.CookiePastePopup(
            self.win, account=acc, email=email, pool=pool, on_save=on_save
        )

    def _do_adopt(self) -> None:
        from tkinter import filedialog

        from orchestrator import cookie_sync

        acc = self._selected_account()
        if not acc:
            ui.toast(self.win, "Chọn một tài khoản trước đã.", "warn")
            return
        path = filedialog.askopenfilename(
            title="Chọn file export Cookie-Editor (JSON)",
            filetypes=[("JSON", "*.json"), ("Tất cả", "*.*")],
        )
        if not path:
            return
        from pathlib import Path

        pool = self._selected_pool() or cookie_sync.default_pool_for(acc)
        email = self.entry_email.get().strip() or None
        self.status.config(text=f"Đang nhận nuôi cookie cho {acc}...")
        self._run_bg(
            lambda: cookie_sync.adopt(
                acc, Path(path), pool_name=pool, account_email=email
            ),
            lambda out: self._after_action(out, "Đã nhận nuôi cookie"),
        )

    def _do_sync(self) -> None:
        from orchestrator import cookie_sync

        acc = self._selected_account()
        if not acc:
            ui.toast(self.win, "Chọn một tài khoản trước đã.", "warn")
            return
        pool = self._selected_pool() or cookie_sync.default_pool_for(acc)
        self.status.config(text=f"Đang đồng bộ cookie {acc}...")
        self._run_bg(
            lambda: cookie_sync.sync(acc, pool_name=pool),
            lambda out: self._after_action(out, "Đã đồng bộ cookie"),
        )

    def _do_check(self) -> None:
        """Gọi API thật để xem session còn hiệu lực không.

        QUAN TRỌNG: đọc cookie **live** từ Chrome riêng của account, không
        đọc pool file. Pool file có thể đã cũ (cookie bị Google rotate) →
        popup báo OK trong khi session thật đã chết, hoặc ngược lại.

        Nếu Chrome riêng không chạy được → fallback sang pool file.
        """
        from orchestrator import cookie_sync

        acc = self._selected_account()
        if not acc:
            ui.toast(self.win, "Chọn một tài khoản trước đã.", "warn")
            return
        pool = self._selected_pool() or cookie_sync.default_pool_for(acc)

        def work() -> dict:
            # 1. Thử cookie live từ Chrome riêng (nguồn sự thật).
            live: dict[str, str] = {}
            try:
                udd = cookie_sync.account_dir(acc)
                if udd.is_dir():
                    port = cookie_sync._find_free_port(
                        cookie_sync._account_port_base(acc)
                    )
                    with cookie_sync.ChromeSession(udd, port=port) as s:
                        live = s.cookies()
            except Exception:
                live = {}

            if cookie_sync.REQUIRED in live:
                cookies = cookie_sync._auth_cookies(live)
                out = cookie_sync._probe_session(cookies)
                out["source"] = "chrome_live"
                out["cookies_found"] = len(live)
                return out

            # 2. Fallback: cookie trong pool file.
            cookies = {}
            pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
            if pool_file.is_file():
                try:
                    data = json.loads(pool_file.read_text(encoding="utf-8"))
                    for c in data.get("cookies") or []:
                        if isinstance(c, dict) and c.get("name"):
                            cookies[c["name"]] = c["value"]
                except (OSError, ValueError):
                    pass
            out = cookie_sync._probe_session(cookies)
            out["source"] = "pool_file"
            return out

        self.status.config(text=f"Đang kiểm tra session {acc}...")
        self._run_bg(work, lambda out: self._after_action(out, "Kết quả kiểm tra session"))

    def _do_harvest_all(self) -> None:
        """Thu hoạch cookie cho MỌI account + tự chữa session chết.

        Đây là nút "một chạm" cho mục tiêu: mọi account đã đăng nhập luôn ở
        trạng thái AVAILABLE.
        """
        from orchestrator import harvest

        self.status.config(text="Đang thu hoạch cookie cho tất cả tài khoản...")
        self._run_bg(
            lambda: harvest.harvest_all(heal=True),
            lambda out: self._after_harvest(out),
        )

    def _do_bridge(self) -> None:
        """Mở cookie bridge — nhận cookie từ extension trong browser thật.

        Chrome/Edge ≥127 mã hoá cookie bằng App-Bound Encryption → không đọc
        được từ ngoài tiến trình. Cách hợp lệ duy nhất là extension chạy BÊN
        TRONG browser (dùng `chrome.cookies`) rồi POST về bridge loopback.
        """
        from orchestrator import cookie_bridge

        try:
            srv = cookie_bridge.BridgeServer(cookie_bridge.DEFAULT_PORT)
            srv.start()
            import threading

            threading.Thread(target=srv._watchdog, daemon=True).start()
        except OSError as e:
            messagebox.showerror(
                "Không mở được bridge",
                f"Port {cookie_bridge.DEFAULT_PORT} đã bị chiếm hoặc lỗi: {e}\n\n"
                "Có thể bridge đang chạy sẵn — dùng token trong tab bridge.",
            )
            return

        info = srv.info()
        # Giữ server sống cùng cửa sổ.
        self._bridge_server = srv

        messagebox.showinfo(
            "Cookie bridge đang chạy",
            f"Endpoint : {info['endpoint']}\n"
            f"Token    : {info['token']}\n\n"
            "Cài extension (một lần):\n"
            "1. Mở chrome://extensions (hoặc edge://extensions)\n"
            "2. Bật 'Developer mode'\n"
            "3. Bấm 'Load unpacked' → chọn thư mục:\n"
            f"   {info['extension_dir']}\n\n"
            "Sau đó: mở tab gemini.google.com đã đăng nhập → bấm icon extension → "
            "dán token → 'Gửi cookie'.\n\n"
            "Bridge tự tắt sau 15 phút không dùng.",
        )
        self.status.config(text="Cookie bridge đang chạy — dán token vào extension.")

        # Mở Explorer tại thư mục extension cho tiện.
        try:
            import os
            import subprocess

            os.startfile(info["extension_dir"])  # noqa: S606 — thư mục local
        except Exception:
            pass

    def _after_harvest(self, out: dict) -> None:
        """Hiển thị kết quả thu hoạch."""
        board.invalidate_quota_cache()
        self._refresh()

        total = out.get("total") or 0
        alive = out.get("alive") or 0
        dead = out.get("dead") or 0
        needs = out.get("needs_human") or 0

        lines = [f"Tổng: {total}  |  AVAILABLE: {alive}  |  chết: {dead}"]
        for r in out.get("results") or []:
            acc = r.get("account")
            st = r.get("account_status") or ("ok" if r.get("session_ok") else "?")
            mark = "OK " if r.get("session_ok") else "XX "
            extra = ""
            heal = r.get("heal") or {}
            if heal:
                extra = f" — {heal.get('state')}: {heal.get('reason', '')[:70]}"
            elif r.get("error"):
                extra = f" — {str(r['error'])[:70]}"
            lines.append(f"  {mark}{acc:<14} {st}{extra}")

        if needs:
            lines.append("")
            lines.append(
                f"{needs} tài khoản cần bạn đăng nhập tay (có 2FA/OAuth). "
                "Cửa sổ Chrome đã mở — đăng nhập xong bấm 'Đồng bộ cookie'."
            )

        msg = "\n".join(lines)
        if alive == total and total:
            ui.toast(self.win, f"OK: {alive}/{total} account AVAILABLE", "ok")
            messagebox.showinfo("Thu hoạch cookie", msg)
        else:
            ui.toast(self.win, f"{alive}/{total} account AVAILABLE", "warn")
            messagebox.showwarning("Thu hoạch cookie", msg)
        self.status.config(text="Xong")

    def _do_remove(self) -> None:
        """Xoá account khỏi registry (và tuỳ chọn xoá pool + credential)."""
        from orchestrator import accounts as account_registry

        acc = self._selected_account()
        if not acc:
            ui.toast(self.win, "Chọn một tài khoản trước đã.", "warn")
            return
        if not messagebox.askyesno(
            "Xoá tài khoản",
            f"Xoá account {acc!r} khỏi registry?\n\n"
            "Mật khẩu đã lưu cũng bị xoá.\n"
            "Cookie trong pool KHÔNG bị xoá (chọn Yes ở câu hỏi sau nếu muốn).",
        ):
            return
        delete_pool = messagebox.askyesno(
            "Xoá pool", f"Xoá luôn file pool của {acc!r}? (cookie sẽ mất)"
        )
        ok = account_registry.remove(acc, delete_pool=delete_pool)
        self._refresh()
        ui.toast(
            self.win,
            "Đã xoá." if ok else "Không tìm thấy account để xoá.",
            "ok" if ok else "warn",
        )

    def _after_login(self, out: dict, acc: str) -> None:
        """Hiển thị kết quả đăng nhập tự động, xử lý các state đặc biệt."""
        self._refresh()
        state = out.get("state")

        if state == "needs_human":
            ui.toast(self.win, out.get("reason") or "Cần bạn xử lý tay.", "warn")
            messagebox.showwarning(
                "Cần bạn xử lý",
                f"{out.get('reason')}\n\n{out.get('hint', '')}\n\n"
                "Sau khi xử lý xong: bấm 'Đồng bộ cookie'.",
            )
        elif state == "blocked":
            ui.toast(self.win, "Google chặn đăng nhập.", "err")
            messagebox.showerror(
                "Google chặn đăng nhập",
                f"{out.get('reason')}\n\n{out.get('hint', '')}",
            )
        elif out.get("ok"):
            msg = f"Đăng nhập thành công cho {acc}."
            if out.get("account_status"):
                msg += f"\ntrạng thái: {out['account_status']}"
            if out.get("cookies"):
                msg += f"\ncookie: {out['cookies']}"
            if out.get("detail"):
                msg += f"\n\n! {out['detail']}"
            messagebox.showinfo("Đăng nhập", msg)
        else:
            ui.toast(self.win, out.get("reason") or out.get("error") or "Thất bại.", "err")
            messagebox.showerror(
                "Đăng nhập thất bại",
                out.get("reason") or out.get("error") or "Không rõ nguyên nhân",
            )
        self.status.config(text="Xong")

    def _after_action(self, out: dict, title: str) -> None:
        """Hiển thị kết quả sau khi hành động xong."""
        # Cookie vừa đổi → cache quota cũ không còn đúng → phải xoá.
        # Nếu không, bảng ngoài vẫn hiện trạng thái cũ (UNAVAILABLE) trong
        # tối đa 120s → đúng triệu chứng "bấm kiểm tra OK nhưng bảng ngoài
        # vẫn nghi là unavailable".
        board.invalidate_quota_cache()
        self._refresh()
        if out.get("ok"):
            msg = f"{title} thành công."
            for key, label in (
                ("cookies", "cookie"),
                ("cookies_seeded", "cookie cấy"),
                ("cookies_live", "cookie live"),
                ("account_status", "trạng thái"),
                ("session_ok", "session"),
            ):
                if key in out:
                    msg += f"\n{label}: {out[key]}"
            if out.get("signed_in") is not None:
                msg += f"\nđã đăng nhập: {out['signed_in']}"
            if out.get("pool_file"):
                msg += f"\npool: {out['pool_file']}"
            if out.get("next_step"):
                msg += f"\n\n{out['next_step']}"
            if out.get("warning"):
                messagebox.showwarning(title, msg + f"\n\n! {out['warning']}")
            else:
                messagebox.showinfo(title, msg)
        else:
            ui.toast(self.win, out.get("error") or "Thất bại", "err")
            messagebox.showerror(title, out.get("error") or "Thất bại")
        self.status.config(text=title)


def main() -> int:
    OrchestratorGUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
