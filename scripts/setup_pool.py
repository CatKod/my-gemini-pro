#!/usr/bin/env python3
"""Tạo và kiểm tra pool cookie cho Gemini Deep Research.

Pool = 1 tài khoản Google + cookie phiên của nó, để `gemini_webapi` dùng
để gọi Deep Research trên gemini.google.com (trừ vào gói AI Pro đã trả).

Luồng:
    1. User export cookie từ Chrome bằng Cookie-Editor -> pools/<tên>.json
       (xem docs/COOKIE-SETUP.md)
    2. `python scripts/setup_pool.py --validate`  -> kiểm tra cookie đủ dùng
    3. `python scripts/setup_pool.py --probe`    -> gọi thật 1 deep research

File pool (pools/*.json):
    {
      "name": "windows",
      "account_email": "mmeo44244@gmail.com",
      "browser": "chrome",
      "profile": "Default",
      "cookies": [
        {"name": "__Secure-1PSID", "value": "...", "domain": ".google.com"},
        {"name": "__Secure-1PSIDTS", "value": "...", "domain": ".google.com"}
      ]
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POOLS_DIR = REPO_ROOT / "pools"
REPORTS_DIR = REPO_ROOT / "reports"

REQUIRED = "__Secure-1PSID"
OPTIONAL = "__Secure-1PSIDTS"

# Map tên pool -> tên file cookie mặc định.
# Tên pool khớp với pool Antigravity trong gemini_mcp.py để dễ nhớ.
DEFAULT_POOL_FILES = {
    "windows": "chrome-default.json",
    "wsl": "chrome-profile1.json",
}

# Tài khoản kỳ vọng — dùng để cảnh báo nhầm pool.
EXPECTED_ACCOUNT = {
    "windows": "mmeo44244@gmail.com",
    "wsl": "catkingmv@gmail.com",
}


# --- Load / validate ---

def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_cookies(raw) -> dict[str, str]:
    """Chấp nhận 3 shape:
       - dict {"cookies": [...]}              (format pool file của ta)
       - list  [...]                          (Cookie-Editor export thẳng)
       - dict {"cookies": {name: value}}      (một số biến thể)
    """
    items = raw
    if isinstance(raw, dict):
        items = raw.get("cookies", raw)
    out: dict[str, str] = {}
    if isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, str):
                out[k] = v
        return out
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("name") and isinstance(it.get("value"), str):
                out[it["name"]] = it["value"]
    return out


def load_pool(path: Path) -> dict:
    raw = _read_json(path)
    cookies = _extract_cookies(raw)

    name = raw.get("name") if isinstance(raw, dict) else None
    email = raw.get("account_email") if isinstance(raw, dict) else None
    if not name:
        # suy ra tên pool từ tên file
        name = path.stem.replace("chrome-", "").replace("edge-", "")
        if name == "default":
            name = "windows"
        elif name in ("profile1", "profile-1"):
            name = "wsl"

    return {
        "name": name,
        "account_email": email,
        "browser": raw.get("browser") if isinstance(raw, dict) else None,
        "profile": raw.get("profile") if isinstance(raw, dict) else None,
        "cookies": cookies,
        "source_file": str(path),
    }


def list_pool_files() -> list[Path]:
    if not POOLS_DIR.is_dir():
        return []
    return sorted(p for p in POOLS_DIR.glob("*.json") if p.name != "example.json")


def validate() -> int:
    files = list_pool_files()
    if not files:
        print("Chưa có file pool nào.")
        print(f"Đặt cookie export vào: {POOLS_DIR}")
        print("Xem docs/COOKIE-SETUP.md để biết cách export.")
        return 1

    print("=" * 74)
    print("  POOL VALIDATION")
    print("=" * 74)
    ok_all = True
    for f in files:
        try:
            pool = load_pool(f)
        except (OSError, ValueError) as e:
            print(f"\n--- {f.name}: LỖI ĐỌC FILE: {e}")
            ok_all = False
            continue

        cookies = pool["cookies"]
        has_req = bool(cookies.get(REQUIRED))
        has_opt = bool(cookies.get(OPTIONAL))
        expected = EXPECTED_ACCOUNT.get(pool["name"])

        print(f"\n--- pool '{pool['name']}'  ({f.name})")
        print(f"    account : {pool['account_email'] or '(không khai báo)'}")
        if expected and pool["account_email"] and pool["account_email"] != expected:
            print(f"    [!] Cảnh báo: kỳ vọng {expected} cho pool này")
        print(f"    {REQUIRED:22} {'✓' if has_req else '✗ THIẾU'}")
        print(f"    {OPTIONAL:22} {'✓' if has_opt else '✗ thiếu (có thể chạy, nhưng kém ổn định)'}")

        if has_req:
            v = cookies[REQUIRED]
            print(f"    {REQUIRED} length      : {len(v)}")
        else:
            ok_all = False

        # Cảnh báo cookie trông như placeholder
        if has_req and len(cookies[REQUIRED]) < 20:
            print("    [!] Giá trị 1PSID quá ngắn — có thể là placeholder, hãy export lại")
            ok_all = False

    print("\n" + "=" * 74)
    if ok_all:
        print("  Tất cả pool OK. Chạy `--probe` để test thật.")
    else:
        print("  Có pool chưa sẵn sàng. Xem docs/COOKIE-SETUP.md.")
    print("=" * 74)
    return 0 if ok_all else 1


# --- Probe thật ---

async def _probe(pool: dict, query: str, timeout: int) -> dict:
    from gemini_webapi import GeminiClient

    client = GeminiClient(
        secure_1psid=pool["cookies"][REQUIRED],
        secure_1psidts=pool["cookies"].get(OPTIONAL),
        verbose=False,
    )
    try:
        # Probe eligibility trước (nhẹ hơn nhiều so với chạy research thật)
        status = await client.inspect_account_status()
        summary = status.get("summary", {})
        dr_ok = summary.get("deep_research_feature_present", False)
        print(f"    deep_research_feature_present : {dr_ok}")
        if not dr_ok:
            print(f"    rejected probes : {summary.get('rejected_probes')}")

        result = await client.deep_research(query, timeout=timeout)
        doc = None
        if result.final_output is not None:
            doc = result.final_output.deep_research_document
        return {
            "done": result.done,
            "title": getattr(doc, "title", None),
            "content": getattr(doc, "content", None),
            "sources": getattr(doc, "sources", None),
            "markdown": getattr(doc, "markdown", None),
            "dr_available": dr_ok,
        }
    finally:
        try:
            await client.close()
        except Exception:
            pass


def probe(query: str, timeout: int) -> int:
    files = list_pool_files()
    if not files:
        print("Chưa có file pool nào — xem docs/COOKIE-SETUP.md")
        return 1

    REPORTS_DIR.mkdir(exist_ok=True)
    all_ok = True
    for f in files:
        pool = load_pool(f)
        if REQUIRED not in pool["cookies"]:
            continue
        print(f"\n{'=' * 74}")
        print(f"  PROBE pool '{pool['name']}' ({pool['account_email'] or '?'})")
        print(f"{'=' * 74}")
        try:
            out = asyncio.run(_probe(pool, query, timeout))
        except Exception as e:
            print(f"    LỖI: {type(e).__name__}: {e}")
            all_ok = False
            continue

        print(f"    done    : {out['done']}")
        print(f"    title   : {out['title']}")
        content = out["content"] or ""
        print(f"    report  : {len(content)} ký tự")
        if out["sources"]:
            print(f"    sources : {len(out['sources'])}")

        if content:
            import re
            import datetime
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", (out["title"] or "report").lower()).strip("-")[:50]
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = REPORTS_DIR / f"{stamp}-{slug}.md"
            path.write_text(out["markdown"] or content, encoding="utf-8")
            print(f"    saved   : {path}")
        else:
            print("    [!] Không có nội dung report")
            all_ok = False

    print(f"\n{'=' * 74}")
    print("  PROBE HOÀN TẤT — mọi pool OK" if all_ok else "  PROBE CÓ LỖI")
    print("=" * 74)
    return 0 if all_ok else 1


# --- Setup từ Cookie-Editor export ---

def setup(name: str | None, export: Path, account: str | None, browser: str, profile: str) -> int:
    raw = _read_json(export)
    cookies = _extract_cookies(raw)

    if REQUIRED not in cookies:
        print(f"[!] Export không chứa {REQUIRED}. Hãy export từ tab gemini.google.com đã đăng nhập.")
        print(f"    Các cookie tìm thấy: {sorted(cookies)[:12]}")
        return 1

    pool_name = name or export.stem.replace("chrome-", "").replace("edge-", "")
    if pool_name == "default":
        pool_name = "windows"
    elif pool_name in ("profile1", "profile-1"):
        pool_name = "wsl"

    POOLS_DIR.mkdir(exist_ok=True)
    out_path = POOLS_DIR / f"{pool_name}.json"
    payload = {
        "name": pool_name,
        "account_email": account,
        "browser": browser,
        "profile": profile,
        "cookies": [
            {"name": n, "value": cookies[n], "domain": ".google.com"}
            for n in (REQUIRED, OPTIONAL) if n in cookies
        ],
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[✓] Đã tạo pool '{pool_name}' -> {out_path}")
    print(f"    {REQUIRED}: ✓")
    print(f"    {OPTIONAL}: {'✓' if OPTIONAL in cookies else '✗'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("validate", help="kiểm tra các pool đã tạo")

    p_probe = sub.add_parser("probe", help="gọi deep research thật để test")
    p_probe.add_argument("--query", default="Tóm tắt 3 điểm khác biệt chính của Rust so với Go về quản lý bộ nhớ.",
                        help="câu hỏi test (nên ngắn để chạy nhanh)")
    p_probe.add_argument("--timeout", type=int, default=600)

    p_setup = sub.add_parser("setup", help="tạo pool từ file Cookie-Editor export")
    p_setup.add_argument("--export", required=True, type=Path, help="đường dẫn file JSON export")
    p_setup.add_argument("--name", default=None, help="tên pool (windows | wsl)")
    p_setup.add_argument("--account", default=None, help="email tài khoản")
    p_setup.add_argument("--browser", default="chrome")
    p_setup.add_argument("--profile", default="")

    args = ap.parse_args()

    if args.cmd == "setup":
        return setup(args.name, args.export, args.account, args.browser, args.profile)
    if args.cmd == "probe":
        return probe(args.query, args.timeout)
    # default: validate
    return validate()


if __name__ == "__main__":
    sys.exit(main())
