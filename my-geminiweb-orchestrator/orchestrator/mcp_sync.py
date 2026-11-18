"""Đồng bộ cookie giữa orchestrator và MCP server C#.

Vì sao cần
----------
Hai thành phần giữ cookie ở **hai file khác nhau**:

| Thành phần            | File pool                                      |
|-----------------------|------------------------------------------------|
| Orchestrator (Python) | `<data_root>/pools/<name>.json`                |
| MCP server (C#, .NET) | `%LOCALAPPDATA%\\MyGeminiWebMcp\\accounts.json`|

Orchestrator thu hoạch lại cookie (harvest / auto-login) nhưng MCP server vẫn giữ
cookie cũ → mọi request trả *"Gemini returned no access token"*, dù trên web vẫn
đăng nhập được. Đây là nguyên nhân gốc của "session chết" giả.

Chỉ giữ **2 cookie xác thực**
-----------------------------
Thêm cookie phụ làm Google trả `UNAUTHENTICATED` — đã verify trong orchestrator:
2 cookie → AVAILABLE, 35 cookie → UNAUTHENTICATED. Xem `docs/HANDOFF.md`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Tên cookie xác thực — hai cookie duy nhất được giữ lại.
AUTH_COOKIES = ("__Secure-1PSID", "__Secure-1PSIDTS")


def mcp_home() -> Path:
    """Thư mục home của MCP server (giống `AppPaths.Resolve()` bên C#)."""
    custom = os.environ.get("GEMINIWEB_MCP_HOME")
    if custom:
        return Path(custom)
    return Path(os.environ.get("LOCALAPPDATA", "")) / "MyGeminiWebMcp"


def mcp_accounts_file() -> Path:
    """File pool của MCP server."""
    return mcp_home() / "accounts.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect_pools(pools_dir: Path) -> list[dict[str, Any]]:
    """Đọc cookie xác thực từ mọi pool của orchestrator."""
    result: list[dict[str, Any]] = []
    if not pools_dir.is_dir():
        return result

    for pool_file in sorted(pools_dir.glob("*.json")):
        if pool_file.name in ("example.json", "account_pools.json"):
            continue

        pool = _read_json(pool_file)
        if not isinstance(pool, dict):
            continue

        cookies = {
            c.get("name"): c.get("value", "")
            for c in pool.get("cookies", [])
            if isinstance(c, dict)
        }
        psid = cookies.get("__Secure-1PSID", "")
        psidts = cookies.get("__Secure-1PSIDTS", "")
        if not psid or not psidts:
            continue

        result.append(
            {
                "name": pool.get("name") or pool_file.stem,
                "email": pool.get("account_email"),
                "psid": psid,
                "psidts": psidts,
                "updated_at": pool.get("updated_at"),
            }
        )

    return result


def sync(apply: bool = False, pools_dir: Path | None = None) -> dict[str, Any]:
    """Đồng bộ cookie từ `pools/` sang `accounts.json` của MCP server.

    :param apply: ghi thay đổi xuống đĩa. Mặc định chỉ báo cáo.
    :param pools_dir: thư mục pool của orchestrator. Mặc định lấy từ `paths`.
    :returns: kết quả tổng hợp.
    """
    if pools_dir is None:
        from orchestrator import paths

        pools_dir = paths.POOLS_DIR

    pools = collect_pools(pools_dir)
    target = mcp_accounts_file()

    if not pools:
        return {"ok": False, "error": f"Không tìm thấy pool nào trong {pools_dir}"}
    if not target.is_file():
        return {"ok": False, "error": f"Không thấy {target}"}

    doc = json.loads(target.read_text(encoding="utf-8"))
    accounts = doc.get("accounts", [])

    changed: list[str] = []
    unchanged: list[str] = []
    skipped: list[str] = []

    for pool in pools:
        account = next((a for a in accounts if a.get("name") == pool["name"]), None)

        # Tên pool và tên account trong MCP pool có thể khác nhau (pool "windows"
        # chứa account "mmeo"). Email là danh tính duy nhất sống sót qua việc
        # Google cấp lại session id, nên dùng nó làm fallback.
        if account is None and pool["email"]:
            email = pool["email"].strip().lower()
            account = next(
                (
                    a
                    for a in accounts
                    if str(a.get("email", "")).strip().lower() == email
                ),
                None,
            )

        if account is None:
            skipped.append(pool["name"])
            continue

        if account.get("psid") == pool["psid"] and account.get("psidts") == pool["psidts"]:
            unchanged.append(pool["name"])
            continue

        account["psid"] = pool["psid"]
        account["psidts"] = pool["psidts"]
        if pool["email"]:
            account["email"] = pool["email"]
        # Chỉ giữ 2 cookie xác thực — cookie phụ làm session bị từ chối.
        account["cookies"] = {}
        account["lastRefreshed"] = datetime.now(timezone.utc).isoformat()
        changed.append(pool["name"])

    if changed and apply:
        target.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    return {
        "ok": True,
        "pools_dir": str(pools_dir),
        "target": str(target),
        "changed": changed,
        "unchanged": unchanged,
        "skipped": skipped,
        "applied": bool(changed and apply),
        "hint": (
            "Đã ghi. Khởi động lại MCP server để nó nạp pool mới."
            if changed and apply
            else ("Chạy lại với --apply để ghi." if changed else "Không có gì thay đổi.")
        ),
    }
