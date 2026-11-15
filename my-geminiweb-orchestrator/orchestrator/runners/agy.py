"""Runner: Antigravity CLI (`agy`) — chạy detached, stream NDJSON về DB.

Format thật của `agy --output-format stream-json` (đã capture trên máy):
    {"event":"init","conversation_id":"<uuid>","init":{...}}
    {"event":"step_update","step_update":{"conversation_id":..,"step_index":0,
        "state":"DONE","step_type":"user_input", ...}}
    {"event":"result","result":{"conversation_id":..,"status":"SUCCESS"|"ERROR",
        "response":"<text>","error":null,"duration_seconds":N,"num_turns":N,
        "usage":{...},"finished_reason":"..."}}

Runner parse từng dòng NDJSON, đẩy event vào SQLite để dashboard hiện
tiến độ theo thời gian thực, và kết thúc job khi gặp event `result`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator import store  # noqa: E402

DEFAULT_TIMEOUT = 1800
AGY_WIN = "agy.exe"
WSL_DISTRO = "Ubuntu"


def _emit(job_id: str, message: str, *, level: str = "info", kind: str | None = None) -> None:
    store.add_event(job_id, message, level=level, kind=kind)
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _build_cmd(params: dict[str, Any], prompt: str, timeout: int) -> list[str]:
    """Dựng lệnh agy cho Windows hoặc WSL2."""
    use_wsl = bool(params.get("wsl"))
    model = params.get("model")
    effort = params.get("effort")
    yolo = bool(params.get("yolo", True))
    cwd = params.get("cwd") or str(Path.cwd())

    if use_wsl:
        wsl_cwd = cwd.replace("\\", "/")
        if len(wsl_cwd) > 1 and wsl_cwd[1] == ":":
            wsl_cwd = f"/mnt/{wsl_cwd[0].lower()}{wsl_cwd[2:]}"
        args = [
            "wsl.exe", "-d", params.get("wsl_distro") or WSL_DISTRO, "--cd", wsl_cwd, "--",
            params.get("agy_bin") or "agy",
            "-p", prompt,
            "--output-format", "stream-json",
            "--print-timeout", f"{timeout}s",
        ]
    else:
        args = [
            params.get("agy_bin") or AGY_WIN,
            "-p", prompt,
            "--output-format", "stream-json",
            "--print-timeout", f"{timeout}s",
        ]

    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if params.get("continue_conversation"):
        args.append("--continue")
    if yolo:
        args.append("--dangerously-skip-permissions")
    return args


def _handle_line(job_id: str, line: str, state: dict[str, Any]) -> None:
    """Parse 1 dòng NDJSON, ghi event, cập nhật state."""
    line = line.strip()
    if not line:
        return
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return

    event = payload.get("event")
    if event == "init":
        state["conversation_id"] = payload.get("conversation_id")
        init = payload.get("init") or {}
        state["cwd"] = init.get("cwd")
        state["permission_mode"] = init.get("permission_mode")
        store.update_job(job_id, external_id=payload.get("conversation_id"))
        _emit(
            job_id,
            f"agy init: conversation={payload.get('conversation_id')} "
            f"cwd={init.get('cwd')} mode={init.get('permission_mode')}",
            kind="status",
        )
    elif event == "step_update":
        su = payload.get("step_update") or {}
        state["last_step"] = su.get("step_type")
        state["last_step_index"] = su.get("step_index")

        step_type = su.get("step_type") or "?"
        delta = su.get("text_delta")
        st = su.get("state")

        if delta:
            # Streaming: chỉ log mẫu đầu, không spam DB.
            if not state.get("_delta_seen"):
                state["_delta_seen"] = True
                _emit(job_id, f"[{step_type}] {delta[:200]}", kind="thinking")
            state.setdefault("_text", "")
            state["_text"] += delta
        elif st == "DONE":
            label = su.get("title") or su.get("name") or step_type
            _emit(job_id, f"step {su.get('step_index')} DONE: {label}"[:300], kind="thinking")
        else:
            label = su.get("title") or su.get("name") or step_type
            _emit(job_id, f"step {su.get('step_index')} [{st}] {label}"[:300], kind="thinking")

        if su.get("duration_seconds") is not None:
            state["last_duration"] = su["duration_seconds"]
        if su.get("usage"):
            state["usage"] = su["usage"]
    elif event == "result":
        res = payload.get("result") or {}
        state["result"] = res
    elif event:
        _emit(job_id, f"{event}: {line[:400]}", kind="thinking")


def run(job_id: str, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
    timeout = int(params.get("timeout") or DEFAULT_TIMEOUT)
    cmd = _build_cmd(params, prompt, timeout)
    _emit(job_id, f"Chạy: {' '.join(cmd[:6])} ...", kind="status")

    state: dict[str, Any] = {}
    stderr_buf: list[str] = []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    store.update_job(job_id, pid=proc.pid)

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_buf.append(line.rstrip())
            if len(stderr_buf) > 200:
                stderr_buf.pop(0)

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        _handle_line(job_id, line, state)
        store.heartbeat(job_id)

    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    result = state.get("result") or {}
    stderr_text = "\n".join(stderr_buf[-40:])
    status = (result.get("status") or "").upper()

    if status == "SUCCESS":
        response = result.get("response") or state.get("_text") or ""
        _emit(job_id, f"agy SUCCESS — {len(response)} ký tự", kind="result")
        payload = {
            "response": response,
            "conversation_id": result.get("conversation_id"),
            "duration_seconds": result.get("duration_seconds"),
            "num_turns": result.get("num_turns"),
            "usage": result.get("usage"),
            "finished_reason": result.get("finished_reason"),
            "stderr": stderr_text or None,
        }
        store.finish_job(
            job_id,
            store.DONE,
            result=payload,
            external_id=result.get("conversation_id"),
        )
        return {"ok": True}

    error = result.get("error") or stderr_text or f"agy kết thúc với status={status!r}"
    partial = (result.get("response") or "").strip()
    if partial:
        # Antigravity có thể đã trả lời xong nhưng một bước sau lỗi (ví dụ hết
        # capacity model). Giữ phần đã có để agent dùng được, nhưng vẫn báo lỗi.
        error = f"{error} | partial_response: {partial[:300]}"
    _emit(job_id, f"agy lỗi: {error}", level="error", kind="error")
    store.finish_job(
        job_id,
        store.FAILED,
        result={
            "response": result.get("response") or "",
            "conversation_id": result.get("conversation_id"),
            "finished_reason": result.get("finished_reason"),
            "stderr": stderr_text or None,
        },
        error=error,
    )
    return {"ok": False, "error": error}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m orchestrator.runners.agy <job_id>", flush=True)
        return 2
    job = store.get_job(sys.argv[1])
    if job is None:
        print("job không tồn tại", flush=True)
        return 2
    if not store.claim_job(sys.argv[1], pid=os.getpid()):
        print("job đã được claim", flush=True)
        return 3
    _emit(sys.argv[1], f"Bắt đầu job antigravity", kind="status")
    try:
        run(sys.argv[1], job["prompt"] or "", job.get("params") or {})
        return 0
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        _emit(sys.argv[1], f"LỖI: {detail}", level="error", kind="error")
        store.finish_job(sys.argv[1], store.FAILED, error=detail)
        return 1


if __name__ == "__main__":
    sys.exit(main())
