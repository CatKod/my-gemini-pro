"""Registry tài khoản Gemini — nguồn sự thật duy nhất.

Vì sao làm lại
--------------
Cơ chế cũ có 3 nguồn trạng thái rời rạc, lệch nhau:

1. `pools/<pool>.json` — cookie + email + `profile`
2. `orchestrator/cookie_sync.py::ACCOUNT_POOLS` — mapping account → pool (vô
   hình, mất khi restart)
3. `orchestrator/credentials.py` — mật khẩu (chưa tồn tại)

Thêm account phải làm 3 thao tác rời rạc, dễ sót → "thêm tài khoản và cơ
chế cookie hiện tại đang không được ổn".

Thiết kế mới
------------
**Một file duy nhất** làm sự thật: `accounts.json` (đã mã hoá phần mật khẩu
bằng DPAPI).

```json
{
  "version": 1,
  "accounts": {
    "mmeo": {
      "email": "mmeo44244@gmail.com",
      "pool": "windows",
      "auth": "oauth",              // "oauth" | "password"
      "password": "<base64 DPAPI>",
      "created_at": "...",
      "last_ok_at": "...",          // lần cuối session còn sống
      "last_error": "..."
    }
  }
}
```

Pool cookie vẫn nằm ở `pools/<pool>.json` (vì `gemini_webapi` đọc ở đó và
`tools/gemini_pool.py` load từ đó) — nhưng giờ **account registry là nguồn
sự thật về *tài khoản***, pool file chỉ là *cache cookie*.

Luồng
-----
```
add_account(email, password=None)      -> tạo entry + pool rỗng
adopt_from_export(file)                -> cấy cookie từ Cookie-Editor
auto_login(account)                    -> đăng nhập bằng email/password (type 2)
sync(account)                          -> lấy cookie live -> pool file
status()                               -> trạng thái từng account
```

Cả 2 loại tài khoản dùng chung 1 entry:
- **Loại 1 (có xác thực / 2FA)**: `auth="oauth"`, không có mật khẩu → user
  đăng nhập tay 1 lần trong Chrome riêng, sau đó `sync` tự động.
- **Loại 2 (chỉ email + password)**: `auth="password"`, có mật khẩu (DPAPI) →
  orchestrator tự điền form đăng nhập.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from orchestrator import credentials
from orchestrator import paths

FORMAT_VERSION = 1

#: File registry.
REGISTRY_FILENAME = "accounts.json"

#: Thư mục gốc data + các đường dẫn con.
#:
#: TRƯỚC ĐÂY module này tự dò data root (`_data_root()`), còn `cookie_sync`
#: dùng cách khác → split-brain khi chạy `.exe` (GUI ghi vào
#: `%LOCALAPPDATA%\\Orchestrator\\pools\\`, dashboard đọc `<repo>\\pools\\`).
#: GIỜ: dùng chung `orchestrator.paths` cho mọi module.
DATA_ROOT = paths.DATA_ROOT
POOLS_DIR = paths.POOLS_DIR
REGISTRY_PATH = paths.ACCOUNTS_FILE

#: Mapping account → pool cho 2 tài khoản Antigravity có sẵn.
DEFAULT_POOLS = {
    "mmeo": "windows",
    "catkingmv": "wsl",
}


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _read() -> dict[str, Any]:
    if not REGISTRY_PATH.is_file():
        return {"version": FORMAT_VERSION, "accounts": {}}
    try:
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": FORMAT_VERSION, "accounts": {}}
    if not isinstance(raw, dict):
        return {"version": FORMAT_VERSION, "accounts": {}}
    raw.setdefault("version", FORMAT_VERSION)
    raw.setdefault("accounts", {})
    return raw


def _write(data: dict[str, Any]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, REGISTRY_PATH)


def _key(account: str) -> str:
    safe = "".join(ch for ch in account.lower() if ch.isalnum() or ch in "._-")
    return safe or "default"


def _default_pool(account: str) -> str:
    return DEFAULT_POOLS.get(_key(account), account)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def add_account(
    email: str,
    *,
    account: str | None = None,
    pool: str | None = None,
    password: str | None = None,
    auth: str | None = None,
) -> dict[str, Any]:
    """Thêm (hoặc cập nhật) 1 tài khoản.

    Args:
        email: email tài khoản Google.
        account: tên account (mặc định: suy ra từ email — phần trước `@`).
        pool: tên pool trong `pools/` (mặc định: `DEFAULT_POOLS` hoặc tên account).
        password: mật khẩu (loại 2). Lưu mã hoá DPAPI.
        auth: `"password"` nếu có mật khẩu, `"oauth"` nếu không (loại 1).

    Returns:
        dict thông tin account (không chứa mật khẩu).
    """
    email = email.strip()
    if not email or "@" not in email:
        raise ValueError(f"email không hợp lệ: {email!r}")

    key = _key(account or email.split("@")[0])
    pool_name = pool or _default_pool(key)
    resolved_auth = auth or ("password" if password else "oauth")

    data = _read()
    entry = data["accounts"].get(key) or {}
    entry.update(
        {
            "email": email,
            "pool": pool_name,
            "auth": resolved_auth,
            "created_at": entry.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    data["accounts"][key] = entry
    _write(data)

    if password:
        credentials.set_password(key, email, password, pool=pool_name, auth=resolved_auth)
        entry["password_set"] = True
    else:
        entry["password_set"] = bool(credentials.get_password(key))

    _ensure_pool_file(pool_name, key, email)

    return _public(key, entry)


def set_password(account: str, password: str) -> dict[str, Any]:
    """Đặt/cập nhật mật khẩu (mã hoá DPAPI) cho account đã có."""
    key = _key(account)
    data = _read()
    entry = data["accounts"].get(key)
    if not entry:
        raise KeyError(f"account {key!r} chưa tồn tại — dùng add_account trước")
    credentials.set_password(key, entry.get("email", ""), password, pool=entry.get("pool"))
    entry["auth"] = "password"
    data["accounts"][key] = entry
    _write(data)
    return _public(key, entry)


def adopt_cookie_json(
    account: str,
    raw_text: str,
    *,
    email: str | None = None,
    pool: str | None = None,
) -> dict[str, Any]:
    """Dán cookie JSON (từ clipboard) cho 1 account — 1 lệnh làm hết.

    Đây là đường nhập cookie **không cần file tạm**: user copy JSON từ
    Cookie-Editor → paste vào popup → bấm "Lưu".

    Khác với `cookie_sync.adopt()` (file) ở chỗ:
      - Input là **chuỗi JSON dán**, không phải path.
      - **Không xoá** profile Chrome của account nếu đã tồn tại (xoá sẽ phá
        DBSC key của session đang chạy).

    Nếu account chưa có trong registry → tự tạo (email lấy từ tham số, hoặc
    suy ra từ tên account).
    """
    from orchestrator import cookie_sync

    key = _key(account)
    info = get(key)
    if info is None:
        info = add_account(
            email or f"{key}@gmail.com",
            account=key,
            pool=pool,
            auth="oauth",
        )

    resolved_email = email or info.get("email")
    resolved_pool = pool or info.get("pool") or key

    try:
        out = cookie_sync.adopt_json(
            key,
            raw_text,
            pool_name=resolved_pool,
            account_email=resolved_email,
        )
    except ValueError as e:
        note_error(key, str(e))
        raise

    if out.get("signed_in"):
        note_ok(key)
    else:
        note_error(
            key,
            "cookie đã lưu nhưng trang Gemini vẫn chưa đăng nhập — "
            "cookie có thể đã hết hạn hoặc thiếu cookie khác.",
        )
    out["account"] = key
    out["pool"] = resolved_pool
    return out


def get(account: str) -> dict[str, Any] | None:
    """Thông tin 1 account (không chứa mật khẩu plaintext)."""
    key = _key(account)
    entry = _read()["accounts"].get(key)
    return _public(key, entry) if entry else None


def list_accounts() -> list[dict[str, Any]]:
    """Danh sách tất cả account."""
    data = _read()
    out = []
    for key, entry in (data.get("accounts") or {}).items():
        out.append(_public(key, entry))
    return out


def remove(account: str, *, delete_pool: bool = False) -> bool:
    """Xoá account khỏi registry (và tuỳ chọn xoá pool file)."""
    key = _key(account)
    data = _read()
    entry = data["accounts"].pop(key, None)
    if entry is None:
        return False
    _write(data)
    credentials.delete_account(key)
    if delete_pool and entry.get("pool"):
        (POOLS_DIR / f"{entry['pool']}.json").unlink(missing_ok=True)
    return True


def note_ok(account: str) -> None:
    """Ghi nhận session còn sống."""
    key = _key(account)
    data = _read()
    entry = data["accounts"].get(key)
    if not entry:
        return
    entry["last_ok_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry.pop("last_error", None)
    data["accounts"][key] = entry
    _write(data)


def note_error(account: str, error: str) -> None:
    """Ghi nhận lỗi (session chết, đăng nhập fail...)."""
    key = _key(account)
    data = _read()
    entry = data["accounts"].get(key)
    if not entry:
        return
    entry["last_error"] = error
    entry["last_error_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["accounts"][key] = entry
    _write(data)


def get_password(account: str) -> str | None:
    """Mật khẩu đã giải mã (dùng nội bộ, không trả ra JSON response)."""
    return credentials.get_password(_key(account))


# ---------------------------------------------------------------------------
# Pool file (cache cookie)
# ---------------------------------------------------------------------------

def _ensure_pool_file(pool_name: str, account: str, email: str) -> Path:
    """Tạo pool file rỗng nếu chưa có (pool = cache cookie)."""
    POOLS_DIR.mkdir(parents=True, exist_ok=True)
    p = POOLS_DIR / f"{pool_name}.json"
    if not p.is_file():
        _write_json_atomic(
            p,
            {
                "name": pool_name,
                "account_email": email,
                "browser": "chrome",
                "profile": account,
                "source": "orchestrator-account-registry",
                "cookies": [],
            },
        )
    return p


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _public(key: str, entry: dict[str, Any] | None) -> dict[str, Any]:
    """Bỏ mọi field nhạy cảm khỏi entry trước khi trả ra ngoài."""
    if entry is None:
        return {}
    return {
        "account": key,
        "email": entry.get("email"),
        "pool": entry.get("pool"),
        "auth": entry.get("auth"),
        "has_password": bool(credentials.get_password(key)),
        "created_at": entry.get("created_at"),
        "last_ok_at": entry.get("last_ok_at"),
        "last_error": entry.get("last_error"),
        "last_error_at": entry.get("last_error_at"),
    }
