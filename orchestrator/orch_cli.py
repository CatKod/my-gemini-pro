"""CLI của Orchestrator.

Lệnh quan trọng nhất cho agent: `wait` / `watch`. Khi chạy **nền** (block=0),
Cursor tự đẩy notification khi tiến trình kết thúc → agent được đánh thức
mà không phải poll.

Ví dụ (Cursor shell, background):
    python -m orchestrator.orch_cli watch j_abc123
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import dispatch, store  # noqa: E402


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _force_utf8_stdout() -> None:
    """Console Windows mặc định cp1252 → không in được tiếng Việt.

    Bản .exe đóng gói cũng dùng chung hàm này.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _fmt_job(job: dict[str, Any], *, verbose: bool = True) -> dict[str, Any]:
    out = {
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "state": job.get("state"),
        "title": job.get("title"),
        "pool": job.get("pool"),
        "error": job.get("error"),
    }
    if verbose:
        out["result"] = job.get("result")
        out["report_path"] = job.get("report_path")
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _load_params(arg: str | None) -> dict[str, Any]:
    """Parse `--params`.

    Hỗ trợ 2 dạng:
      - JSON inline:      --params '{"a":1}'
      - đường dẫn file:  --params @params.json
    """
    if not arg:
        return {}
    raw = arg
    if arg.startswith("@"):
        path = Path(arg[1:])
        raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"--params không phải JSON hợp lệ ({e}). "
            "Dùng --params '{\"key\": \"value\"}' hoặc --params @file.json"
        ) from e
    if not isinstance(data, dict):
        raise ValueError("--params phải là JSON object")
    return data


def cmd_submit(args: argparse.Namespace) -> int:
    try:
        params = _load_params(args.params)
    except ValueError as e:
        print(f"LỖI: {e}", file=sys.stderr)
        return 1
    if args.model:
        params["model"] = args.model
    if args.wsl:
        params["wsl"] = True
    if args.yolo is not None:
        params["yolo"] = args.yolo
    if args.timeout:
        params["timeout"] = args.timeout
    if args.force_canvas is not None:
        params["force_canvas"] = args.force_canvas
    if args.profile_directory:
        params["profile_directory"] = args.profile_directory
    if args.headless is not None:
        params["headless"] = args.headless

    res = dispatch.submit(
        args.kind,
        args.prompt,
        pool=args.pool,
        model=args.model,
        params=params,
        title=args.title,
    )
    _print(res)
    return 0 if res.get("state") != store.FAILED else 1


def cmd_status(args: argparse.Namespace) -> int:
    job = store.get_job(args.job_id)
    if job is None:
        print(f"không tìm thấy job {args.job_id}", file=sys.stderr)
        return 1
    events = store.get_events(args.job_id, after_id=args.after_event, limit=args.events)
    _print({"job": _fmt_job(job, verbose=True), "events": events})
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    job = store.get_job(args.job_id)
    if job is None:
        print(f"không tìm thấy job {args.job_id}", file=sys.stderr)
        return 1
    if job["state"] not in store.TERMINAL_STATES:
        print(
            json.dumps(
                {
                    "job_id": job["id"],
                    "state": job["state"],
                    "pending": True,
                    "hint": "Job chưa xong. Chạy nền: python -m orchestrator.orch_cli wait "
                    + job["id"],
                },
                ensure_ascii=False,
            )
        )
        return 2
    _print(_fmt_job(job, verbose=True))
    return 0 if job["state"] == store.DONE else 1


def cmd_list(args: argparse.Namespace) -> int:
    jobs = store.list_jobs(state=args.state, kind=args.kind, limit=args.limit)
    _print([_fmt_job(j, verbose=False) for j in jobs])
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _print(dispatch.cancel(args.job_id))
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    job = dispatch.wait(args.job_id, timeout=args.timeout, poll=args.poll)
    _print(_fmt_job(job, verbose=True))
    return 0 if job.get("state") == store.DONE else 1


def cmd_watch(args: argparse.Namespace) -> int:
    """Chờ nhiều job cùng lúc; kết thúc khi tất cả terminal."""
    deadline = time.time() + args.timeout
    ids = args.job_ids
    while time.time() < deadline:
        pending = []
        for jid in ids:
            job = store.get_job(jid)
            if job is None or job["state"] not in store.TERMINAL_STATES:
                pending.append(jid)
        if not pending:
            break
        time.sleep(args.poll)

    out = []
    for jid in ids:
        job = store.get_job(jid)
        if job is None:
            out.append({"job_id": jid, "state": "NOT_FOUND"})
        else:
            out.append(_fmt_job(job, verbose=True))
    _print(out)
    return 0 if all(o.get("state") == store.DONE for o in out) else 1


def cmd_purge(args: argparse.Namespace) -> int:
    n = store.purge(keep=args.keep)
    _print({"purged": n})
    return 0


def cmd_chrome(args: argparse.Namespace) -> int:
    from orchestrator import chrome_profile

    if args.chrome_cmd == "list":
        _print(chrome_profile.list_profiles())
        return 0
    try:
        res = chrome_profile.create_profile(
            args.name, open_after=not args.no_open
        )
    except RuntimeError as e:
        _print({"error": str(e)})
        return 1
    _print(res)
    return 0


def cmd_cookie(args: argparse.Namespace) -> int:
    """Đồng bộ cookie Gemini tự động (không cần Cookie-Editor)."""
    from pathlib import Path as _Path

    from orchestrator import cookie_sync

    try:
        if args.cookie_cmd == "sync":
            out = cookie_sync.sync(
                args.account, pool_name=args.pool, account_email=args.email
            )
        elif args.cookie_cmd == "adopt":
            if not args.export:
                print(
                    "LỖI: adopt cần --export <file JSON export Cookie-Editor>",
                    file=sys.stderr,
                )
                return 1
            out = cookie_sync.adopt(
                args.account,
                _Path(args.export),
                pool_name=args.pool,
                account_email=args.email,
            )
        elif args.cookie_cmd == "login":
            out = cookie_sync.login(args.account)
        else:
            out = cookie_sync.status(args.account, pool_name=args.pool)
    except Exception as e:
        _print({"error": f"{type(e).__name__}: {e}"})
        return 1
    _print(out)
    if args.cookie_cmd == "status":
        return 0
    return 0 if out.get("ok", True) else 1


def cmd_account(args: argparse.Namespace) -> int:
    """Quản lý tài khoản Gemini (thêm / đăng nhập / kiểm tra / xoá).

    Đây là **điểm vào duy nhất** để thêm tài khoản — thay vì phải nhớ cả
    `cookie adopt` + `cookie sync` + sửa `pools/*.json` thủ công.
    """
    from orchestrator import accounts as account_registry

    cmd = args.account_cmd

    try:
        if cmd == "add":
            out = account_registry.add_account(
                args.email,
                account=args.account,
                pool=args.pool,
                password=args.password,
            )
            # Nếu có mật khẩu → tự đăng nhập luôn (tài khoản loại 2).
            if args.password and args.auto_login:
                from orchestrator import auto_login

                login_result = auto_login.auto_login(
                    out["account"], keep_open=not args.no_keep_open
                )
                out["login"] = login_result
                out["ok"] = bool(login_result.get("ok"))
        elif cmd == "set-password":
            out = account_registry.set_password(args.account, args.password)
        elif cmd == "login":
            from orchestrator import auto_login

            out = auto_login.auto_login(args.account)
        elif cmd == "list":
            out = account_registry.list_accounts()
        elif cmd == "show":
            out = account_registry.get(args.account) or {
                "error": f"không tìm thấy account {args.account!r}"
            }
        elif cmd == "remove":
            out = {
                "ok": account_registry.remove(
                    args.account, delete_pool=args.delete_pool
                ),
                "account": args.account,
            }
        else:  # pragma: no cover - argparse chặn trước
            out = {"error": f"lệnh không hợp lệ: {cmd}"}
    except Exception as e:
        _print({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1

    _print(out)
    if isinstance(out, dict):
        return 0 if out.get("ok", True) else 1
    return 0


def cmd_runner(args: argparse.Namespace) -> int:
    """Chạy runner trong tiến trình hiện tại (lệnh nội bộ).

    Dùng khi bản `.exe` đã đóng gói cần spawn runner con: không có
    `python -m orchestrator.runners.<x>` nữa, nên ta gọi lại chính
    executable với `__runner__ <module> <job_id>`.
    """
    import importlib

    try:
        mod = importlib.import_module(f"orchestrator.runners.{args.module}")
    except ImportError as e:
        print(f"Không import được runner {args.module!r}: {e}", file=sys.stderr)
        return 2
    sys.argv = [f"orchestrator.runners.{args.module}", args.job_id]
    return int(mod.main())


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="orch_cli", description="Orchestrator — chạy task Gemini/Antigravity nền."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit", help="Tạo job và chạy nền")
    p.add_argument("kind", choices=sorted(dispatch.RUNNER_FOR_KIND))
    p.add_argument("prompt")
    p.add_argument("--pool", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--params", default=None, help="JSON object")
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--wsl", action="store_true")
    p.add_argument("--yolo", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--force-canvas", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument(
        "--profile-directory", default=None, help="Chrome profile cho canvas (browser)"
    )
    p.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=None,
        help="Chạy browser ẩn (canvas)",
    )
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status", help="Trạng thái + event log")
    p.add_argument("job_id")
    p.add_argument("--after-event", type=int, default=0)
    p.add_argument("--events", type=int, default=100)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("result", help="Kết quả job (thoát code 2 nếu chưa xong)")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("list", help="Danh sách job")
    p.add_argument("--state", default=None, help="active | DONE | FAILED | ...")
    p.add_argument("--kind", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("cancel", help="Huỷ job")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("wait", help="Chờ 1 job xong (chạy nền để được wake-up)")
    p.add_argument("job_id")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--poll", type=float, default=3.0)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("watch", help="Chờ nhiều job xong")
    p.add_argument("job_ids", nargs="+")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--poll", type=float, default=3.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("purge", help="Xoá job cũ")
    p.add_argument("--keep", type=int, default=200)
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("chrome", help="Quản lý hồ sơ Chrome")
    p.add_argument("chrome_cmd", choices=["list", "create"])
    p.add_argument("--name", default=None)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_chrome)

    p = sub.add_parser(
        "cookie",
        help="Đồng bộ cookie Gemini tự động (không cần Cookie-Editor)",
    )
    p.add_argument("cookie_cmd", choices=["sync", "adopt", "status", "login"])
    p.add_argument("--account", default="mmeo", help="tên account (thư mục Chrome riêng)")
    p.add_argument("--pool", default=None, help="tên pool trong pools/")
    p.add_argument("--email", default=None, help="email tài khoản")
    p.add_argument("--export", default=None, help="file JSON export Cookie-Editor (cho adopt)")
    p.set_defaults(func=cmd_cookie)

    # Lệnh nội bộ: dùng để spawn runner khi chạy từ `.exe` đã đóng gói.
    # Khi frozen, `sys.executable` chính là file .exe — không thể dùng
    p = sub.add_parser(
        "account",
        help="Quản lý tài khoản Gemini (add / login / list / remove)",
    )
    p.add_argument(
        "account_cmd",
        choices=["add", "set-password", "login", "list", "show", "remove"],
    )
    p.add_argument("--email", default=None, help="email tài khoản Google")
    p.add_argument("--account", default=None, help="tên account (mặc định: suy từ email)")
    p.add_argument("--pool", default=None, help="tên pool trong pools/")
    p.add_argument(
        "--password",
        default=None,
        help="mật khẩu (lưu mã hoá DPAPI; truyền trực tiếp để tránh lưu)",
    )
    p.add_argument(
        "--no-auto-login",
        dest="auto_login",
        action="store_false",
        help="không tự đăng nhập sau khi thêm account",
    )
    p.add_argument(
        "--no-keep-open",
        dest="no_keep_open",
        action="store_true",
        help="đóng Chrome sau khi đăng nhập (mặc định giữ mở để xử lý tay)",
    )
    p.add_argument(
        "--delete-pool",
        action="store_true",
        help="xoá luôn file pool khi remove account",
    )
    p.set_defaults(func=cmd_account, auto_login=True)

    # `python -m orchestrator.runners.<x>` nữa. Thay vào đó ta gọi lại chính
    # executable với lệnh này.
    p = sub.add_parser("__runner__", help=argparse.SUPPRESS)
    p.add_argument("module")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_runner)

    return ap


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
