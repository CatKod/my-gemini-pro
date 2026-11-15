"""MCP server của Orchestrator — cổng gọi duy nhất cho agent.

Tool:
    orch_submit(kind, prompt, ...)   → tạo job chạy NỀN, trả về ngay job_id
    orch_status(job_id)               → trạng thái + log event (xem "đang làm gì")
    orch_result(job_id)               → kết quả cuối
    orch_list(state, kind)            → danh sách job
    orch_cancel(job_id)               → huỷ job
    orch_watch(job_ids)               → chờ job xong (dùng khi agent thực sự cần)
    orch_chrome_profile(action, ...)  → tạo/liệt kê hồ sơ Chrome
    orch_board(refresh_quota)         → snapshot toàn hệ thống (dashboard)
    orch_reap(grace)                  → dọn job mồ côi (runner đã chết)
    orch_account(action, ...)         → quản lý tài khoản Gemini (2 loại)

Điểm mấu chốt: `orch_submit` **không bao giờ chặn**. Job chạy ở process con
độc lập; agent làm việc khác rồi quay lại `orch_result` sau. Nếu agent cần
được đánh thức khi job xong, dùng `orch_watch` qua Cursor shell background —
Cursor tự inject notification khi tiến trình kết thúc.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import dispatch, store  # noqa: E402

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore

mcp = MCPServer(
    "orchestrator",
    instructions=(
        "Điều phối task dài cho Gemini (deep_research/chat/canvas) và "
        "Antigravity (antigravity) mà KHÔNG chặn agent.\n"
        "orch_submit trả về ngay job_id — agent làm việc khác rồi gọi "
        "orch_result sau. Muốn được đánh thức khi job xong: chạy nền "
        "'python -m orchestrator.orch_cli watch <job_id>'."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_view(job: dict[str, Any], *, with_events: bool = False) -> dict[str, Any]:
    view = {
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "state": job.get("state"),
        "title": job.get("title"),
        "pool": job.get("pool"),
        "account": job.get("account"),
        "error": job.get("error"),
        "report_path": job.get("report_path"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
    }
    if with_events:
        view["result"] = job.get("result")
        view["events"] = store.get_events(job.get("id"), limit=200)
    return view


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def orch_submit(
    kind: str,
    prompt: str,
    pool: str = "auto",
    model: str | None = None,
    title: str | None = None,
    timeout: int | None = None,
    params: dict[str, Any] | None = None,
    chat_metadata: list[str] | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Tạo một job chạy NỀN và trả về ngay (không chờ kết quả).

    Args:
        kind: `deep_research` | `chat` | `canvas` | `canvas_api` | `antigravity`
            | `chrome_profile`
            - `canvas` = điều khiển browser thật (Gemini Canvas, cần Chrome profile
              đã đăng nhập).
            - `canvas_api` = fallback qua API, không cần browser.
        prompt: nội dung task. Với `chrome_profile` thì bỏ trống (dùng `params.display_name`).
        pool: `auto` hoặc tên pool (`windows` / `wsl`).
        model: model tuỳ chọn (model Gemini, hoặc model Antigravity).
        title: tiêu đề job.
        timeout: giới hạn giây (deep_research mặc định 3600).
        params: tuỳ chọn riêng: `{"wsl": true}`, `{"yolo": true}`,
            `{"force_canvas": true}`, `{"display_name": "Acc 2"}`,
            `{"profile_directory": "Profile 2"}`, `{"headless": true}`.
        chat_metadata: **tiếp tục cùng hội thoại** (chỉ cho `kind="chat"`).
            Lấy từ `result.chat_metadata` của job chat trước (list
            `[cid, rid, rcid]`). Bỏ trống → tạo hội thoại mới.
        attachments: **đường dẫn file đính kèm** gửi lên Gemini (chỉ cho
            `kind="chat"`). Gemini đọc được file (code, csv, pdf...) và trả lời
            dựa trên nội dung đó. File Gemini sinh ra (`.xlsx`, `.pdf`, `.csv`)
            được tự động tải về `reports/downloads/` và trả kèm trong
            `result.files`.

    Returns:
        dict có `job_id` và `state`. Dùng `orch_result(job_id)` để lấy kết quả.
    """
    extra = dict(params or {})
    if model:
        extra["model"] = model
    if timeout:
        extra["timeout"] = timeout
    if chat_metadata:
        extra["chat_metadata"] = list(chat_metadata)
    if attachments:
        extra["attachments"] = [str(a) for a in attachments]
    try:
        res = dispatch.submit(
            kind,
            prompt,
            pool=None if pool == "auto" else pool,
            model=model,
            params=extra,
            title=title,
        )
    except ValueError as e:
        return {"status": "ERROR", "error": str(e)}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}
    return {"status": "SUBMITTED", **res}


@mcp.tool()
async def orch_status(job_id: str, with_events: bool = True) -> dict[str, Any]:
    """Trạng thái job + nhật ký tiến độ (để biết Gemini/Antigravity đang làm gì)."""
    job = store.get_job(job_id)
    if job is None:
        return {"status": "ERROR", "error": f"không tìm thấy job {job_id}"}
    return {"status": "OK", "job": _job_view(job, with_events=with_events)}


@mcp.tool()
async def orch_result(job_id: str) -> dict[str, Any]:
    """Kết quả cuối của job. Nếu job chưa xong, trả `state` + gợi ý cách chờ."""
    job = store.get_job(job_id)
    if job is None:
        return {"status": "ERROR", "error": f"không tìm thấy job {job_id}"}
    if job["state"] not in store.TERMINAL_STATES:
        return {
            "status": "PENDING",
            "job_id": job_id,
            "state": job["state"],
            "hint": (
                "Job chưa xong — KHÔNG poll. Chạy nền trong Cursor shell: "
                f"python -m orchestrator.orch_cli watch {job_id} "
                "(Cursor sẽ tự đánh thức bạn khi nó kết thúc)."
            ),
        }
    return {
        "status": "OK",
        "job_id": job_id,
        "state": job["state"],
        "title": job.get("title"),
        "report_path": job.get("report_path"),
        "error": job.get("error"),
        "result": job.get("result"),
    }


@mcp.tool()
async def orch_list(
    state: str | None = None, kind: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """Liệt kê job. `state` = `active` | `DONE` | `FAILED` | `CANCELLED` | None."""
    jobs = store.list_jobs(state=state, kind=kind, limit=limit)
    return {"status": "OK", "jobs": [_job_view(j) for j in jobs]}


@mcp.tool()
async def orch_cancel(job_id: str) -> dict[str, Any]:
    """Huỷ job đang chạy."""
    return {"status": "OK", **dispatch.cancel(job_id)}


@mcp.tool()
async def orch_watch(
    job_ids: list[str], timeout: int = 3600, poll: float = 3.0
) -> dict[str, Any]:
    """Chờ job kết thúc (BLOCKING — chỉ dùng khi thực sự cần kết quả ngay).

    Cách tốt hơn: chạy nền `python -m orchestrator.orch_cli watch <ids>` trong
    Cursor shell, Cursor sẽ tự đánh thức agent khi tiến trình kết thúc.
    """
    import time as _t

    deadline = _t.time() + timeout
    pending = list(job_ids)
    while _t.time() < deadline and pending:
        pending = [
            jid
            for jid in pending
            if (store.get_job(jid) or {}).get("state") not in store.TERMINAL_STATES
        ]
        if pending:
            await __import__("asyncio").sleep(poll)
    out = []
    for jid in job_ids:
        job = store.get_job(jid)
        out.append(_job_view(job) if job else {"job_id": jid, "state": "NOT_FOUND"})
    return {"status": "OK", "jobs": out}


@mcp.tool()
async def orch_chrome_profile(
    action: str = "list", display_name: str | None = None, open_after: bool = True
) -> dict[str, Any]:
    """Quản lý hồ sơ Chrome cho multi-account Google.

    `action` = `list` | `create`.

    `create` chỉ tạo hồ sơ trống — đăng nhập/2FA là người dùng tự làm.
    Chrome phải đang đóng lúc tạo, nếu không sẽ báo lỗi.
    """
    from orchestrator import chrome_profile

    try:
        if action == "list":
            return {"status": "OK", "profiles": chrome_profile.list_profiles()}
        if action == "create":
            res = chrome_profile.create_profile(display_name, open_after=open_after)
            return {"status": "OK", **res}
        return {"status": "ERROR", "error": f"action không hợp lệ: {action!r}"}
    except RuntimeError as e:
        return {"status": "ERROR", "error": str(e)}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def orch_board(refresh_quota: bool = False) -> dict[str, Any]:
    """Snapshot toàn hệ thống cho dashboard — 1 lần đọc, đủ mọi thứ.

    Trả về:
      - `jobs.active` / `jobs.recent` — job đang chạy + job gần đây
      - `accounts` — mỗi account: email, pool, số cookie, session_ok,
        `account_status`, quota headroom
      - `summary` — số đang chạy / xếp hàng / account còn sống

    `refresh_quota=True` ép đọc quota mới (gọi mạng, chậm ~2-5s) — mặc định
    `False` dùng cache 120s.
    """
    from orchestrator import board

    try:
        snap = board.snapshot(refresh_quota=refresh_quota)
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}
    return {"status": "OK", **snap}


@mcp.tool()
async def orch_reap(grace: float = 60.0) -> dict[str, Any]:
    """Dọn job mồ côi (runner đã chết nhưng job kẹt QUEUED/RUNNING).

    Chạy định kỳ để dashboard không hiện job "đang chạy" ma.
    """
    try:
        cleaned = store.reap_orphans(grace=grace)
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}
    return {"status": "OK", "cleaned": cleaned, "count": len(cleaned)}


@mcp.tool()
async def orch_account(
    action: str = "list",
    account: str | None = None,
    email: str | None = None,
    pool: str | None = None,
    password: str | None = None,
    cookie_json: str | None = None,
    auto_login: bool = False,
) -> dict[str, Any]:
    """Quản lý tài khoản Gemini (2 loại: có xác thực / chỉ email+mật khẩu).

    `action` = `list` | `add` | `show` | `status` | `login` | `paste-cookie`
    | `set-password` | `remove`.

    * `add`: tạo account trong registry. Có `password` → tài khoản loại 2
      (email+mật khẩu); không có → loại 1 (đăng nhập tay trong Chrome riêng).
    * `login`: đăng nhập tự động bằng email+mật khẩu (loại 2). Gặp CAPTCHA
      hoặc 2FA → trả `state="needs_human"`, KHÔNG bypass.
    * `paste-cookie`: dán JSON cookie (từ Cookie-Editor) trực tiếp. Không
      cần file tạm. `cookie_json` = nội dung JSON.
    * `status`: trạng thái session + pool + quota của 1 account.
    """
    from orchestrator import accounts as account_registry

    try:
        if action == "list":
            return {"status": "OK", "accounts": account_registry.list_accounts()}
        if action == "add":
            if not email:
                return {"status": "ERROR", "error": "thiếu `email`"}
            out = account_registry.add_account(
                email, account=account, pool=pool, password=password
            )
            if password and auto_login:
                from orchestrator import auto_login as al

                out["login"] = al.auto_login(out["account"])
                out["ok"] = bool(out["login"].get("ok"))
            return {"status": "OK", **out}
        if action == "show":
            out = account_registry.get(account or "")
            if out is None:
                return {"status": "ERROR", "error": f"không có account {account!r}"}
            return {"status": "OK", **out}
        if action == "status":
            from orchestrator import cookie_sync

            if not account:
                return {"status": "ERROR", "error": "thiếu `account`"}
            info = account_registry.get(account) or {}
            p = info.get("pool") or cookie_sync.default_pool_for(account)
            return {
                "status": "OK",
                "account": account,
                "registry": info,
                "cookie": cookie_sync.status(account, pool_name=p),
            }
        if action == "login":
            from orchestrator import auto_login as al

            if not account:
                return {"status": "ERROR", "error": "thiếu `account`"}
            return {"status": "OK", **al.auto_login(account)}
        if action == "paste-cookie":
            if not account:
                return {"status": "ERROR", "error": "thiếu `account`"}
            if not cookie_json:
                return {"status": "ERROR", "error": "thiếu `cookie_json`"}
            out = account_registry.adopt_cookie_json(
                account, cookie_json, email=email, pool=pool
            )
            return {"status": "OK", **out}
        if action == "set-password":
            if not account or not password:
                return {"status": "ERROR", "error": "thiếu `account` hoặc `password`"}
            return {
                "status": "OK",
                **account_registry.set_password(account, password),
            }
        if action == "remove":
            ok = account_registry.remove(account or "", delete_pool=False)
            return {"status": "OK", "ok": ok, "account": account}
        return {"status": "ERROR", "error": f"action không hợp lệ: {action!r}"}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def orch_cookie_sync(
    action: str = "sync",
    account: str = "mmeo",
    pool: str | None = None,
    export_path: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Đồng bộ cookie Gemini tự động — KHÔNG cần Cookie-Editor.

    `action` = `sync` | `adopt` | `status` | `login` | `archive` | `paste`.

    * `sync` (mặc định): mở Chrome riêng của orchestrator, đọc cookie live qua
      CDP, ghi vào `pools/<pool>.json`. Chạy bất cứ lúc nào cần cookie mới.
    * `adopt`: thao tác **1 lần** cho account mới — cấy cookie từ file export
      Cookie-Editor (`export_path`) vào Chrome riêng, rồi mọi lần sau dùng `sync`.
    * `login`: mở Chrome riêng để người dùng đăng nhập Google trực tiếp
      (thay cho Cookie-Editor). Sau khi đăng nhập + đóng Chrome → dùng `sync`.
    * `status`: xem trạng thái Chrome riêng + pool (không mở Chrome).

    Profile Chrome của user **không bao giờ bị đụng** — orchestrator dùng
    user-data-dir riêng tại `%LOCALAPPDATA%\\OrchestratorChrome\\<account>`.
    """
    from pathlib import Path as _Path

    from orchestrator import cookie_sync

    try:
        if action == "status":
            return {"status": "OK", **cookie_sync.status(account, pool_name=pool)}
        if action == "login":
            return {"status": "OK", **cookie_sync.login(account)}
        if action == "adopt":
            if not export_path:
                return {
                    "status": "ERROR",
                    "error": "adopt cần `export_path` (file JSON export Cookie-Editor)",
                }
            out = cookie_sync.adopt(
                account,
                _Path(export_path),
                pool_name=pool,
                account_email=email,
            )
            return {"status": "OK", **out}
        if action == "sync":
            out = cookie_sync.sync(account, pool_name=pool, account_email=email)
            return {"status": "OK", **out}
        if action == "archive":
            return {
                "status": "OK",
                "dir": str(cookie_sync.cookie_archive_dir()),
                "items": cookie_sync.list_cookie_archives(),
            }
        if action == "paste":
            if not export_path:
                return {
                    "status": "ERROR",
                    "error": (
                        "paste cần `export_path` là file chứa JSON cookie "
                        "(đọc nội dung file rồi dán vào GUI popup)"
                    ),
                }
            raw = _Path(export_path).read_text(encoding="utf-8")
            from orchestrator import accounts as account_registry

            out = account_registry.adopt_cookie_json(
                account, raw, email=email, pool=pool
            )
            return {"status": "OK", **out}
        return {"status": "ERROR", "error": f"action không hợp lệ: {action!r}"}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def orch_harvest(
    action: str = "all",
    account: str | None = None,
    pool: str | None = None,
    heal: bool = True,
) -> dict[str, Any]:
    """Thu hoạch cookie tự động cho mọi account — mục tiêu: **mọi acc đã đăng
    nhập luôn ở trạng thái AVAILABLE**.

    `action` = `all` | `account` | `list`.

    * `all`: quét mọi account trong registry → mở Chrome riêng của account đó,
      đọc cookie live qua CDP (`Storage.getCookies`), ghi `pools/<pool>.json`,
      rồi kiểm tra session bằng API thật.
    * `account`: chỉ 1 account.
    * `list`: liệt kê các profile Chrome riêng (không mở Chrome).

    `heal=True`: nếu session chết → tự đăng nhập lại (tài khoản loại 2 có
    mật khẩu) hoặc mở Chrome cho user xử lý (loại 1, có 2FA — **không** bypass).

    Lưu ý: profile Chrome **thật** của user không bao giờ bị đụng — Chrome ≥127
    mã hoá cookie bằng App-Bound Encryption nên không đọc được từ ngoài tiến trình.
    Orchestrator dùng profile riêng (`%LOCALAPPDATA%\\OrchestratorChrome\\<account>`).
    """
    from orchestrator import harvest

    try:
        if action == "list":
            return {"status": "OK", "profiles": harvest.list_profiles()}
        if action == "account":
            if not account:
                return {"status": "ERROR", "error": "thiếu `account`"}
            out = harvest.harvest_account(account, pool_name=pool, heal=heal)
            return {"status": "OK", **out}
        out = harvest.harvest_all(heal=heal)
        return {"status": "OK", **out}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def orch_bridge(
    action: str = "info",
    port: int = 9477,
    rotate: bool = False,
) -> dict[str, Any]:
    """Cookie bridge — lấy cookie từ **browser thật** của user (tự động hoá).

    Chrome/Edge ≥127 mã hoá cookie bằng App-Bound Encryption (`v20`) → **không**
    đọc được từ ngoài tiến trình (đã verify: đọc DB, DPAPI, copy profile,
    `--remote-debugging-port` trên default dir, junction — tất cả đều thất bại).

    Đường hợp lệ duy nhất: **extension chạy BÊN TRONG browser** dùng API
    `chrome.cookies` (đúng cách Cookie-Editor hoạt động). Extension POST về
    bridge trên `127.0.0.1`, bridge tự tìm pool ứng với cookie, ghi pool file,
    cấy vào Chrome riêng của orchestrator, xoá cache cũ → account AVAILABLE.

    `action` = `info` (token + hướng dẫn cài extension) | `start` (chạy server).

    Dùng:
      1. `orch_bridge(action="info")` → lấy `token` + `extension_dir`
      2. `orch_bridge(action="start")` → chạy server (chặn, nên gọi ở background)
      3. Trong Chrome: `chrome://extensions` → Developer mode → Load unpacked →
         chọn `extension_dir` → bấm icon → dán token → "Gửi cookie".
    """
    from orchestrator import cookie_bridge

    try:
        if action == "info":
            return {
                "status": "OK",
                "url": f"http://127.0.0.1:{port}",
                "endpoint": f"http://127.0.0.1:{port}/harvest",
                "token": cookie_bridge.current_token(rotate=rotate),
                "extension_dir": str(
                    cookie_bridge.paths.DATA_ROOT
                    / "browser_extension"
                    / "orchestrator-bridge"
                ),
                "hint": cookie_bridge.BridgeServer(port).info()["hint"],
            }
        if action == "start":
            srv = cookie_bridge.BridgeServer(port, rotate_token=rotate)
            srv.start()
            import threading

            threading.Thread(target=srv._watchdog, daemon=True).start()
            return {"status": "OK", **srv.info()}
        return {"status": "ERROR", "error": f"action không hợp lệ: {action!r}"}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
