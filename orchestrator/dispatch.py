"""Dispatcher — submit job, spawn runner detached, theo dõi, huỷ.

Đây là tầng giữa giữa MCP server (gọi bởi agent) và runner (process con).

Vòng đời:
    submit()  → tạo row trong SQLite + spawn process con (DETACHED, không chặn)
    runner   → tự ghi event + kết thúc job trong DB
    wait()   → (tuỳ chọn) chờ job kết thúc, dùng cho CLI/test

Vì sao detach được:
    - `subprocess.CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` trên Windows,
      `start_new_session=True` trên POSIX → tiến trình con sống độc lập với
      tiến trình cha (MCP server có thể tắt).
    - Runner ghi kết quả vào SQLite, nên agent có thể đọc lại bất kỳ lúc nào.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Job nào chạy bằng runner nào.
#
# `canvas` dùng browser thật (Canvas chỉ tồn tại trên web app, `gemini-webapi`
# không có API cho nó). `canvas_api` là đường fallback qua API, dùng khi
# account không có model sinh artifact (chỉ `gemini-flash-lite`).
RUNNER_FOR_KIND = {
    "deep_research": "gemini",
    "chat": "gemini",
    "canvas": "canvas",
    "canvas_api": "gemini",
    "antigravity": "agy",
    "chrome_profile": "chrome",
}

# Job kinds chạy inline (nhanh, không cần process con).
INLINE_KINDS = {"chrome_profile"}


def _python_exe() -> str:
    return sys.executable or "python"


def _spawn_args(module: str, job_id: str) -> list[str]:
    """Lệnh để chạy runner.

    - Chạy từ source: `python -m orchestrator.runners.<module> <job_id>`.
    - Chạy từ `.exe` (frozen): `sys.executable` chính là file .exe, nên
      `-m orchestrator.runners.<module>` không có nghĩa. Thay vào đó gọi lại
      chính executable với lệnh nội bộ `__runner__ <module> <job_id>`.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "__runner__", module, job_id]
    return [_python_exe(), "-u", "-m", f"orchestrator.runners.{module}", job_id]


def _spawn(module: str, job_id: str, log: Path) -> subprocess.Popen:
    """Spawn runner như process con độc lập."""
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", encoding="utf-8", buffering=1)

    kwargs: dict[str, Any] = {
        "stdout": fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if getattr(sys, "frozen", False):
        # Bản .exe: `REPO_ROOT` là `<dist>/orchestrator-cli/_internal` — không
        # có `pools/` ở đó. Kế thừa cwd của tiến trình cha (thư mục user
        # đang chạy lệnh) để `pools/`/`reports/` được resolve đúng.
        kwargs["cwd"] = os.getcwd()
    else:
        kwargs["cwd"] = str(REPO_ROOT)
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        )
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    return subprocess.Popen(_spawn_args(module, job_id), **kwargs)


def submit(
    kind: str,
    prompt: str,
    *,
    pool: str | None = None,
    model: str | None = None,
    params: dict[str, Any] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Tạo job và khởi chạy. Trả về thông tin job (không chờ kết quả)."""
    if kind not in RUNNER_FOR_KIND:
        raise ValueError(
            f"kind={kind!r} không hợp lệ. Hợp lệ: {sorted(RUNNER_FOR_KIND)}"
        )
    if not prompt.strip() and kind not in INLINE_KINDS:
        raise ValueError("prompt không được rỗng.")

    job_id = store.create_job(
        kind, prompt, pool=pool, model=model, params=params, title=title
    )

    if kind in INLINE_KINDS:
        store.update_job(job_id, state=store.RUNNING, started_at=time.time())
        try:
            from orchestrator import chrome_profile

            if kind == "chrome_profile":
                result = chrome_profile.create_profile(
                    (params or {}).get("display_name"),
                    open_after=(params or {}).get("open_after", True),
                )
            else:  # pragma: no cover
                raise ValueError(kind)
        except Exception as e:
            store.finish_job(job_id, store.FAILED, error=f"{type(e).__name__}: {e}")
            return {
                "job_id": job_id,
                "state": store.FAILED,
                "error": f"{type(e).__name__}: {e}",
            }
        store.finish_job(job_id, store.DONE, result=result, title=result.get("display_name"))
        return {"job_id": job_id, "state": store.DONE, "result": result}

    module = RUNNER_FOR_KIND[kind]
    log = store.log_path(job_id)
    try:
        proc = _spawn(module, job_id, log)
    except Exception as e:
        store.finish_job(job_id, store.FAILED, error=f"Không spawn được runner: {e}")
        return {"job_id": job_id, "state": store.FAILED, "error": str(e)}

    store.update_job(job_id, pid=proc.pid)
    store.add_event(
        job_id,
        f"Đã spawn runner {module} (pid={proc.pid})",
        kind="status",
    )
    return {
        "job_id": job_id,
        "state": store.QUEUED,
        "pid": proc.pid,
        "log_path": str(log),
        "hint": (
            "Job chạy nền. Đọc kết quả sau bằng orch_result(job_id). "
            "Không cần poll — dùng orch_watch để chờ tất cả job hoàn tất."
        ),
    }


def wait(job_id: str, timeout: float = 3600.0, poll: float = 2.0) -> dict[str, Any]:
    """Chờ job kết thúc (dùng cho CLI hoặc test, không dùng trong tool agent)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.get_job(job_id)
        if job and job["state"] in store.TERMINAL_STATES:
            return job
        time.sleep(poll)
    job = store.get_job(job_id)
    return job or {"id": job_id, "state": "TIMEOUT"}


def cancel(job_id: str) -> dict[str, Any]:
    """Huỷ job đang chạy."""
    job = store.get_job(job_id)
    if job is None:
        return {"ok": False, "error": "không tìm thấy job"}
    if job["state"] in store.TERMINAL_STATES:
        return {"ok": False, "error": f"job đã ở trạng thái {job['state']}"}

    killed = False
    pid = job.get("pid")
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=30,
                )
            else:
                os.kill(pid, 9)
            killed = True
        except Exception:
            killed = False

    store.finish_job(job_id, store.CANCELLED, error="bị huỷ thủ công")
    store.add_event(job_id, "Job bị huỷ thủ công", level="warn", kind="status")
    return {"ok": True, "killed": killed, "state": store.CANCELLED}
