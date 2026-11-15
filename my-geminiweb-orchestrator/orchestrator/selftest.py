"""Smoke test MCP server orchestrator: liệt kê tools + gọi thử tool.

Chạy:
    python -m orchestrator.selftest
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import orch_mcp  # noqa: E402


async def main() -> int:
    mcp = orch_mcp.mcp

    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    print(f"TOOLS ({len(names)}): {names}")

    expected = {
        "orch_submit",
        "orch_status",
        "orch_result",
        "orch_list",
        "orch_cancel",
        "orch_watch",
        "orch_chrome_profile",
        "orch_cookie_sync",
    }
    missing = expected - set(names)
    if missing:
        print(f"FAIL: thiếu tools {missing}")
        return 1

    # orch_list
    res = await mcp.call_tool("orch_list", {"state": None, "limit": 5})
    payload = _extract(res)
    print(f"orch_list -> ok={payload.get('status')} n_jobs={len(payload.get('jobs') or [])}")

    # orch_chrome_profile list
    res = await mcp.call_tool("orch_chrome_profile", {"action": "list"})
    payload = _extract(res)
    profiles = payload.get("profiles") or []
    print(f"orch_chrome_profile list -> {len(profiles)} profile(s)")
    for p in profiles:
        print(f"   {p['directory']}: {p['name']} <{p['user_name']}>")

    # orch_cookie_sync status (không mở Chrome → nhanh, an toàn)
    res = await mcp.call_tool("orch_cookie_sync", {"action": "status", "account": "mmeo"})
    payload = _extract(res)
    print(
        f"orch_cookie_sync status -> {payload.get('status')} "
        f"profile={payload.get('profile_exists')} cookies={payload.get('cookies_in_db')}"
    )

    # orch_submit (chat) + orch_result
    res = await mcp.call_tool(
        "orch_submit",
        {
            "kind": "chat",
            "prompt": "Reply with exactly one word: SELFTEST",
            "pool": "windows",
            "title": "selftest-mcp",
        },
    )
    payload = _extract(res)
    job_id = payload.get("job_id")
    print(f"orch_submit -> {payload.get('status')} job_id={job_id}")
    if not job_id:
        print(f"FAIL: {payload}")
        return 1

    # Chờ job xong (bounded) rồi đọc kết quả.
    for _ in range(40):
        res = await mcp.call_tool("orch_result", {"job_id": job_id})
        payload = _extract(res)
        if payload.get("status") != "PENDING":
            break
        await asyncio.sleep(2)

    print(f"orch_result -> state={payload.get('state')}")
    result = payload.get("result") or {}
    print(f"   text={result.get('text')!r}")
    print(f"   citations={len(result.get('citations') or [])}")

    ok = payload.get("state") == "DONE" and "SELFTEST" in (result.get("text") or "").upper()
    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return 0 if ok else 1


def _extract(res: Any) -> dict:
    """MCP tool call trả về `CallToolResult` (structured_content) hoặc list[TextContent]."""
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, dict) and structured:
        return structured
    if isinstance(res, dict):
        if isinstance(res.get("structured_content"), dict):
            return res["structured_content"]
        return res
    if isinstance(res, (list, tuple)):
        for item in res:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}
    return {"raw": str(res)}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
