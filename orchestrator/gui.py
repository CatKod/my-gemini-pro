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
    """Cửa sổ quản lý tài khoản Gemini.

    Cho phép:
      - Thêm account mới (tên + email + pool) → tạo pool rỗng sẵn sàng.
      - **Đăng nhập**: mở Chrome riêng để user đăng nhập Google (1 lần).
      - **Nhập cookie từ file**: dùng file Cookie-Editor export (JSON).
      - **Đồng bộ**: lấy cookie live từ Chrome riêng → ghi pool.
      - Xem trạng thái: cookie trong DB, pool, session còn hiệu lực không.
    """

    def __init__(self, parent: tk.Misc, app: OrchestratorGUI) -> None:
        self.app = app
        self.win = tk.Toplevel(parent)
        self.win.title("Tài khoản Gemini")
        self.win.geometry("920x560")
        self.win.minsize(760, 460)

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
        cols = ("account", "pool", "email", "profile", "db", "pool_state")
        self.tree = ttk.Treeview(self.win, columns=cols, show="headings", selectmode="browse")
        heads = {
            "account": ("Account", 130),
            "pool": ("Pool", 90),
            "email": ("Email", 190),
            "profile": ("Chrome riêng", 150),
            "db": ("Cookie DB", 80),
            "pool_state": ("Trạng thái pool", 260),
        }
        for c, (h, w) in heads.items():
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        # Form thêm account
        form = ttk.LabelFrame(self.win, text="Thêm tài khoản mới", padding=8)
        form.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(form, text="Tên account:").grid(row=0, column=0, sticky="w")
        self.entry_account = ttk.Entry(form, width=18)
        self.entry_account.grid(row=0, column=1, sticky="w", padx=(4, 12))

        ttk.Label(form, text="Pool:").grid(row=0, column=2, sticky="w")
        self.entry_pool = ttk.Entry(form, width=14)
        self.entry_pool.grid(row=0, column=3, sticky="w", padx=(4, 12))

        ttk.Label(form, text="Email:").grid(row=0, column=4, sticky="w")
        self.entry_email = ttk.Entry(form, width=26)
        self.entry_email.grid(row=0, column=5, sticky="w", padx=(4, 4))

        ttk.Button(form, text="Thêm", command=self._add_account).grid(
            row=0, column=6, padx=(6, 0)
        )

        ttk.Label(
            form,
            text=(
                "Tên account = tên thư mục Chrome riêng (%LOCALAPPDATA%\\OrchestratorChrome\\<tên>). "
                "Pool = tên file trong pools/ (mặc định windows/wsl)."
            ),
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))

        # Hành động
        btns = ttk.Frame(self.win, padding=(10, 0, 10, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="Đăng nhập (Chrome)", command=self._do_login).pack(
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
        ttk.Button(btns, text="Xoá pool", command=self._do_delete_pool).pack(
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
        from orchestrator import cookie_sync

        rows: list[tuple[str, str, str, str, str, str]] = []
        names = set(cookie_sync.ACCOUNT_POOLS.keys())

        # Account đã khai báo trong pools/ (có "profile" = tên account).
        pool_states: dict[str, str] = {}
        for pf in sorted(cookie_sync.POOLS_DIR.glob("*.json")):
            if pf.name in ("example.json", cookie_sync.ACCOUNT_POOLS_FILE):
                continue
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            acc = str(data.get("profile") or pf.stem)
            names.add(acc)
            cookies = data.get("cookies") or []
            pool_states[acc] = (
                f"{pf.name} — {len(cookies)} cookie"
                + (f" — {data.get('account_email')}" if data.get("account_email") else "")
            )

        for acc in sorted(names):
            pool = cookie_sync.default_pool_for(acc)
            st = cookie_sync.status(acc, pool_name=pool)
            db = (
                "n/a"
                if st.get("cookies_in_db", 0) < 0
                else f"{st.get('cookies_in_db', 0)} cookie"
            )
            rows.append(
                (
                    acc,
                    pool,
                    st.get("account_email") or "",
                    st.get("user_data_dir") or "",
                    db,
                    pool_states.get(acc, "chưa có pool"),
                )
            )

        for jid in self.tree.get_children():
            self.tree.delete(jid)
        for r in rows:
            self.tree.insert("", "end", values=r)
        self.status.config(text=f"{len(rows)} tài khoản")

    def _add_account(self) -> None:
        from orchestrator import cookie_sync

        acc = self.entry_account.get().strip()
        if not acc:
            messagebox.showinfo("Thêm tài khoản", "Nhập tên account trước đã.")
            return
        pool = self.entry_pool.get().strip() or cookie_sync.default_pool_for(acc)
        email = self.entry_email.get().strip() or None

        pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
        if pool_file.is_file():
            if not messagebox.askyesno(
                "Pool đã tồn tại",
                f"Pool {pool!r} đã có cookie. Ghi đè?",
            ):
                return
        payload = {
            "name": pool,
            "account_email": email,
            "browser": "chrome",
            "profile": acc,
            "source": "orchestrator-gui",
            "cookies": [],
        }
        cookie_sync._write_json_atomic(pool_file, payload)

        # Ghi nhớ mapping account → pool cho các lần sau.
        cookie_sync.set_account_pool(acc, pool)

        self.entry_account.delete(0, "end")
        self.entry_pool.delete(0, "end")
        self.entry_email.delete(0, "end")
        self._refresh()
        messagebox.showinfo(
            "Thêm tài khoản",
            f"Đã thêm account {acc!r} → pool {pool!r}.\n\n"
            "Bước tiếp theo: bấm 'Đăng nhập (Chrome)' để đăng nhập Google, "
            "hoặc 'Nhập cookie (file)' nếu bạn đã export từ Cookie-Editor.",
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

    def _do_delete_pool(self) -> None:
        from orchestrator import cookie_sync

        acc = self._selected_account()
        if not acc:
            messagebox.showinfo("Xoá", "Chọn một tài khoản trước đã.")
            return
        pool = self._selected_pool() or cookie_sync.default_pool_for(acc)
        pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
        if not pool_file.is_file():
            messagebox.showinfo("Xoá", f"Pool {pool!r} không có file.")
            return
        if not messagebox.askyesno(
            "Xoá pool", f"Xoá file pool {pool_file.name}?\nCookie sẽ phải nhập lại."
        ):
            return
        pool_file.unlink(missing_ok=True)
        self._refresh()

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
                messagebox.showwarning(title, msg + f"\n\n⚠ {out['warning']}")
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
