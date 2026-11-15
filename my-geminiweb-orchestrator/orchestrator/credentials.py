"""Kho credential cho tài khoản Gemini — mã hoá bằng DPAPI của Windows.

Vì sao cần module này
---------------------
Tài khoản loại 2 (chỉ email + password, không 2FA) cần lưu **mật khẩu** để
orchestrator tự đăng nhập lại khi session chết. Mật khẩu là secret — không
được nằm plaintext trong `pools/*.json` (file đó đã gitignore nhưng vẫn nên
mã hoá).

Cơ chế
-------
Dùng **Windows DPAPI** (`CryptProtectData`) với `CRYPTPROTECT_LOCAL_MACHINE`?
→ **Không**. Ta dùng DPAPI **user-scope** (mặc định): secret chỉ giải mã được
bởi cùng user Windows + cùng máy. Đây đúng là thứ ta cần — và nó là API
native của Windows nên **không thêm dependency nào**.

DPAPI cũng tự cung cấp **integrity**: ciphertext bị sửa sẽ fail decrypt, nên
tấn công offline vào file cũng vô nghĩa (đổi được ciphertext → không giải mã
được → không lấy được mật khẩu).

File lưu
--------
Một file duy nhất: `<state_dir>/credentials.json`

```json
{
  "version": 1,
  "accounts": {
    "t1vodich": {
      "email": "user@example.com",
      "pool": "acc3",
      "auth": "oauth",          // hoặc "password"
      "pw": "<base64 DPAPI blob>",
      "created_at": "2026-11-14T08:30:00"
    }
  }
}
```

Ràng buộc an toàn
-----------------
- Không bao giờ log mật khẩu.
- Không bao giờ trả mật khẩu ra JSON response.
- File được set ACL chỉ cho user hiện tại (best-effort trên Windows).
- `GEMINI_STATE_DIR` / `ORCH_STATE_DIR` quyết định vị trí.

Ví dụ dùng
----------
```python
from orchestrator import credentials

credentials.set_password("t1vodich", "user@example.com", "hunter2", pool="acc3")
pwd = credentials.get_password("t1vodich")   # -> str | None
credentials.list_accounts()                  # -> [{account, email, pool, auth, has_password}]
credentials.delete_account("t1vodich")
```
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

#: File lưu credential (đã mã hoá).
CREDS_FILENAME = "credentials.json"

#: Phiên bản format.
FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# DPAPI
# ---------------------------------------------------------------------------

class _DataBlob(ctypes.Structure):
    """`DATA_BLOB` của Win32 — buffer động dài."""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    """Tạo `DATA_BLOB` từ bytes (giữ tham chiếu buffer cho tới khi dùng xong)."""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _dpapi_protect(data: bytes, *, entropy: bytes | None = None) -> bytes:
    """Mã hoá bytes bằng DPAPI (user-scope).

    Raises:
        OSError: nếu Windows từ chối (ví dụ chạy trong container không có user profile).
    """
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob, in_buf = _blob(data)
    entropy_blob = None
    entropy_buf = None
    if entropy:
        entropy_blob, entropy_buf = _blob(entropy)

    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,  # szDataDescr
        ctypes.byref(entropy_blob) if entropy_blob else None,
        None,  # pOptionalEntropy (reserved)
        None,  # pvReserved
        0,  # dwFlags -> user scope, không máy
        ctypes.byref(out_blob),
    )
    _ = (in_buf, entropy_buf)  # giữ tham chiếu tới khi hết dùng
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"DPAPI protect thất bại (WinError {err})")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes, *, entropy: bytes | None = None) -> bytes:
    """Giải mã blob DPAPI. Raises OSError nếu sai user/máy hoặc bị sửa."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob, in_buf = _blob(data)
    entropy_blob = None
    entropy_buf = None
    if entropy:
        entropy_blob, entropy_buf = _blob(entropy)

    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob) if entropy_blob else None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    _ = (in_buf, entropy_buf)
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"DPAPI unprotect thất bại (WinError {err})")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ---------------------------------------------------------------------------
# Vị trí file
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    """Thư mục state của orchestrator (nơi job DB cũng nằm)."""
    for env in ("ORCH_STATE_DIR", "GEMINI_STATE_DIR"):
        val = os.environ.get(env)
        if val:
            return Path(val)
    return Path.home() / ".cursor-agents" / "orchestrator"


def creds_path() -> Path:
    return state_dir() / CREDS_FILENAME


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _load() -> dict[str, Any]:
    p = creds_path()
    if not p.is_file():
        return {"version": FORMAT_VERSION, "accounts": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": FORMAT_VERSION, "accounts": {}}
    if not isinstance(raw, dict):
        return {"version": FORMAT_VERSION, "accounts": {}}
    raw.setdefault("version", FORMAT_VERSION)
    raw.setdefault("accounts", {})
    return raw


def _save(data: dict[str, Any]) -> None:
    p = creds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    _restrict_acl(p)


def _restrict_acl(path: Path) -> None:
    """Chỉ cho user hiện tại đọc/ghi (best-effort, chỉ trên Windows)."""
    if os.name != "nt":
        return
    try:
        import subprocess

        # `icacls` với inherit rỗng rồi chỉ grant cho user hiện tại.
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        if not user:
            return
        principal = f"{domain}\\{user}" if domain else user
        subprocess.run(
            [
                "icacls", str(path),
                "/inheritance:r",
                "/grant:r", f"{principal}:(F)",
            ],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass  # best-effort — không được thì bỏ qua


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def set_password(
    account: str,
    email: str,
    password: str,
    *,
    pool: str | None = None,
    auth: str = "password",
) -> dict[str, Any]:
    """Lưu mật khẩu (mã hoá DPAPI) cho 1 account."""
    key = _key(account)
    data = _load()
    entry = data["accounts"].get(key) or {}
    entry.update(
        {
            "email": email,
            "pool": pool or entry.get("pool"),
            "auth": auth,
            "created_at": entry.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    entry["pw"] = base64.b64encode(
        _dpapi_protect(password.encode("utf-8"), entropy=_entropy(key))
    ).decode("ascii")
    data["accounts"][key] = entry
    _save(data)
    return {
        "ok": True,
        "account": key,
        "email": email,
        "pool": entry.get("pool"),
        "auth": auth,
    }


def get_password(account: str) -> str | None:
    """Lấy mật khẩu đã giải mã. `None` nếu chưa lưu hoặc giải mã fail."""
    key = _key(account)
    data = _load()
    entry = data["accounts"].get(key)
    if not entry or not entry.get("pw"):
        return None
    try:
        blob = base64.b64decode(entry["pw"])
        return _dpapi_unprotect(blob, entropy=_entropy(key)).decode("utf-8")
    except (OSError, ValueError):
        return None


def get_email(account: str) -> str | None:
    """Email của account (không cần giải mã)."""
    key = _key(account)
    data = _load()
    entry = data["accounts"].get(key)
    return entry.get("email") if entry else None


def get_account(account: str) -> dict[str, Any] | None:
    """Thông tin account (không bao giờ chứa mật khẩu plaintext)."""
    key = _key(account)
    data = _load()
    entry = data["accounts"].get(key)
    if not entry:
        return None
    return {
        "account": key,
        "email": entry.get("email"),
        "pool": entry.get("pool"),
        "auth": entry.get("auth"),
        "has_password": bool(entry.get("pw")),
        "created_at": entry.get("created_at"),
    }


def list_accounts() -> list[dict[str, Any]]:
    """Danh sách account đã lưu (không có mật khẩu)."""
    data = _load()
    out: list[dict[str, Any]] = []
    for key, entry in (data.get("accounts") or {}).items():
        out.append(
            {
                "account": key,
                "email": entry.get("email"),
                "pool": entry.get("pool"),
                "auth": entry.get("auth"),
                "has_password": bool(entry.get("pw")),
                "created_at": entry.get("created_at"),
            }
        )
    return out


def delete_account(account: str) -> bool:
    """Xoá credential của 1 account. `True` nếu có thật sự xoá."""
    key = _key(account)
    data = _load()
    if key not in (data.get("accounts") or {}):
        return False
    del data["accounts"][key]
    _save(data)
    return True


def _key(account: str) -> str:
    """Chuẩn hoá tên account (khớp với `cookie_sync.account_dir`)."""
    safe = "".join(ch for ch in account.lower() if ch.isalnum() or ch in "._-")
    return safe or "default"


def _entropy(key: str) -> bytes:
    """Entropy bổ sung cho DPAPI — ràng buộc ciphertext vào đúng account."""
    return f"orchestrator-gemini:{key}".encode("utf-8")
