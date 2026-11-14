"""GUI của Orchestrator — Tkinter, chạy nhẹ, mở khi cần quản lý.

Thiết kế cốt lõi: GUI là *view* của cùng 1 SQLite store mà runner ghi vào.
Đóng GUI không ảnh hưởng job đang chạy.

Tính năng:
    - Danh sách job (kind, state, pool, tiêu đề, thời lượng)
    - Xem log event của job đang chọn (tự refresh 2s)
    - Xem kết quả cuối
    - Nút: Tạo hồ sơ Chrome mới, Huỷ job, Mở report
    - **Quản lý tài khoản Gemini**: thêm account mới, đăng nhập, nhập cookie
      từ file Cookie-Editor, xem trạng thái session của từng account.

Chạy:
    python -m orchestrator.gui
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import dispatch, store  # noqa: E402

STATE_COLORS = {
    store.QUEUED: "#8a8a8a",
    store.RUNNING: "#1a73e8",
    store.DONE: "#188038",
    store.FAILED: "#d93025",
    store.CANCELLED: "#b06000",
}

KIND_LABELS = {
    "deep_research": "Deep Research",
    "chat": "Chat",
    "canvas": "Canvas",
    "antigravity": "Antigravity",
    "chrome_profile": "Chrome Profile",
}


class OrchestratorGUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Orchestrator")
        self.root.geometry("1180x720")
        self.root.minsize(900, 560)

        self._build_style()
        self._build_widgets()
        self._refresh()
        self.root.after(2000, self._tick)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=22)
        style.configure("Title.TLabel", font=("Segoe UI", 13, "bold"))

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Orchestrator", style="Title.TLabel").pack(side="left")
        ttk.Button(
            top, text="Tài khoản Gemini", command=self._open_accounts_window
        ).pack(side="right", padx=4)
        ttk.Button(
            top, text="Tạo hồ sơ Chrome", command=self._create_chrome_profile
        ).pack(side="right", padx=4)
        ttk.Button(
            top, text="Đồng bộ cookie", command=self._sync_cookie
        ).pack(side="right", padx=4)
        ttk.Button(top, text="Huỷ job", command=self._cancel_job).pack(side="right", padx=4)
        ttk.Button(top, text="Làm mới", command=self._refresh).pack(side="right", padx=4)

        # Cột trái: danh sách job
        left = ttk.Frame(self.root, padding=(10, 0))
        left.pack(side="left", fill="both", expand=True)

        self.filter_var = tk.StringVar(value="active")
        filters = ttk.Frame(left)
        filters.pack(fill="x", pady=(0, 4))
        for label, val in (
            ("Đang chạy", "active"),
            ("Tất cả", "all"),
            ("Xong", store.DONE),
            ("Lỗi", store.FAILED),
        ):
            ttk.Radiobutton(
                filters, text=label, value=val or "", variable=self.filter_var,
                command=self._refresh,
            ).pack(side="left", padx=(0, 8))

        cols = ("kind", "state", "title", "pool", "dur")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        heads = {
            "kind": ("Loại", 120),
            "state": ("Trạng thái", 90),
            "title": ("Tiêu đề", 300),
            "pool": ("Pool", 90),
            "dur": ("Thời lượng", 90),
        }
        for c, (h, w) in heads.items():
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure(store.RUNNING, foreground=STATE_COLORS[store.RUNNING])
        self.tree.tag_configure(store.DONE, foreground=STATE_COLORS[store.DONE])
        self.tree.tag_configure(store.FAILED, foreground=STATE_COLORS[store.FAILED])

        # Cột phải: log + kết quả
        right = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        right.pack(side="right", fill="both", expand=True)

        ttk.Label(right, text="Nhật ký tiến độ", font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        self.log_text = tk.Text(right, height=14, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(4, 10))

        ttk.Label(right, text="Kết quả", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.result_text = tk.Text(right, height=10, wrap="word", state="disabled")
        self.result_text.pack(fill="both", expand=True, pady=(4, 0))

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Mở report", command=self._open_report).pack(
            side="left", padx=(0, 6)
        )

        self.status = ttk.Label(self.root, text="", padding=(10, 4))
        self.status.pack(fill="x", side="bottom")

    # ------------------------------------------------------------------
    # Hành động
    # ------------------------------------------------------------------

    def _selected_job_id(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return sel[0]

    def _on_select(self, _event=None) -> None:
        jid = self._selected_job_id()
        if not jid:
            return
        job = store.get_job(jid)
        if job is None:
            return
        events = store.get_events(jid, limit=500)
        lines = []
        for e in events:
            stamp = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            tag = {"error": "!!", "warn": "! "}.get(e["level"], "  ")
            lines.append(f"{stamp} {tag} {e['message']}")
        self._set_text(self.log_text, "\n".join(lines))

        result = job.get("result")
        if result:
            import json

            self._set_text(self.result_text, json.dumps(result, indent=2, ensure_ascii=False))
        else:
            self._set_text(self.result_text, job.get("error") or "(chưa có kết quả)")

    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _refresh(self) -> None:
        raw_filter = self.filter_var.get()
        state_filter = None if raw_filter in ("", "all") else raw_filter
        jobs = store.list_jobs(state=state_filter, limit=200)
        keep = set(self.tree.get_children())
        for jid in list(keep):
            self.tree.delete(jid)
        for job in jobs:
            started = job.get("started_at")
            ended = job.get("ended_at")
            if started and ended:
                dur = f"{ended - started:.0f}s"
            elif started:
                dur = f"{time.time() - started:.0f}s…"
            else:
                dur = ""
            self.tree.insert(
                "",
                "end",
                iid=job["id"],
                values=(
                    KIND_LABELS.get(job["kind"], job["kind"]),
                    job["state"],
                    job.get("title") or (job.get("prompt") or "")[:60],
                    job.get("pool") or "",
                    dur,
                ),
                tags=(job["state"],),
            )
        active = sum(1 for j in store.list_jobs(state="active", limit=500))
        self.status.config(
            text=f"{len(jobs)} job  •  {active} đang chạy  •  DB: {store.db_path()}"
        )

    def _tick(self) -> None:
        self._refresh()
        if self._selected_job_id():
            self._on_select()
        self.root.after(2000, self._tick)

    def _cancel_job(self) -> None:
        jid = self._selected_job_id()
        if not jid:
            messagebox.showinfo("Huỷ", "Chọn một job trước đã.")
            return
        res = dispatch.cancel(jid)
        self._refresh()

    def _open_report(self) -> None:
        jid = self._selected_job_id()
        job = store.get_job(jid) if jid else None
        path = job.get("report_path") if job else None
        if not path or not Path(path).exists():
            messagebox.showinfo("Mở report", "Job này chưa có file report.")
            return
        opener = "open" if sys.platform == "darwin" else "start"
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "", path])
        else:
            subprocess.Popen([opener, path])

    def _create_chrome_profile(self) -> None:
        try:
            res = dispatch.submit(
                "chrome_profile", "", params={"display_name": None, "open_after": True}
            )
        except Exception as e:
            messagebox.showerror("Chrome", f"{type(e).__name__}: {e}")
            return
        if res.get("state") == store.FAILED:
            messagebox.showerror("Chrome", res.get("error") or "Thất bại")
            return
        result = res.get("result") or {}
        messagebox.showinfo(
            "Chrome",
            f"Đã tạo hồ sơ: {result.get('display_name')} "
            f"({result.get('directory')})\n\n{result.get('next_step', '')}",
        )
        self._refresh()

    def _sync_cookie(self) -> None:
        """Đồng bộ cookie Gemini tự động (không cần Cookie-Editor).

        Chạy nền trong thread để GUI không bị treo (Chrome mở mất vài giây).
        """
        import threading

        def worker() -> None:
            from orchestrator import cookie_sync

            try:
                out = cookie_sync.sync(self._cookie_account())
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            def done() -> None:
                if out.get("ok"):
                    messagebox.showinfo(
                        "Cookie",
                        f"Đã đồng bộ {out.get('cookies')} cookie\n"
                        f"→ {out.get('pool_file')}",
                    )
                else:
                    messagebox.showerror("Cookie", out.get("error") or "Thất bại")
                self._refresh()

            self.root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _cookie_account(self) -> str:
        """Tên account dùng cho Chrome riêng của orchestrator."""
        from orchestrator import cookie_sync

        pools = list(cookie_sync.POOLS_DIR.glob("*.json"))
        for p in pools:
            if p.name in ("example.json", cookie_sync.ACCOUNT_POOLS_FILE):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            profile = data.get("profile")
            if profile:
                return str(profile)
        return "mmeo"

    # ------------------------------------------------------------------
    # Quản lý tài khoản Gemini
    # ------------------------------------------------------------------

    def _open_accounts_window(self) -> None:
        """Mở cửa sổ quản lý tài khoản Gemini (cookie + pool)."""
        AccountsWindow(self.root, self)

    def run(self) -> None:
        self.root.mainloop()


class AccountsWindow:
    """Cửa sổ quản lý tài khoản Gemini — **dùng account registry làm nguồn sự thật**.

    Vì sao làm lại
    -------------
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
        self.win.geometry("980x640")
        self.win.minsize(820, 540)

        self._build()
        self._refresh()

    # -- UI ------------------------------------------------------------

    def _build(self) -> None:
        top = ttk.Frame(self.win, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Tài khoản Gemini", font=("Segoe UI", 12, "bold")).pack(
            side="left"
        )
        ttk.Button(top, text="Làm mới", command=self._refresh).pack(side="right", padx=4)

        # Bảng tài khoản
        cols = ("account", "pool", "email", "auth", "session", "last")
        self.tree = ttk.Treeview(
            self.win, columns=cols, show="headings", selectmode="browse"
        )
        heads = {
            "account": ("Account", 130),
            "pool": ("Pool", 90),
            "email": ("Email", 210),
            "auth": ("Loại", 190),
            "session": ("Session", 200),
            "last": ("Cập nhật lần cuối", 170),
        }
        for c, (h, w) in heads.items():
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        # Form thêm account
        form = ttk.LabelFrame(self.win, text="Thêm tài khoản mới", padding=8)
        form.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(form, text="Email:").grid(row=0, column=0, sticky="w")
        self.entry_email = ttk.Entry(form, width=26)
        self.entry_email.grid(row=0, column=1, sticky="w", padx=(4, 12))

        ttk.Label(form, text="Tên account:").grid(row=0, column=2, sticky="w")
        self.entry_account = ttk.Entry(form, width=16)
        self.entry_account.grid(row=0, column=3, sticky="w", padx=(4, 12))

        ttk.Label(form, text="Pool:").grid(row=0, column=4, sticky="w")
        self.entry_pool = ttk.Entry(form, width=12)
        self.entry_pool.grid(row=0, column=5, sticky="w", padx=(4, 4))

        ttk.Button(form, text="Thêm & đăng nhập", command=self._add_account).grid(
            row=0, column=6, padx=(6, 0)
        )

        # Form mật khẩu (loại 2)
        form2 = ttk.LabelFrame(
            self.win,
            text="Mật khẩu (tài khoản loại 2 — chỉ cần email + mật khẩu)",
            padding=8,
        )
        form2.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(form2, text="Account:").grid(row=0, column=0, sticky="w")
        self.entry_pw_account = ttk.Entry(form2, width=16)
        self.entry_pw_account.grid(row=0, column=1, sticky="w", padx=(4, 12))

        ttk.Label(form2, text="Mật khẩu:").grid(row=0, column=2, sticky="w")
        self.entry_password = ttk.Entry(form2, width=26, show="*")
        self.entry_password.grid(row=0, column=3, sticky="w", padx=(4, 12))

        ttk.Button(form2, text="Lưu & đăng nhập", command=self._do_auto_login).grid(
            row=0, column=4, padx=(6, 0)
        )

        ttk.Label(
            form2,
            text=(
                "Mật khẩu được mã hoá bằng DPAPI của Windows (chỉ user này, trên máy này "
                "giải mã được). Không bao giờ ghi plaintext."
            ),
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # Hành động
        btns = ttk.Frame(self.win, padding=(10, 0, 10, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="Đăng nhập tay (Chrome)", command=self._do_login).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btns, text="Nhập cookie (file)", command=self._do_adopt).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btns, text="Đồng bộ cookie", command=self._do_sync).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btns, text="Kiểm tra session", command=self._do_check).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btns, text="Xoá tài khoản", command=self._do_remove).pack(
            side="left", padx=(0, 6)
        )

        self.status = ttk.Label(self.win, text="", padding=(10, 4))
        self.status.pack(fill="x", side="bottom")

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

        # 1. Account trong registry (nguồn sự thật).
        registry = {a["account"]: a for a in account_registry.list_accounts()}

        # 2. Pool file có cookie nhưng chưa khai báo trong registry → hiện luôn
        #    để user không bỏ sót (trạng thái legacy).
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

            # Trạng thái pool: có cookie hay không.
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

            rows.append(
                (
                    acc,
                    pool,
                    info.get("email") or "",
                    auth,
                    session,
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
            messagebox.showinfo("Thêm tài khoản", "Nhập email trước đã.")
            return
        acc = self.entry_account.get().strip() or None
        pool = self.entry_pool.get().strip() or None
        password = self.entry_password.get().strip() or None

        if not password:
            if not messagebox.askyesno(
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
            messagebox.showerror("Thêm tài khoản", f"{type(e).__name__}: {e}")
            return

        acc_name = out["account"]
        self.entry_email.delete(0, "end")
        self.entry_account.delete(0, "end")
        self.entry_pool.delete(0, "end")
        self.entry_password.delete(0, "end")

        if not password:
            self._refresh()
            messagebox.showinfo(
                "Thêm tài khoản",
                f"Đã thêm account {acc_name!r} (loại 1).\n\n"
                "Bước tiếp theo: bấm 'Đăng nhập tay (Chrome)' và đăng nhập Google "
                "một lần. Sau đó cookie được đồng bộ tự động.",
            )
            return

        # Có mật khẩu -> tự đăng nhập.
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
            messagebox.showinfo("Đăng nhập", "Chọn một tài khoản trước đã.")
            return
        password = self.entry_password.get().strip()
        if not password:
            messagebox.showinfo("Đăng nhập", "Nhập mật khẩu trước đã.")
            return

        try:
            account_registry.set_password(acc, password)
        except Exception as e:
            messagebox.showerror("Lưu mật khẩu", f"{type(e).__name__}: {e}")
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
            messagebox.showinfo("Đăng nhập", "Chọn một tài khoản trước đã.")
            return
        self.status.config(text=f"Đang mở Chrome cho {acc}...")
        self._run_bg(
            lambda: cookie_sync.login(acc),
            lambda out: self._after_action(out, "Chrome đã mở"),
        )

    def _do_adopt(self) -> None:
        from tkinter import filedialog

        from orchestrator import cookie_sync

        acc = self._selected_account()
        if not acc:
            messagebox.showinfo("Nhập cookie", "Chọn một tài khoản trước đã.")
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
            messagebox.showinfo("Đồng bộ", "Chọn một tài khoản trước đã.")
            return
        pool = self._selected_pool() or cookie_sync.default_pool_for(acc)
        self.status.config(text=f"Đang đồng bộ cookie {acc}...")
        self._run_bg(
            lambda: cookie_sync.sync(acc, pool_name=pool),
            lambda out: self._after_action(out, "Đã đồng bộ cookie"),
        )

    def _do_check(self) -> None:
        """Gọi API thật để xem session còn hiệu lực không."""
        from orchestrator import cookie_sync

        acc = self._selected_account()
        if not acc:
            messagebox.showinfo("Kiểm tra", "Chọn một tài khoản trước đã.")
            return
        pool = self._selected_pool() or cookie_sync.default_pool_for(acc)
        pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
        cookies: dict[str, str] = {}
        if pool_file.is_file():
            try:
                data = json.loads(pool_file.read_text(encoding="utf-8"))
                for c in data.get("cookies") or []:
                    if isinstance(c, dict) and c.get("name"):
                        cookies[c["name"]] = c["value"]
            except (OSError, ValueError):
                pass
        self.status.config(text=f"Đang kiểm tra session {acc}...")
        self._run_bg(
            lambda: cookie_sync._probe_session(cookies),
            lambda out: self._after_action(out, "Kết quả kiểm tra session"),
        )

    def _do_remove(self) -> None:
        """Xoá account khỏi registry (và tuỳ chọn xoá pool + credential)."""
        from orchestrator import accounts as account_registry

        acc = self._selected_account()
        if not acc:
            messagebox.showinfo("Xoá", "Chọn một tài khoản trước đã.")
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
        messagebox.showinfo(
            "Xoá tài khoản",
            "Đã xoá." if ok else "Không tìm thấy account để xoá.",
        )

    def _after_login(self, out: dict, acc: str) -> None:
        """Hiển thị kết quả đăng nhập tự động, xử lý các state đặc biệt."""
        self._refresh()
        state = out.get("state")

        if state == "needs_human":
            messagebox.showwarning(
                "Cần bạn xử lý",
                f"{out.get('reason')}\n\n{out.get('hint', '')}\n\n"
                "Sau khi xử lý xong: bấm 'Đồng bộ cookie'.",
            )
        elif state == "blocked":
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
            messagebox.showerror(
                "Đăng nhập thất bại",
                out.get("reason") or out.get("error") or "Không rõ nguyên nhân",
            )
        self.status.config(text="Xong")

    def _after_action(self, out: dict, title: str) -> None:
        """Hiển thị kết quả sau khi hành động xong."""
        self._refresh()
        if out.get("ok"):
            msg = f"{title} thành công."
            for key, label in (
                ("cookies", "cookie"),
                ("cookies_seeded", "cookie cấy"),
                ("cookies_live", "cookie live"),
                ("account_status", "trạng thái"),
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
            messagebox.showerror(title, out.get("error") or "Thất bại")
        self.status.config(text=title)


def main() -> int:
    OrchestratorGUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
