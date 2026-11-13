#!/usr/bin/env python3
"""Quét cookie Gemini trong các profile Chrome/Edge của máy local.

Mục đích: xác định profile nào đã đăng nhập gemini.google.com và cookie
`__Secure-1PSID` / `__Secure-1PSIDTS` có tồn tại hay không.

CHÚ Ý — giới hạn thực tế:
    Chrome >= 127 mã hoá cookie bằng *app-bound encryption* (prefix `v20`).
    Cookie nằm trong cột `encrypted_value`, `value` rỗng, và KHÔNG tool ngoài
    Chrome đọc được (đã thử: shutil.copyfile, CreateFileW + share flags,
    esentutl /y, SQLite mode=ro&immutable=1 -> đều fail).

    Script này chỉ BÁO CÁO trạng thái, không giải mã được gì.
    Muốn lấy cookie dùng được -> export từ trong Chrome (Cookie-Editor),
    hoặc mở Chrome với --remote-debugging-port để đọc qua CDP.

Cách dùng:
    python scripts/scan_browser_cookies.py
    python scripts/scan_browser_cookies.py --verbose
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

# --- Đường dẫn cài đặt (ưu tiên Program Files, fallback LOCALAPPDATA) ---

BROWSER_ROOTS = {
    "Chrome": [
        r"C:\Program Files\Google\Chrome\Application\User Data",
        r"C:\Program Files (x86)\Google\Chrome\Application\User Data",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    ],
    "Edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\User Data",
        r"C:\Program Files\Microsoft\Edge\Application\User Data",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ],
}

GEMINI_COOKIE_HOSTS = ("%gemini%", "%bard%")
AUTH_COOKIE_NAMES = (
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
)


# --- Tiện ích ---

def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if os.path.isdir(p):
            return p
    return None


def _copy_db(src: str, tag: str) -> str | None:
    """Copy một SQLite DB (có thể đang bị lock) sang thư mục tạm.

    Trả về path bản copy, hoặc None nếu không copy được.
    """
    dest = os.path.join(tempfile.gettempdir(), f"scan_{tag}.db")
    try:
        shutil.copyfile(src, dest)
        return dest
    except PermissionError:
        pass
    except OSError:
        return None

    # Thử robocopy /B (backup mode) — đôi khi bypass được lock của Chromium
    netdir = os.path.dirname(src)
    tmpdir = tempfile.gettempdir()
    os.system(
        f'robocopy "{netdir}" "{tmpdir}" Cookies /B /R:0 /W:0 '
        f"/NFL /NDL /NJH /NJS /NP >nul 2>&1"
    )
    got = os.path.join(tmpdir, "Cookies")
    if os.path.exists(got):
        try:
            shutil.copyfile(got, dest)
        except OSError:
            return None
        finally:
            try:
                os.remove(got)
            except OSError:
                pass
        return dest
    return None


def _query(path: str, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        con = sqlite3.connect(path)
        con.text_factory = bytes
        rows = con.execute(sql, args).fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return []


def _text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


# --- Trình quét ---

def scan_profile(browser: str, profile_dir: str, root: str, verbose: bool = False) -> dict:
    label = f"{browser}/{profile_dir}"
    info: dict = {
        "label": label,
        "profile_dir": profile_dir,
        "root": root,
        "gemini_cookies": [],
        "auth_cookies": [],
        "account_email": None,
        "gaia_id": None,
        "profile_name": None,
        "locked": False,
    }

    # 1. Thông tin tài khoản từ Preferences
    pref = os.path.join(root, profile_dir, "Preferences")
    if os.path.exists(pref):
        try:
            with open(pref, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
            prof = data.get("profile", {}) or {}
            info["profile_name"] = prof.get("name")
            # Email/gaia nằm ở nhiều nơi (account_info, signin, managed...) ->
            # quét toàn bộ file text, lấy match đầu tiên.
            blob = re.sub(r"\s+", "", json.dumps(data, separators=(",", ":")))
            m = re.search(r'"email":"([^"]+@[^"]+)"', blob)
            if m:
                info["account_email"] = m.group(1)
            m = re.search(r'"(?:gaia|obfuscated_gaia_id)":"([^"]+)"', blob)
            if m:
                info["gaia_id"] = m.group(1)
        except (OSError, ValueError):
            pass

    # 2. Cookie DB
    cookie_db = os.path.join(root, profile_dir, "Network", "Cookies")
    if not os.path.exists(cookie_db):
        return info

    copied = _copy_db(cookie_db, f"{browser}_{profile_dir}".replace(" ", "_"))
    if not copied:
        info["locked"] = True
        return info

    try:
        gem = _query(
            copied,
            "select host_key, name, length(value), length(encrypted_value), "
            "hex(substr(encrypted_value,1,3)) from cookies "
            "where host_key like '%gemini%' or host_key like '%bard%'",
        )
        for host, name, plain_len, enc_len, prefix in gem:
            info["gemini_cookies"].append({
                "host": _text(host),
                "name": _text(name),
                "plain_len": plain_len,
                "encrypted_len": enc_len,
                "prefix": _text(prefix),
                "decryptable": bool(plain_len),
            })

        placeholders = ",".join("?" * len(AUTH_COOKIE_NAMES))
        auth = _query(
            copied,
            f"select name, length(value), length(encrypted_value), "
            f"hex(substr(encrypted_value,1,3)) from cookies "
            f"where host_key='.google.com' and name in ({placeholders})",
            AUTH_COOKIE_NAMES,
        )
        for name, plain_len, enc_len, prefix in auth:
            info["auth_cookies"].append({
                "name": _text(name),
                "plain_len": plain_len,
                "encrypted_len": enc_len,
                "prefix": _text(prefix),
                "decryptable": bool(plain_len),
            })
    finally:
        try:
            os.remove(copied)
        except OSError:
            pass

    return info


def scan_all(verbose: bool = False) -> list[dict]:
    results: list[dict] = []
    for browser, candidates in BROWSER_ROOTS.items():
        root = _first_existing(candidates)
        if not root:
            results.append({
                "label": f"{browser}/<không cài>",
                "gemini_cookies": [],
                "auth_cookies": [],
                "account_email": None,
                "locked": False,
                "profile_name": None,
            })
            continue

        profiles = sorted(
            d for d in glob.glob(os.path.join(root, "*"))
            if os.path.isdir(d) and os.path.isdir(os.path.join(d, "Network"))
        )
        for pdir in profiles:
            name = os.path.basename(pdir)
            results.append(scan_profile(browser, name, root, verbose=verbose))
    return results


def render(results: list[dict]) -> None:
    print("=" * 78)
    print("  GEMINI COOKIE SCAN")
    print("=" * 78)
    for r in results:
        label = r.get("label", "?")
        print(f"\n--- {label}")
        if r.get("profile_name"):
            print(f"    profile.name : {r['profile_name']}")
        if r.get("account_email"):
            print(f"    account      : {r['account_email']}")
        if r.get("gaia_id"):
            print(f"    gaia_id      : {r['gaia_id']}")

        if r.get("locked"):
            print("    [!] Cookie DB bị LOCK — hãy đóng browser rồi chạy lại")
            continue

        gems = r.get("gemini_cookies", [])
        if gems:
            print(f"    Gemini cookies: {len(gems)}")
            for g in gems:
                state = "PLAINTEXT" if g["decryptable"] else f"ENCRYPTED({g['prefix']})"
                print(f"      {g['host']:24} {g['name']:20} {state}")
        else:
            print("    Gemini cookies: 0")

        auths = r.get("auth_cookies", [])
        if auths:
            print(f"    .google.com auth cookies: {len(auths)}")
            for a in auths:
                state = "PLAINTEXT" if a["decryptable"] else f"ENCRYPTED({a['prefix']})"
                print(f"      {a['name']:22} plain={a['plain_len']:3} "
                      f"enc={a['encrypted_len']:4}  {state}")
        else:
            print("    .google.com auth cookies: 0")

    print("\n" + "=" * 78)
    print("  KẾT LUẬN")
    print("=" * 78)
    ready = [r for r in results
             if any(a["decryptable"] for a in r.get("auth_cookies", []))]
    gem = [r for r in results if r.get("gemini_cookies")]
    if ready:
        print("  [!] Coi profile nào có cookie PLAINTEXT — dùng được trực tiếp.")
    else:
        print("  [i] Mọi cookie đều ENCRYPTED (v20 app-bound).")
        print("      -> Phải export từ BÊN TRONG Chrome (Cookie-Editor),")
        print("         hoặc mở Chrome với --remote-debugging-port để đọc qua CDP.")
    if gem:
        print(f"  [i] {len(gem)} profile đã từng truy cập gemini.google.com.")
    else:
        print("  [!] Chưa profile nào có cookie Gemini — cần đăng nhập gemini.google.com.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="xuất kết quả dạng JSON")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    results = scan_all(verbose=args.verbose)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        render(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
