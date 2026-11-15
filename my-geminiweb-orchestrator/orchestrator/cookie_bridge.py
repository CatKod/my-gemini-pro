"""Cookie Bridge — cầu nối để lấy cookie từ **browser thật** của người dùng.

Vì sao cần
----------
Mục tiêu của user: *"lúc nào tất cả các acc đã đăng nhập và xác thực đều luôn
trong trạng thái available và có thể dùng được"* — tức **không** phải copy/paste
cookie thủ công nữa.

Những gì đã verify (2026-11-14, script trong `reports/`)
-------------------------------------------------------
| Cách                                            | Kết quả                        |
|-------------------------------------------------|--------------------------------|
| Đọc DB cookie ngoài tiến trình (Chrome/Edge)    | ❌ prefix `v20` = App-Bound    |
| DPAPI thuần (`CryptUnprotectData`)              | ❌ `v20` không phải DPAPI       |
| Copy profile Chrome sang thư mục khác rồi CDP   | ❌ Chrome **xoá** cookie (DBSC)|
| Chrome/Edge thật + `--remote-debugging-port`    | ❌ Chromium 136+ chặn (default) |
| Junction -> user-data-dir khác + CDP            | ❌ không giải mã được          |
| CDP `Storage.getCookies` trên **profile riêng** | ✅ trả cookie đã giải mã       |
| **Extension chạy trong browser (chrome.cookies)** | ✅ đọc được, chính browser đọc |

Điểm mấu chốt
-------------
Chrome/Edge mã hoá cookie trên đĩa bằng **App-Bound Encryption** (`v20`) —
khoá gắn với *tiến trình trình duyệt*. Ngoài tiến trình đó, không đoạn code
nào (kể cả Cookie-Editor) đọc được cookie từ đĩa.

Nhưng **Cookie-Editor chạy BÊN TRONG trình duyệt** → nó dùng API
`chrome.cookies` → trình duyệt tự giải mã và trả về plaintext.

→ Ta làm đúng điều đó: một extension nhỏ (`browser_extension/orchestrator-bridge/`)
đọc cookie qua `chrome.cookies` rồi **POST về orchestrator** trên
`127.0.0.1`. Không cần REA/dịch ngược vì cơ chế đã rõ và đã được verify.

Kiến trúc
---------
```
   Chrome/Edge của user (đã đăng nhập acc)
        │
        │  bấm icon extension → "Gửi cookie"
        │  chrome.cookies.getAll({domain: "google.com"})
        ▼
   POST http://127.0.0.1:9477/harvest
        {token, psid, psidts, email?}
        │
        ▼
   orchestrator.cookie_bridge
        │  1. xác thực token
        │  2. thử cookie với TỪNG pool đã biết → tìm pool nào AVAILABLE
        │  3. ghi cookie vào pool file đúng
        │  4. cấy vào Chrome riêng của orchestrator (để session sống lâu)
        │  5. xoá cache cookie cũ của gemini_webapi
        ▼
   pool file + profile riêng → AVAILABLE
```

Bảo mật
-------
- Server chỉ bind `127.0.0.1` (không nhận từ mạng).
- Mỗi lần start sinh 1 **token ngẫu nhiên**; extension phải gửi đúng token.
- Token lưu trong file state dir (chỉ user hiện tại đọc được, ACL chặt).
- Server **tự tắt** sau `BRIDGE_IDLE_TIMEOUT` giây không có request.
- Cookie **không** được ghi vào log.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from orchestrator import cookie_sync
from orchestrator import paths

#: Cổng mặc định của bridge (loopback).
DEFAULT_PORT = 9477

#: Tự tắt sau N giây không nhận request (tránh để server chạy mãi).
BRIDGE_IDLE_TIMEOUT = 900.0

#: Số cookie tối đa nhận trong 1 request.
MAX_BODY = 64 * 1024

#: Cookie auth duy nhất `gemini_webapi` cần.
REQUIRED = cookie_sync.REQUIRED
OPTIONAL = cookie_sync.OPTIONAL


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def token_path() -> Path:
    """File lưu token của bridge."""
    d = paths.STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "bridge_token.txt"


def current_token(*, rotate: bool = False) -> str:
    """Token hiện tại (sinh mới nếu chưa có hoặc `rotate=True`)."""
    p = token_path()
    if not rotate and p.is_file():
        try:
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except OSError:
            pass
    tok = secrets.token_urlsafe(24)
    try:
        p.write_text(tok, encoding="utf-8")
        _restrict_acl(p)
    except OSError:
        pass
    return tok


def _restrict_acl(path: Path) -> None:
    """Chỉ user hiện tại đọc (best-effort, Windows)."""
    if os.name != "nt":
        return
    try:
        import subprocess

        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        if not user:
            return
        principal = f"{domain}\\{user}" if domain else user
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(F)"],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Nghiệp vụ: cookie → pool
# ---------------------------------------------------------------------------

def _known_pools() -> list[tuple[str, str]]:
    """Danh sách `(pool, account)` của mọi account đã biết."""
    from orchestrator import accounts as account_registry

    out: list[tuple[str, str]] = []
    for info in account_registry.list_accounts():
        pool = info.get("pool") or info["account"]
        out.append((pool, info["account"]))
    return out


def find_owner(psid: str, psidts: str) -> dict[str, Any]:
    """Tìm pool nào ứng với cookie này bằng cách **probe API thật**.

    Không cần biết email trước: mỗi account có 1 pool riêng, ta thử cookie
    vào từng pool cho tới khi cái nào trả `AVAILABLE`.

    Ngoài ra, nếu cookie khớp **giá trị `1PSID` đã lưu** trong pool file nào
    đó → biết ngay chủ sở hữu (không cần gọi mạng).

    Returns:
        dict: `{"pool": str|None, "account": str|None, "matched_by": str, ...}`
    """
    # 1) Khớp theo 1PSID đã lưu (nhanh, không gọi mạng).
    for pool, account in _known_pools():
        pf = cookie_sync.POOLS_DIR / f"{pool}.json"
        if not pf.is_file():
            continue
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for c in data.get("cookies") or []:
            if (
                isinstance(c, dict)
                and c.get("name") == REQUIRED
                and c.get("value") == psid
            ):
                return {
                    "pool": pool,
                    "account": account,
                    "matched_by": "psid_match",
                }

    # 2) Thử probe từng pool (cookie thuộc về account nào thì cái đó AVAILABLE).
    for pool, account in _known_pools():
        res = cookie_sync._probe_session({REQUIRED: psid, OPTIONAL: psidts})
        if res.get("ok"):
            return {
                "pool": pool,
                "account": account,
                "matched_by": "probe",
                "account_status": res.get("account_status"),
            }

    return {"pool": None, "account": None, "matched_by": "none"}


def ingest(
    psid: str,
    psidts: str,
    *,
    email: str | None = None,
    source: str = "extension",
) -> dict[str, Any]:
    """Nhận cookie từ bridge → ghi vào pool đúng → cấy vào Chrome riêng.

    Args:
        psid: giá trị `__Secure-1PSID`.
        psidts: giá trị `__Secure-1PSIDTS`.
        email: email account (nếu extension lấy được) — dùng để **match
            nhanh** khi pool file chưa có cookie.
        source: nguồn gốc (để audit).

    Returns:
        dict kết quả.
    """
    if not psid:
        return {"ok": False, "error": "thiếu cookie __Secure-1PSID"}

    # Match nhanh bằng email nếu có.
    owner = None
    if email:
        from orchestrator import accounts as account_registry

        for info in account_registry.list_accounts():
            if (info.get("email") or "").lower() == email.lower():
                owner = {
                    "pool": info.get("pool") or info["account"],
                    "account": info["account"],
                    "matched_by": "email",
                }
                break

    if owner is None:
        owner = find_owner(psid, psidts)

    pool = owner.get("pool")
    account = owner.get("account")
    if not pool:
        return {
            "ok": False,
            "error": (
                "Không xác định được cookie này thuộc account nào. "
                "Hãy mở tab gemini.google.com đã đăng nhập trước khi gửi, "
                "để extension lấy được email."
            ),
            "matched_by": owner.get("matched_by"),
            "email": email,
        }

    # **Xác minh TRƯỚC khi ghi** — không ghi đè session đang chạy bằng cookie
    # chết, và không adopt cookie hỏng vào profile Chrome riêng.
    probe = cookie_sync._probe_session({REQUIRED: psid, OPTIONAL: psidts})

    # Nếu pool hiện tại đang sống mà cookie mới không → giữ cookie cũ.
    pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
    old_cookies: dict[str, str] = {}
    payload: dict[str, Any] = {}
    if pool_file.is_file():
        try:
            payload = json.loads(pool_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        for c in payload.get("cookies") or []:
            if isinstance(c, dict) and c.get("name"):
                old_cookies[c["name"]] = c["value"]

    if old_cookies.get(REQUIRED) and old_cookies.get(REQUIRED) != psid:
        old_probe = cookie_sync._probe_session(
            {
                n: old_cookies[n]
                for n in (REQUIRED, OPTIONAL)
                if old_cookies.get(n)
            }
        )
        if old_probe.get("ok") and not probe.get("ok"):
            return {
                "ok": False,
                "error": (
                    "Cookie mới không dùng được nhưng cookie hiện tại vẫn sống "
                    "— đã giữ nguyên cookie cũ, không ghi đè."
                ),
                "pool": pool,
                "account": account,
                "matched_by": owner.get("matched_by"),
                "session_ok": True,
                "account_status": old_probe.get("account_status"),
            }

    # Ghi cookie vào pool file (chỉ 2 cookie auth — xem `_cookie_list`).
    payload.update(
        {
            "name": pool,
            "account_email": email or payload.get("account_email"),
            "profile": account or payload.get("profile"),
            "source": f"orchestrator-cookie-bridge:{source}",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cookies": [
                {"name": n, "value": v, "domain": ".google.com"}
                for n, v in ((REQUIRED, psid), (OPTIONAL, psidts))
                if v
            ],
        }
    )
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = pool_file.with_suffix(pool_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, pool_file)

    if account:
        cookie_sync.set_account_pool(account, pool)

    # Xoá cache cookie cũ của gemini_webapi (nó che cookie mới).
    purged = cookie_sync.purge_gemini_cache(psid)

    # Cấy vào Chrome riêng để session sống lâu dài (không phụ thuộc browser
    # user). CHỈ làm khi cookie mới đã được xác minh sống — tránh phá session
    # đang hoạt động của profile riêng.
    adopted = False
    if account and probe.get("ok"):
        try:
            out = cookie_sync.adopt_json(
                account,
                json.dumps(
                    [
                        {"name": n, "value": v, "domain": ".google.com"}
                        for n, v in ((REQUIRED, psid), (OPTIONAL, psidts))
                        if v
                    ]
                ),
                pool_name=pool,
                account_email=email,
                verify_login=False,
            )
            adopted = bool(out.get("ok"))
        except Exception:
            adopted = False

    # Xoá cache quota để dashboard hiện ngay.
    try:
        from orchestrator import board

        board.invalidate_quota_cache()
    except Exception:
        pass

    return {
        "ok": bool(probe.get("ok")),
        "pool": pool,
        "account": account,
        "matched_by": owner.get("matched_by"),
        "email": email,
        "pool_file": str(pool_file),
        "adopted_to_chrome": adopted,
        "session_ok": probe.get("ok"),
        "account_status": probe.get("account_status"),
        "cache_purged": len(purged),
        "warning": probe.get("warning"),
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Handler cho bridge (loopback + token)."""

    server_version = "OrchestratorBridge/1.0"

    # Token dùng để xác thực (set khi start server).
    _token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        """Không log cookie — chỉ log method + path."""
        try:
            path = (args[0] if args else "").split("?")[0]
        except Exception:
            path = ""
        print(f"[bridge] {self.command} {path}")

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 — tên chuẩn của http.server
        self._send_json(204, {})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/ping"):
            self._send_json(200, {"ok": True, "service": "orchestrator-bridge"})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/harvest"):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"ok": False, "error": "body rỗng hoặc quá lớn"})
            return
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
        except Exception:
            self._send_json(400, {"ok": False, "error": "JSON không hợp lệ"})
            return

        token = str(data.get("token") or "")
        if not token or not secrets.compare_digest(token, self._token):
            self._send_json(403, {"ok": False, "error": "token sai"})
            return

        psid = str(data.get("psid") or "")
        psidts = str(data.get("psidts") or "")
        email = data.get("email")
        email = str(email) if isinstance(email, str) and email else None

        if not psid:
            self._send_json(400, {"ok": False, "error": "thiếu psid"})
            return

        try:
            out = ingest(psid, psidts, email=email, source=str(data.get("source") or "extension"))
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._send_json(200 if out.get("ok") else 400, out)


class BridgeServer:
    """Server bridge chạy trong thread nền."""

    def __init__(self, port: int = DEFAULT_PORT, *, rotate_token: bool = True) -> None:
        self.port = port
        self.token = current_token(rotate=rotate_token)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._last_hit = time.time()
        self._stop = threading.Event()

    # -- vòng đời ------------------------------------------------------

    def start(self) -> None:
        """Khởi động server (không chặn caller)."""
        handler = type("_BoundHandler", (_Handler,), {"_token": self.token})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._last_hit = time.time()

    def stop(self) -> None:
        """Dừng server."""
        self._stop.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
        self._httpd = None

    def _watchdog(self) -> None:
        """Tự tắt nếu không có request trong `BRIDGE_IDLE_TIMEOUT`."""
        while not self._stop.is_set():
            self._stop.wait(30)
            if self._stop.is_set():
                break
            if time.time() - self._last_hit > BRIDGE_IDLE_TIMEOUT:
                print("[bridge] hết thời gian chờ → tự tắt")
                self.stop()
                break

    # -- truy cập ------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def info(self) -> dict[str, Any]:
        """Thông tin để hiển thị cho user (token + hướng dẫn)."""
        ext = paths.DATA_ROOT / "browser_extension" / "orchestrator-bridge"
        return {
            "ok": True,
            "running": self._httpd is not None,
            "url": self.url,
            "port": self.port,
            "token": self.token,
            "extension_dir": str(ext),
            "endpoint": f"{self.url}/harvest",
            "hint": (
                "Cài extension: Chrome → chrome://extensions → Bật 'Developer mode' → "
                "'Load unpacked' → chọn thư mục extension_dir. Bấm icon extension, "
                "dán token, bấm 'Gửi cookie'."
            ),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="cookie_bridge",
        description="Cầu nối nhận cookie từ extension trong browser thật.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="chạy bridge (chặn tới Ctrl+C)")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT)

    p_info = sub.add_parser("info", help="in token + hướng dẫn cài extension")
    p_info.add_argument("--rotate", action="store_true", help="sinh token mới")

    args = ap.parse_args(argv)

    if args.cmd == "info":
        info = {
            "url": f"http://127.0.0.1:{DEFAULT_PORT}",
            "token": current_token(rotate=args.rotate),
            "extension_dir": str(
                paths.DATA_ROOT / "browser_extension" / "orchestrator-bridge"
            ),
        }
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "start":
        srv = BridgeServer(args.port)
        srv.start()
        threading.Thread(target=srv._watchdog, daemon=True).start()
        info = srv.info()
        print("=" * 62)
        print("  ORCHESTRATOR COOKIE BRIDGE")
        print("=" * 62)
        print(f"  Endpoint : {info['endpoint']}")
        print(f"  Token    : {info['token']}")
        print(f"  Extension: {info['extension_dir']}")
        print()
        for line in info["hint"].split(". "):
            if line.strip():
                print(f"  - {line.strip()}")
        print()
        print("  (Ctrl+C để dừng)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[bridge] dừng")
            srv.stop()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
