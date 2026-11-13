"""MCP server của Orchestrator — cổng gọi duy nhất cho agent.

Tool:
    orch_submit(kind, prompt, ...)   → tạo job chạy NỀN, trả về ngay job_id
    orch_status(job_id)               → trạng thái + log event (xem "đang làm gì")
    orch_result(job_id)               → kết quả cuối
    orch_list(state, kind)            → danh sách job
    orch_cancel(job_id)               → huỷ job
    orch_watch(job_ids)               → chờ job xong (dùng khi agent thực sự cần)
    orch_chrome_profile(action, ...)  → tạo/liệt kê hồ sơ Chrome

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

    Returns:
        dict có `job_id` và `state`. Dùng `orch_result(job_id)` để lấy kết quả.
    """
    extra = dict(params or {})
    if model:
        extra["model"] = model
    if timeout:
        extra["timeout"] = timeout
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
async def orch_cookie_sync(
    action: str = "sync",
    account: str = "mmeo",
    pool: str | None = None,
    export_path: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Đồng bộ cookie Gemini tự động — KHÔNG cần Cookie-Editor.

    `action` = `sync` | `adopt` | `status` | `login`.

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
        return {"status": "ERROR", "error": f"action không hợp lệ: {action!r}"}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
