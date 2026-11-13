#!/usr/bin/env python3
"""Entry point cho phiên Cursor mới — chạy khi user nhắn "continue".

Đọc `docs/HANDOFF.md`, in ra trạng thái hiện tại, và kiểm tra các
prerequisite để biết có thể đi tiếp bước nào.

Cách dùng:
    python scripts/continue.py
    python scripts/continue.py --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDOFF = REPO_ROOT / "docs" / "HANDOFF.md"
POOLS_DIR = REPO_ROOT / "pools"
REQUIRED = "__Secure-1PSID"
OPTIONAL = "__Secure-1PSIDTS"


def _load_pool(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = raw.get("cookies", raw) if isinstance(raw, dict) else raw
    cookies: dict[str, str] = {}
    if isinstance(items, dict):
        cookies = {k: v for k, v in items.items() if isinstance(v, str)}
    elif isinstance(items, list):
        cookies = {i["name"]: i["value"] for i in items
                   if isinstance(i, dict) and i.get("name") and isinstance(i.get("value"), str)}
    name = raw.get("name") if isinstance(raw, dict) else None
    if not name:
        name = path.stem
    return {
        "name": name,
        "account_email": raw.get("account_email") if isinstance(raw, dict) else None,
        "has_1psid": bool(cookies.get(REQUIRED)),
        "has_1psidts": bool(cookies.get(OPTIONAL)),
    }


def check_env() -> dict:
    """Kiểm tra dependency quan trọng."""
    out: dict = {"python": sys.version.split()[0], "gemini_webapi": False, "mcp": False}
    try:
        import gemini_webapi  # noqa: F401
        out["gemini_webapi"] = True
        out["gemini_webapi_version"] = getattr(gemini_webapi, "__version__", "unknown")
    except ImportError:
        pass
    try:
        import mcp  # noqa: F401
        out["mcp"] = True
    except ImportError:
        pass
    return out


def check_pools() -> list[dict]:
    if not POOLS_DIR.is_dir():
        return []
    pools = []
    for f in sorted(POOLS_DIR.glob("*.json")):
        if f.name == "example.json":
            continue
        p = _load_pool(f)
        if p:
            p["file"] = f.name
            pools.append(p)
    return pools


def check_gemini_mcp() -> bool:
    p = Path.home() / ".cursor" / "bin" / "gemini_mcp.py"
    return p.exists()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    handoff_exists = HANDOFF.exists()
    env = check_env()
    pools = check_pools()
    state = {
        "repo": str(REPO_ROOT),
        "handoff_exists": handoff_exists,
        "env": env,
        "pools": pools,
        "gemini_mcp_present": check_gemini_mcp(),
    }

    ready = [p for p in pools if p["has_1psid"]]

    if args.json:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0

    print("=" * 74)
    print("  my-gemini-pro — CONTINUE CONTEXT")
    print("=" * 74)
    print(f"  repo            : {REPO_ROOT}")
    print(f"  HANDOFF.md      : {'✓' if handoff_exists else '✗ THIẾU'}")
    print(f"  gemini_webapi   : {'✓ ' + env.get('gemini_webapi_version', '') if env['gemini_webapi'] else '✗ chưa cài'}")
    print(f"  mcp (sdk)       : {'✓' if env['mcp'] else '✗'}")
    print(f"  gemini_mcp.py   : {'✓' if state['gemini_mcp_present'] else '✗'}")

    print(f"\n  POOLS ({len(pools)}):")
    if not pools:
        print("    (chưa có — cần export cookie, xem docs/COOKIE-SETUP.md)")
    for p in pools:
        mark = "✓" if p["has_1psid"] else "✗"
        ts = "✓" if p["has_1psidts"] else "-"
        print(f"    {mark} {p['name']:10} {p['account_email'] or '?':28} 1PSID={mark} 1PSIDTS={ts}")

    print("\n" + "=" * 74)
    if ready:
        print("  → READY: có pool hợp lệ. Bước tiếp theo:")
        print("    1. `python scripts/setup_pool.py probe` để test thật")
        print("    2. Viết tools/gemini_pool.py + tools/gemini_dr_mcp.py")
        print("  Đọc docs/HANDOFF.md §6 (Kế hoạch triển khai) để biết chi tiết.")
    else:
        print("  → BLOCKED: chưa có cookie hợp lệ.")
        print("    Bước tiếp theo (CẦN USER):")
        print("      1. Mở Chrome, vào tab gemini.google.com (đã đăng nhập)")
        print("      2. Cookie-Editor → Export as JSON")
        print("      3. `python scripts/setup_pool.py setup --export <file> --name windows --account <email>`")
        print("    Chi tiết: docs/COOKIE-SETUP.md")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
