"""Tạo hồ sơ Chrome mới cho multi-account Google (song song hoá pool).

Vì sao cần:
    Mỗi tài khoản Google cần 1 hồ sơ Chrome riêng, để:
      - đăng nhập song song mà không kick nhau (Evil Clone),
      - export cookie bằng Cookie-Editor từ đúng hồ sơ đó.

Module này CHỈ tạo hồ sơ. Việc đăng nhập / xác thực / 2FA là của người dùng.

Cách hoạt động (Windows):
    1. Đọc `%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Local State`.
    2. Chọn tên hồ sơ chưa dùng (`Profile N` hoặc tên tuỳ chỉnh).
    3. Tạo thư mục hồ sơ + file `Preferences` tối thiểu.
    4. Ghi entry vào `profile.info_cache` + `profiles_order` + `profiles_created`
       trong `Local State` để Chrome hiện hồ sơ trong profile picker.
    5. Mở Chrome với `--profile-directory="<tên>"` để người dùng đăng nhập.

Yêu cầu: Chrome PHẢI đang đóng khi ghi `Local State`, nếu không Chrome sẽ
ghi đè thay đổi của ta khi thoát.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
)


def user_data_dir() -> Path:
    """Thư mục `User Data` của Chrome."""
    override = os.environ.get("CHROME_USER_DATA")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def local_state_path() -> Path:
    return user_data_dir() / "Local State"


def chrome_exe() -> Path | None:
    for c in CHROME_CANDIDATES:
        if c.is_file():
            return c
    return None


def chrome_running() -> bool:
    """Chrome đang chạy không (thật cả Chrome và các tiến trình con)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        return "chrome.exe" in (out.stdout or "")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Đọc Local State
# ---------------------------------------------------------------------------


def _read_local_state() -> dict[str, Any]:
    p = local_state_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_local_state(data: dict[str, Any]) -> None:
    p = local_state_path()
    # Ghi atomically: Chrome có thể đang đọc file này.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def list_profiles() -> list[dict[str, Any]]:
    """Liệt kê hồ sơ Chrome hiện có."""
    data = _read_local_state()
    cache = (data.get("profile") or {}).get("info_cache") or {}
    order = (data.get("profile") or {}).get("profiles_order") or list(cache.keys())
    out: list[dict[str, Any]] = []
    for dirname in order:
        info = cache.get(dirname) or {}
        out.append(
            {
                "directory": dirname,
                "name": info.get("name") or dirname,
                "user_name": info.get("user_name") or "",
                "gaia_id": info.get("gaia_id") or "",
                "is_ephemeral": bool(info.get("is_ephemeral", False)),
            }
        )
    return out


def _next_profile_dir(cache: dict[str, Any]) -> str:
    used = set(cache.keys())
    n = 1
    while f"Profile {n}" in used:
        n += 1
    return f"Profile {n}"


# ---------------------------------------------------------------------------
# Tạo hồ sơ
# ---------------------------------------------------------------------------

_MINIMAL_PREFERENCES = {
    "profile": {
        "name": "",
        "managed_user_id": "",
        "is_using_default_name": True,
    },
    "session": {},
    "browser": {"has_seeded_welcome_page": True},
    "extensions": {"settings": {}},
}


def create_profile(
    display_name: str | None = None,
    *,
    open_after: bool = True,
    allow_chrome_running: bool = False,
) -> dict[str, Any]:
    """Tạo hồ sơ Chrome mới.

    Args:
        display_name: tên hiển thị trong profile picker (mặc định "Acc N").
        open_after: mở Chrome với hồ sơ vừa tạo để người dùng đăng nhập.
        allow_chrome_running: cho phép tạo dù Chrome đang mở (không khuyến nghị).

    Returns:
        dict với `directory`, `display_name`, `path`, `launched`.
    """
    ud = user_data_dir()
    if not ud.is_dir():
        raise RuntimeError(f"Không tìm thấy Chrome user data: {ud}")

    running = chrome_running()
    if running and not allow_chrome_running:
        raise RuntimeError(
            "Chrome đang chạy. Hãy đóng Chrome hoàn toàn rồi thử lại — "
            "Chrome sẽ ghi đè 'Local State' khi thoát và xoá hồ sơ vừa tạo."
        )

    data = _read_local_state()
    profile_block = data.setdefault("profile", {})
    cache = profile_block.setdefault("info_cache", {})

    directory = _next_profile_dir(cache)
    profile_dir = ud / directory
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Tạo file Preferences tối thiểu để Chrome không bỏ qua hồ sơ.
    prefs_path = profile_dir / "Preferences"
    if not prefs_path.exists():
        prefs = json.loads(json.dumps(_MINIMAL_PREFERENCES))
        prefs["profile"]["name"] = display_name or ""
        prefs_path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

    entry = {
        "name": display_name or directory,
        "user_name": "",
        "gaia_id": "",
        "is_using_default_name": not bool(display_name),
        "is_ephemeral": False,
        "active_time": int(time.time()),
        "has_seeded_welcome_page": True,
        "metrics_bucket_index": len(cache) + 1,
    }
    cache[directory] = entry

    order = list(profile_block.get("profiles_order") or [])
    if directory not in order:
        order.append(directory)
    profile_block["profiles_order"] = order
    profile_block["profiles_created"] = int(profile_block.get("profiles_created") or 0) + 1
    profile_block["picker_shown"] = True

    _write_local_state(data)

    launched = False
    if open_after:
        exe = chrome_exe()
        if exe is None:
            raise RuntimeError(
                "Tạo hồ sơ xong nhưng không tìm thấy chrome.exe để mở. "
                "Mở Chrome thủ công và chọn hồ sơ vừa tạo."
            )
        subprocess.Popen(
            [
                str(exe),
                f"--profile-directory={directory}",
                "https://accounts.google.com/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launched = True

    return {
        "directory": directory,
        "display_name": entry["name"],
        "path": str(profile_dir),
        "launched": launched,
        "next_step": (
            "Đăng nhập tài khoản Google trong cửa sổ vừa mở, rồi export cookie "
            f"bằng Cookie-Editor (chọn profile '{directory}') thành "
            f"pools/{directory.replace(' ', '-').lower()}.json"
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="chrome_profile", description="Tạo / liệt kê hồ sơ Chrome cho multi-account."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Liệt kê hồ sơ hiện có")

    p_new = sub.add_parser("create", help="Tạo hồ sơ mới")
    p_new.add_argument("--name", default=None, help="Tên hiển thị (mặc định 'Acc N')")
    p_new.add_argument("--no-open", action="store_true", help="Không mở Chrome sau khi tạo")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        for p in list_profiles():
            user = f"  <{p['user_name']}>" if p["user_name"] else ""
            print(f"{p['directory']:<12} {p['name']}{user}")
        return 0

    if args.cmd == "create":
        try:
            result = create_profile(args.name, open_after=not args.no_open)
        except RuntimeError as e:
            print(f"LỖI: {e}")
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
