"""Tự động thu hoạch cookie từ browser mà user **đã đăng nhập sẵn**.

Bối cảnh
--------
Mục tiêu của user: *"lúc nào tất cả các acc đã đăng nhập và xác thực đều luôn
trong trạng thái available và có thể dùng được"* — tức không phải copy/paste
cookie thủ công nữa.

Những gì đã verify (2026-11-14, script trong `reports/`)
---------------------------------------------------------
| Cách                                             | Kết quả                       |
|--------------------------------------------------|-------------------------------|
| Đọc DB cookie ngoài tiến trình (Chrome/Edge)     | ❌ prefix `v20` = App-Bound    |
| DPAPI thuần (`CryptUnprotectData`)               | ❌ `v20` không phải DPAPI       |
| Copy profile Chrome sang thư mục khác rồi CDP    | ❌ Chrome **xoá** cookie (DBSC) |
| Chrome thật + `--remote-debugging-port`          | ❌ Chrome ≥136 chặn (default dir) |
| Chrome thật + short path 8.3                      | ⚠️ port mở được nhưng cookie = 0 |
| **CDP `Storage.getCookies` trên profile riêng**  | ✅ trả cookie đã giải mã       |

Kết luận
---------
**Không thể** đọc cookie từ profile Chrome/Edge thật của user một cách an toàn
và ổn định (App-Bound Encryption + DBSC). REA / dịch ngược Cookie-Editor cũng
không giúp, vì Cookie-Editor chỉ đọc cookie qua `chrome.cookies` API **bên
trong** browser — tức là phải mở browser với profile của user.

Đường khả thi duy nhất (và là đường đã xây)
-------------------------------------------
Orchestrator dùng **profile Chrome riêng** cho mỗi account
(`%LOCALAPPDATA%\\OrchestratorChrome\\<account>`). Profile riêng này:
  * không bị ràng buộc DBSC của profile user → Chrome **tự giải mã** cookie
    trong chính tiến trình của nó,
  * CDP đọc được toàn bộ cookie đã giải mã (`Storage.getCookies`).

Nên "tự động lấy cookie" = **mở Chrome riêng đó → đọc cookie → ghi pool**.
Đó chính là `cookie_sync.sync()`, và module này mở rộng nó thành:

    harvest_all()  — quét MỌI profile riêng, sync từng cái, tự chữa session chết.

Chữa session chết
-----------------
Khi session chết (Google đã hết hạn), 2 khả năng theo thứ tự ưu tiên:
  1. **Tài khoản loại 2** (có mật khẩu trong `credentials.json`, mã hoá DPAPI)
     → `auto_login.auto_login()` điền form đăng nhập tự động.
  2. **Tài khoản loại 1** (OAuth / 2FA) → không tự động hoá được (không bypass
     2FA) → báo `needs_human`, mở Chrome riêng cho user đăng nhập tay.

Không bao giờ
-------------
* Đọc/ghi profile Chrome **thật** của user.
* Bypass CAPTCHA / 2FA.
* Ghi cookie vào log hoặc file ngoài `pools/` (gitignore).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from orchestrator import accounts as account_registry
from orchestrator import cookie_sync

#: Backoff cho account đã thử chữa mà thất bại (giây).
#:
#: Vì sao cần: `auto_heal_tick` chạy mỗi `AUTO_HEAL_INTERVAL` (180s). Nếu
#: account loại 1 (2FA) chết session, mỗi lần lại mở Chrome → spam + tốn
#: tài nguyên + mở cửa sổ Chrome liên tục. Backoff tăng dần theo số lần thất
#: bại, và reset khi account sống lại.
#:
#: Lưu trong file state dir để giữ qua các lần restart của GUI.
BACKOFF_FILE = "harvest_backoff.json"

#: Các mốc backoff (giây) theo số lần thất bại liên tiếp.
BACKOFF_STEPS = (0, 600, 1800, 3600, 7200)


def _backoff_path() -> Path:
    from orchestrator import paths

    d = paths.STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / BACKOFF_FILE


def _load_backoff() -> dict[str, Any]:
    p = _backoff_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_backoff(data: dict[str, Any]) -> None:
    try:
        p = _backoff_path()
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _backoff_delay(fails: int) -> float:
    """Độ trễ giữa 2 lần thử chữa, theo số lần thất bại liên tiếp."""
    idx = min(fails, len(BACKOFF_STEPS) - 1)
    return float(BACKOFF_STEPS[idx])


def _backoff_gate(account: str) -> float:
    """Số giây còn phải chờ trước khi thử lại account này (0 = được thử)."""
    data = _load_backoff()
    entry = data.get(account)
    if not isinstance(entry, dict):
        return 0.0
    fails = int(entry.get("fails") or 0)
    if fails <= 0:
        return 0.0
    last = float(entry.get("last_at") or 0.0)
    wait = _backoff_delay(fails) - (time.time() - last)
    return max(0.0, wait)


def _backoff_record(account: str, *, ok: bool) -> None:
    """Ghi nhận kết quả chữa session (ok → reset, fail → tăng backoff)."""
    data = _load_backoff()
    entry = data.get(account)
    if not isinstance(entry, dict):
        entry = {}
    if ok:
        data.pop(account, None)
    else:
        entry["fails"] = int(entry.get("fails") or 0) + 1
        entry["last_at"] = time.time()
        data[account] = entry
    _save_backoff(data)


# ---------------------------------------------------------------------------
# Quét profile
# ---------------------------------------------------------------------------

def list_profiles() -> list[dict[str, Any]]:
    """Liệt kê các profile Chrome riêng của orchestrator.

    Mỗi entry: `{"account": str, "dir": str, "has_1psid": bool, "cookies": int}`.
    """
    root = cookie_sync.ORCH_CHROME_ROOT
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        entry: dict[str, Any] = {
            "account": d.name,
            "dir": str(d),
            "has_1psid": False,
            "cookies": 0,
        }
        # Đọc nhanh từ DB cookie (không mở Chrome) để lọc profile rác.
        db = d / "Default" / "Network" / "Cookies"
        if db.is_file():
            try:
                import shutil
                import sqlite3
                import tempfile

                tmp = Path(tempfile.gettempdir()) / f"orch_harvest_{id(d)}.db"
                shutil.copy2(db, tmp)
                con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
                try:
                    entry["cookies"] = con.execute(
                        "select count(*) from cookies"
                    ).fetchone()[0]
                    entry["has_1psid"] = (
                        con.execute(
                            "select count(*) from cookies where name = ?",
                            (cookie_sync.REQUIRED,),
                        ).fetchone()[0]
                        > 0
                    )
                finally:
                    con.close()
                    tmp.unlink(missing_ok=True)
            except Exception:
                pass
        out.append(entry)
    return out


def _known_accounts() -> list[str]:
    """Danh sách account trong registry."""
    return [a["account"] for a in account_registry.list_accounts()]


# ---------------------------------------------------------------------------
# Thu hoạch
# ---------------------------------------------------------------------------

def harvest_account(
    account: str,
    *,
    pool_name: str | None = None,
    heal: bool = True,
) -> dict[str, Any]:
    """Thu hoạch cookie của 1 account + tự chữa nếu session chết.

    Args:
        account: tên account.
        pool_name: tên pool (mặc định: pool của account trong registry).
        heal: nếu session chết → thử đăng nhập lại tự động (chỉ với account
            loại 2 có mật khẩu; loại 1 trả `needs_human`).

    Returns:
        dict kết quả — xem docstring module.
    """
    info = account_registry.get(account) or {}
    pool = pool_name or info.get("pool") or cookie_sync.default_pool_for(account)

    # Backoff: không thử chữa lại account vừa thất bại (tránh spam Chrome).
    wait = _backoff_gate(account) if heal else 0.0

    result = cookie_sync.sync(account, pool_name=pool)
    result.setdefault("account", account)
    result.setdefault("pool", pool)

    if not result.get("ok"):
        result["healed"] = False
        result["heal_error"] = str(result.get("error") or "")
        return result

    if result.get("session_ok"):
        account_registry.note_ok(account)
        _backoff_record(account, ok=True)
        result["healed"] = False
        return result

    # Session chết → thử chữa.
    if not heal:
        result["healed"] = False
        return result

    if wait > 0:
        result["healed"] = False
        result["heal"] = {
            "ok": False,
            "state": "backoff",
            "reason": (
                f"Đang nghỉ {int(wait)}s trước lần thử lại tiếp theo "
                "(lần trước thất bại)."
            ),
        }
        return result

    result["healed"] = True
    result["heal"] = _heal(account, pool)
    result["session_ok"] = result["heal"].get("session_ok")
    result["account_status"] = result["heal"].get("account_status")
    _backoff_record(account, ok=bool(result["heal"].get("ok")))
    return result


def _heal(account: str, pool: str) -> dict[str, Any]:
    """Cố gắng làm sống lại session của account.

    - Loại 2 (có mật khẩu) → `auto_login.auto_login()`.
    - Loại 1 → `cookie_sync.login()` mở Chrome cho user đăng nhập tay.
    """
    from orchestrator import auto_login

    info = account_registry.get(account) or {}
    has_pw = bool(account_registry.get_password(account))

    if has_pw or info.get("auth") == "password":
        try:
            out = auto_login.auto_login(account, keep_open=True)
        except Exception as e:
            return {
                "ok": False,
                "state": "failed",
                "reason": f"{type(e).__name__}: {e}",
            }
        if out.get("ok"):
            return {
                "ok": True,
                "state": "ok",
                "reason": "đăng nhập lại tự động thành công",
                "session_ok": out.get("session_ok"),
                "account_status": out.get("account_status"),
            }
        return {
            "ok": False,
            "state": out.get("state") or "unknown",
            "reason": out.get("reason") or "đăng nhập lại thất bại",
            "hint": out.get("hint"),
        }

    # Loại 1: mở Chrome riêng cho user đăng nhập tay.
    try:
        cookie_sync.login(account)
    except Exception as e:
        return {
            "ok": False,
            "state": "failed",
            "reason": f"{type(e).__name__}: {e}",
        }
    return {
        "ok": False,
        "state": "needs_human",
        "reason": (
            "Tài khoản loại 1 (có 2FA/OAuth) — đã mở Chrome riêng để bạn "
            "đăng nhập lại. Đăng nhập xong bấm 'Đồng bộ cookie'."
        ),
        "hint": f"Chrome riêng: {cookie_sync.account_dir(account)}",
    }


def harvest_all(
    *,
    heal: bool = True,
    only_registry: bool = True,
) -> dict[str, Any]:
    """Thu hoạch cookie cho **mọi** account + tự chữa session chết.

    Args:
        heal: tự đăng nhập lại nếu session chết.
        only_registry: chỉ quét account có trong registry. `False` → thêm mọi
            profile Chrome riêng tìm thấy (kể cả chưa khai báo).

    Returns:
        dict tổng kết — xem `harvest_account()`.
    """
    accounts = _known_accounts() if only_registry else []
    if not only_registry:
        known = set(accounts)
        accounts = [p["account"] for p in list_profiles() if p["account"] not in known]

    results: list[dict[str, Any]] = []
    for acc in accounts:
        try:
            r = harvest_account(acc, heal=heal)
        except Exception as e:
            r = {
                "ok": False,
                "account": acc,
                "error": f"{type(e).__name__}: {e}",
            }
        results.append(r)

    alive = [r for r in results if r.get("session_ok")]
    dead = [r for r in results if not r.get("session_ok")]
    needs_human = [
        r for r in dead if (r.get("heal") or {}).get("state") == "needs_human"
    ]
    backoff = [r for r in dead if (r.get("heal") or {}).get("state") == "backoff"]

    # Xoá cache quota để dashboard hiển thị ngay trạng thái mới.
    try:
        from orchestrator import board

        board.invalidate_quota_cache()
    except Exception:
        pass

    return {
        "ok": bool(alive) or not results,
        "total": len(results),
        "alive": len(alive),
        "dead": len(dead),
        "needs_human": len(needs_human),
        "in_backoff": len(backoff),
        "results": results,
        "generated_at": time.time(),
    }


def write_harvest_report(payload: dict[str, Any]) -> Path:
    """Ghi báo cáo thu hoạch ra `reports/` (không chứa cookie)."""
    from orchestrator import paths

    d = paths.REPORTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = d / f"harvest-{stamp}.json"
    # Lược bỏ mọi giá trị cookie khỏi báo cáo.
    safe = json.loads(json.dumps(payload, default=str))
    for r in safe.get("results", []):
        r.pop("cookies_live", None)
        r.pop("cookies", None)
    p.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
    return p
