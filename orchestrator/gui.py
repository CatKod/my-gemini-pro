"""GUI của Orchestrator — Tkinter, chạy nhẹ, mở khi cần quản lý.

Thiết kế cốt lõi: GUI là *view* của cùng 1 SQLite store mà runner ghi vào.
Đóng GUI không ảnh hưởng job đang chạy.

Tính năng:
    - Danh sách job (kind, state, pool, tiêu đề, thời lượng)
    - Xem log event của job đang chọn (tự refresh 2s)
    - Xem kết quả cuối
    - Nút: Tạo hồ sơ Chrome mới, Huỷ job, Mở report

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
            if p.name == "example.json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            profile = data.get("profile")
            if profile:
                return str(profile)
        return "mmeo"

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    OrchestratorGUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
