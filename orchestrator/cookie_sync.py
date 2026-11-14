"""Tự động hoá việc lấy / cập nhật cookie Gemini — không cần Cookie-Editor.

Bối cảnh
--------
Người dùng trước đây phải: mở Chrome → vào Gemini → mở Cookie-Editor →
export JSON → paste. Lặp lại mỗi khi session hết hạn.

Điều tra thực nghiệm (chi tiết trong `docs/COOKIE-SETUP.md`):
    Chrome ≥127 mã hoá cookie bằng **App-Bound Encryption** (`v20`) + **Device
    Bound Session** (DBSC). Các hệ quả đã verify bằng script thật:

    1. Đọc DB cookie ngoài tiến trình → không giải mã được.
    2. **Copy** profile sang user-data-dir khác → Chrome không giải mã được
       DBSC → **xoá cookie** (65 → 0). Profile thật của user thì an toàn.
    3. Chrome ≥136 **chặn** `--remote-debugging-port` khi `--user-data-dir`
       trỏ đúng thư mục default. Bypass bằng short path 8.3 cho phép mở port
       nhưng cookie vẫn không giải mã (Chrome so sánh path đã resolve).

Cách lời giải đã verify thành công
-----------------------------------
Orchestrator sở hữu **một Chrome riêng** (user-data-dir riêng tại
`%LOCALAPPDATA%\\OrchestratorChrome\\<account>`). Chrome đó KHÔNG bị ràng buộc
Device Bound Session vào profile của user, nên:

    * **Adopt** (1 lần): cấy cookie hợp lệ (từ export Cookie-Editor) vào
      profile riêng qua CDP `Network.setCookie`. Chrome ghi lại cookie kèm
      DBSC key **của profile riêng** → browser đăng nhập thật (verify: trang
      Gemini hiện "Thư viện", "Đã lên lịch", lịch sử chat).
    * **Sync** (mỗi lần): mở Chrome riêng đó → đọc cookie live qua CDP
      `Storage.getCookies` → ghi vào `pools/<name>.json`.
    * **Persist**: đóng Chrome bằng CDP `Browser.close` (graceful) để Chrome
      flush cookie vào DB. Kill cứng → session cookie mất.

Điểm mấu chốt: **profile của user KHÔNG BAO GIỜ bị đụng** → không lo
"Evil Clone", không xoá session người dùng, không cần đóng Chrome của bạn.

Lưu ý an toàn
-------------
Module này KHÔNG tự đăng nhập, không tự xác thực 2FA, không bypass Google.
Nó chỉ đọc cookie từ Chrome mà người dùng đã đăng nhập sẵn (trong profile
riêng của orchestrator), rồi đồng bộ sang file pool.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_pools_dir() -> Path:
    """Tìm thư mục `pools/`.

    Thứ tự:
      1. `GEMINI_POOLS_DIR` (explicit, ưu tiên cao nhất).
      2. Khi chạy từ source → `<repo>/pools`.
      3. Khi chạy từ `.exe` → `<cwd>/pools` nếu cwd trông như repo, else
         `%LOCALAPPDATA%\\Orchestrator\\pools` (để .exe dùng độc lập repo).
    """
    env = os.environ.get("GEMINI_POOLS_DIR")
    if env:
        return Path(env)

    if not getattr(sys, "frozen", False):
        return REPO_ROOT / "pools"

    cwd = Path.cwd()
    if (cwd / "pools").is_dir() or (cwd / ".git").exists():
        return cwd / "pools"
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Orchestrator"
    return local / "pools"


POOLS_DIR = _resolve_pools_dir()

REQUIRED = "__Secure-1PSID"
OPTIONAL = "__Secure-1PSIDTS"

#: Các cookie cần cho phiên Gemini. Nhiều hơn thì càng tốt cho adopt.
SESSION_COOKIES = (
    REQUIRED,
    OPTIONAL,
    "__Secure-1PSIDCC",
    "__Secure-1PSIDRTS",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "__Secure-3PSIDCC",
    "__Secure-3PSIDRTS",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "SAPISID",
    "SSID",
    "APISID",
    "HSID",
    "SID",
    "NID",
    "AEC",
    "COMPASS",
)

#: Thư mục gốc của các Chrome riêng do orchestrator quản lý.
ORCH_CHROME_ROOT = Path(
    os.environ.get("ORCHESTRATOR_CHROME_HOME")
    or (Path(os.environ.get("LOCALAPPDATA", "")) / "OrchestratorChrome")
)

CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
)

DEFAULT_DEBUG_PORT = 9455
GEMINI_URL = "https://gemini.google.com/app"

#: Mapping account -> tên pool trong `pools/`. Tên pool khớp pool Antigravity
#: để routing `auto` nhất quán (xem `docs/ARCHITECTURE.md`).
#:
#: Được persist trong `pools/.account_pools.json` khi user thêm account mới
#: từ GUI, nên không mất khi restart.
ACCOUNT_POOLS = {
    "mmeo": "windows",
    "catkingmv": "wsl",
}

#: File lưu mapping account -> pool (thêm bởi GUI).
ACCOUNT_POOLS_FILE = "account_pools.json"


def _load_account_pools() -> dict[str, str]:
    """Đọc mapping account -> pool đã lưu (nếu có)."""
    f = POOLS_DIR / ACCOUNT_POOLS_FILE
    if not f.is_file():
        return {}
    try:
        raw = _read_json(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _save_account_pools(mapping: dict[str, str]) -> None:
    """Lưu mapping account -> pool vào `pools/.account_pools.json`."""
    try:
        _write_json_atomic(POOLS_DIR / ACCOUNT_POOLS_FILE, mapping)
    except OSError:
        pass


def set_account_pool(account: str, pool: str) -> None:
    """Gán pool cho account và lưu bền vững."""
    key = account.lower()
    ACCOUNT_POOLS[key] = pool
    mapping = _load_account_pools()
    mapping[key] = pool
    _save_account_pools(mapping)


def default_pool_for(account: str) -> str:
    """Pool mặc định của 1 account (fallback: chính tên account)."""
    key = account.lower()
    if key in ACCOUNT_POOLS:
        return ACCOUNT_POOLS[key]
    saved = _load_account_pools()
    if key in saved:
        return saved[key]
    return account


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------


def chrome_exe() -> Path | None:
    for c in CHROME_CANDIDATES:
        if c.is_file():
            return c
    return None


def account_dir(account: str) -> Path:
    """Thư mục user-data riêng cho 1 account."""
    safe = "".join(ch for ch in account.lower() if ch.isalnum() or ch in "._-")
    return ORCH_CHROME_ROOT / (safe or "default")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def extract_cookies(raw: Any) -> dict[str, str]:
    """Chấp nhận shape Cookie-Editor (`list`) hoặc pool (`dict`).

    Cookie-Editor export: `[{"domain": ..., "name": ..., "value": ...}, ...]`
    Pool file: `{"cookies": [{"name":..., "value":...}, ...]}`
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


def _account_port_base(account: str) -> int:
    """Port debug riêng cho từng account (tránh trùng khi chạy song song)."""
    digest = sum(ord(c) for c in account.lower())
    return DEFAULT_DEBUG_PORT + (digest % 400)


def _find_free_port(start: int = DEFAULT_DEBUG_PORT) -> int:
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Không tìm được port trống cho CDP.")


# ---------------------------------------------------------------------------
# Chrome session
# ---------------------------------------------------------------------------


class ChromeSession:
    """Chrome riêng của orchestrator + CDP client tối giểu.

    Vòng đời: `with ChromeSession(...) as s:` — luôn đóng graceful
    (`Browser.close`) để Chrome flush session cookie vào `Network/Cookies`.
    Kill cứng sẽ làm cookie mất.

    Nếu Chrome của account đó **đang chạy sẵn** (ví dụ sau lệnh `login`),
    ta **kết nối vào instance đó** thay vì spawn mới — Chrome không cho 2
    tiến trình dùng chung một user-data-dir. Khi đó ta không đóng browser
    (nó thuộc phiên đăng nhập của user).
    """

    def __init__(
        self,
        user_data_dir: Path,
        *,
        port: int = DEFAULT_DEBUG_PORT,
        start_url: str = "about:blank",
        reuse_existing: bool = True,
    ) -> None:
        self.user_data_dir = user_data_dir
        self.port = port
        self.start_url = start_url
        self.reuse_existing = reuse_existing
        self._proc: subprocess.Popen[bytes] | None = None
        self._cdp: Any = None
        self._owns_browser = False

    def __enter__(self) -> "ChromeSession":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _existing_debug_port(self) -> int | None:
        """Port debug của Chrome đang chạy với user-data-dir này (nếu có).

        Chrome 154 **không** tạo file `DevToolsActivePort`, nên ta tự ghi
        port ra file `orch-cdp-port.txt` ngay khi spawn. Nếu file có nhưng
        Chrome đã chết → xoá file, coi như không có instance nào.
        """
        marker = self.user_data_dir / "orch-cdp-port.txt"
        if not marker.is_file():
            return None
        try:
            port = int(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            marker.unlink(missing_ok=True)
            return None
        # Kiểm tra Chrome còn sống không.
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=3
            ):
                return port
        except (urllib.error.URLError, OSError, ValueError):
            marker.unlink(missing_ok=True)
            return None

    def start(self) -> None:
        from orchestrator import cdp

        exe = chrome_exe()
        if exe is None:
            raise RuntimeError("Không tìm thấy chrome.exe.")

        # Ưu tiên dùng lại instance đang chạy (sau lệnh `login`).
        if self.reuse_existing:
            existing = self._existing_debug_port()
            if existing:
                client = cdp.CDPClient(existing)
                try:
                    client.wait_until_ready(timeout=10)
                    client.connect()
                except Exception:
                    pass
                else:
                    self._cdp = client
                    self._owns_browser = False
                    return

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(exe),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,OptimizationHints",
            "--disable-background-networking",
            self.start_url,
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._owns_browser = True
        self._cdp = cdp.CDPClient(self.port)
        self._cdp.wait_until_ready()
        self._cdp.connect()
        # Ghi port để lần sau nhận ra instance đang chạy.
        (self.user_data_dir / "orch-cdp-port.txt").write_text(
            str(self.port), encoding="utf-8"
        )

    # -- API ---------------------------------------------------------------

    def cookies(self) -> dict[str, str]:
        return self._cdp.get_cookies()

    def set_cookies(self, cookies: dict[str, str]) -> int:
        """Cấy cookie vào profile (dùng cho `adopt`).

        `Network.setCookie` cần page session → attach 1 tab trước.
        """
        expires = int(time.time()) + 60 * 60 * 24 * 30
        ok = 0
        try:
            target_id = self._cdp.open_page("about:blank")
            session_id = self._cdp.attach(target_id)
        except Exception:
            return 0
        for name, value in cookies.items():
            try:
                self._cdp.set_cookie(
                    name, value, session_id=session_id, expires=expires
                )
                ok += 1
            except Exception:
                continue
        return ok

    def goto(self, url: str, wait_ms: int = 7000) -> str:
        """Mở URL và trả về text trang (dùng để kiểm tra đã đăng nhập)."""
        target_id = self._cdp.open_page(url)
        session_id = self._cdp.attach(target_id)
        return self._cdp.navigate(session_id, url, wait_ms=wait_ms)

    def close(self) -> None:
        """Đóng Chrome graceful (flush cookie) rồi kill nếu cần.

        Chỉ đóng nếu ta spawn nó. Nếu đang dùng lại instance của user (sau
        lệnh `login`), ta để nguyên — user cần nó để đăng nhập.
        """
        if self._cdp is not None:
            if self._owns_browser:
                try:
                    self._cdp.close_browser()
                except Exception:
                    pass
            try:
                self._cdp.close()
            except Exception:
                pass
            self._cdp = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=30)
            except Exception:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=10)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
        if self._owns_browser:
            (self.user_data_dir / "orch-cdp-port.txt").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Nghiệp vụ
# ---------------------------------------------------------------------------


def _looks_signed_out(page_text: str) -> bool:
    """Trang Gemini có đang yêu cầu đăng nhập không?

    Bẫy đã gặp: trang **chưa đăng nhập** vẫn hiện "Cuộc trò chuyện mới" →
    logic "có signed-in marker là được coi là đã đăng nhập" trả về sai
    (false positive). Thực tế trang chưa đăng nhập có nút "Đăng nhập" và
    "Tải ứng dụng"; trang đã đăng nhập có "Thư viện", "Đã lên lịch", lịch sử chat.

    Quy tắc: có CTA đăng nhập → coi như CHƯA đăng nhập (trừ khi có dấu hiệu
    đã đăng nhập rõ ràng như "thư viện"/"library").
    """
    t = (page_text or "").lower()
    signed_out_markers = (
        "sign in",
        "đăng nhập",
        "download the app",
        "tải ứng dụng",
    )
    strong_signed_in_markers = (
        "thư viện",
        "library",
        "đã lên lịch",
        "gems",
    )
    if any(m in t for m in strong_signed_in_markers):
        return False
    return any(m in t for m in signed_out_markers)


def adopt(
    account: str,
    export_path: Path,
    *,
    pool_name: str | None = None,
    account_email: str | None = None,
    port: int | None = None,
    verify_login: bool = True,
) -> dict[str, Any]:
    """Cấy cookie từ file export Cookie-Editor vào Chrome riêng của orchestrator.

    Thao tác **1 lần** cho mỗi account. Sau đó `sync()` tự động hoá.
    """
    raw = _read_json(export_path)
    cookies = extract_cookies(raw)
    if REQUIRED not in cookies:
        raise RuntimeError(
            f"File export không có cookie {REQUIRED}. "
            "Hãy export từ tab gemini.google.com đã đăng nhập."
        )

    udd = account_dir(account)
    if udd.exists():
        shutil.rmtree(udd, ignore_errors=True)
    udd.mkdir(parents=True, exist_ok=True)

    port = port or _find_free_port(_account_port_base(account))
    # `adopt` xoá profile → không thể dùng lại instance đang chạy.
    with ChromeSession(udd, port=port, reuse_existing=False) as s:
        added = s.set_cookies(cookies)
        live = s.cookies()
        page_text = s.goto(GEMINI_URL) if verify_login else ""

    signed_in = REQUIRED in live and not _looks_signed_out(page_text)

    name = pool_name or default_pool_for(account)
    # Giữ email đã biết nếu không truyền mới (tránh mất khi sync chạy lại).
    if not account_email:
        existing = POOLS_DIR / f"{name}.json"
        if existing.is_file():
            try:
                prev = _read_json(existing)
                if isinstance(prev, dict):
                    account_email = prev.get("account_email")
            except (OSError, ValueError):
                pass
    payload = {
        "name": name,
        "account_email": account_email,
        "browser": "chrome",
        "profile": account,
        "source": "orchestrator-cookie-sync",
        "cookies": [
            {"name": n, "value": cookies[n], "domain": ".google.com"}
            for n in (REQUIRED, OPTIONAL)
            if n in cookies
        ],
    }
    _write_json_atomic(POOLS_DIR / f"{name}.json", payload)
    set_account_pool(account, name)

    return {
        "ok": True,
        "account": account,
        "user_data_dir": str(udd),
        "cookies_seeded": added,
        "cookies_live": len(live),
        "signed_in": signed_in,
        "pool_file": str(POOLS_DIR / f"{name}.json"),
        "page_preview": page_text.replace("\n", " ")[:200],
    }


def sync(
    account: str,
    *,
    pool_name: str | None = None,
    account_email: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    """Đọc cookie từ Chrome riêng của orchestrator và ghi vào `pools/`.

    Chạy bất cứ lúc nào cần cookie mới — không cần Cookie-Editor, không cần
    user thao tác.
    """
    udd = account_dir(account)
    if not udd.is_dir():
        return {
            "ok": False,
            "account": account,
            "error": (
                f"Chưa có Chrome riêng cho account {account!r} tại {udd}. "
                "Chạy `adopt` một lần để cấy cookie từ file export Cookie-Editor."
            ),
        }

    port = port or _find_free_port(_account_port_base(account))
    try:
        with ChromeSession(udd, port=port) as s:
            live = s.cookies()
    except Exception as e:
        return {"ok": False, "account": account, "error": f"{type(e).__name__}: {e}"}

    if REQUIRED not in live:
        return {
            "ok": False,
            "account": account,
            "error": (
                f"Chrome riêng của {account!r} không còn cookie {REQUIRED} "
                "(session có thể đã hết hạn). Cần chạy `adopt` lại."
            ),
        }

    name = pool_name or default_pool_for(account)
    pool_file = POOLS_DIR / f"{name}.json"
    payload: dict[str, Any] = {}
    if pool_file.is_file():
        try:
            payload = _read_json(pool_file)
        except (OSError, ValueError):
            payload = {}

    payload.update(
        {
            "name": name,
            "account_email": account_email or payload.get("account_email"),
            "profile": account,
            "source": "orchestrator-cookie-sync",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cookies": [
                {"name": n, "value": live[n], "domain": ".google.com"}
                for n in (REQUIRED, OPTIONAL)
                if n in live
            ],
        }
    )
    _write_json_atomic(pool_file, payload)
    set_account_pool(account, name)

    # Kiểm tra session thật (không chỉ cookie còn sống).
    #
    # Bẫy đã gặp: cookie vẫn còn (21 cookie) nhưng Google đã hết hạn session
    # → `gemini_webapi` trả `UNAUTHENTICATED`, mọi Deep Research fail với
    # "Permission denied". Chỉ đọc cookie là không đủ.
    session = _probe_session(live)

    return {
        "ok": True,
        "account": account,
        "cookies": len(live),
        "pool_file": str(pool_file),
        "updated_at": payload["updated_at"],
        "session_ok": session.get("ok"),
        "account_status": session.get("account_status"),
        "warning": session.get("warning"),
    }


def _probe_session(cookies: dict[str, str]) -> dict[str, Any]:
    """Kiểm tra session Gemini còn sống không (gọi API thật).

    Trả về `{"ok": bool, "account_status": str|None, "warning": str|None}`.
    Không raise — lỗi mạng chỉ là `ok=False` + warning.
    """
    if REQUIRED not in cookies:
        return {"ok": False, "account_status": None, "warning": "thiếu cookie 1PSID"}

    try:
        import asyncio

        from gemini_webapi import GeminiClient
    except ImportError as e:
        return {"ok": None, "account_status": None, "warning": f"thiếu gemini_webapi: {e}"}

    async def _probe() -> str | None:
        client = GeminiClient(
            secure_1psid=cookies[REQUIRED],
            secure_1psidts=cookies.get(OPTIONAL),
            verbose=False,
        )
        try:
            await client.init(auto_refresh=False)
            status = getattr(client.account_status, "name", None)
            return status
        finally:
            try:
                await client.close()
            except Exception:
                pass

    try:
        status = asyncio.run(_probe())
    except Exception as e:
        return {
            "ok": None,
            "account_status": None,
            "warning": f"không kiểm tra được session: {type(e).__name__}: {e}",
        }

    ok = status == "ACTIVE"
    warning = None
    if not ok:
        warning = (
            f"Session KHÔNG còn hiệu lực (account_status={status}). "
            "Cookie vẫn còn nhưng Google đã hết hạn session → mọi Deep Research "
            "sẽ fail với 'Permission denied'. Cần chạy `adopt` lại (file export "
            "Cookie-Editor mới) hoặc `login` để đăng nhập lại trong Chrome riêng."
        )
    return {"ok": ok, "account_status": status, "warning": warning}


def login(account: str, *, port: int | None = None) -> dict[str, Any]:
    """Mở Chrome riêng của orchestrator để người dùng đăng nhập Google.

    Đây là cách **thay thế** cho Cookie-Editor: thay vì export cookie rồi
    `adopt`, user chỉ cần đăng nhập trực tiếp trong cửa sổ Chrome này.
    Sau đó `sync()` tự động lấy cookie — vĩnh viễn, không cần làm gì thêm.

    Không tự động đăng nhập (không bypass 2FA, không vi phạm ToS).
    """
    udd = account_dir(account)
    udd.mkdir(parents=True, exist_ok=True)

    exe = chrome_exe()
    if exe is None:
        raise RuntimeError("Không tìm thấy chrome.exe.")

    # Nếu Chrome của account này đang chạy rồi → không spawn thêm.
    marker = udd / "orch-cdp-port.txt"
    port = port or _find_free_port(_account_port_base(account))
    if marker.is_file():
        try:
            port = int(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            marker.unlink(missing_ok=True)

    cmd = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        "https://accounts.google.com/",
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Ghi port để `sync` nhận ra instance đang chạy.
    marker.write_text(str(port), encoding="utf-8")

    return {
        "ok": True,
        "account": account,
        "user_data_dir": str(udd),
        "debug_port": port,
        "next_step": (
            "Chrome đã mở. Đăng nhập Google trong cửa sổ đó, mở tab "
            "https://gemini.google.com/app để chắc chắn đã đăng nhập, "
            "rồi đóng Chrome và chạy: "
            f"python -m orchestrator.orch_cli cookie sync --account {account}"
        ),
    }


def status(account: str, *, pool_name: str | None = None) -> dict[str, Any]:
    """Trạng thái Chrome riêng + pool tương ứng (không cần mở Chrome)."""
    udd = account_dir(account)
    db = udd / "Default" / "Network" / "Cookies"
    cookie_count = 0
    has_psid = False
    if db.is_file():
        try:
            import sqlite3
            import tempfile

            tmp = Path(tempfile.gettempdir()) / f"orch_ck_{os.getpid()}.db"
            shutil.copy2(db, tmp)
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            try:
                cookie_count = con.execute("select count(*) from cookies").fetchone()[0]
                has_psid = (
                    con.execute(
                        "select count(*) from cookies where name = ?", (REQUIRED,)
                    ).fetchone()[0]
                    > 0
                )
            finally:
                con.close()
                tmp.unlink(missing_ok=True)
        except Exception:
            # Chrome đang giữ lock (WinError 32) — DB không đọc được offline.
            # Không phải lỗi nghiêm trọng: trạng thái thật nằm ở pool file.
            cookie_count = -1
            has_psid = False

    pool_name = pool_name or default_pool_for(account)
    pool_file = POOLS_DIR / f"{pool_name}.json"
    pool_cookies = 0
    account_email = None
    if pool_file.is_file():
        try:
            payload = _read_json(pool_file)
            items = payload.get("cookies") if isinstance(payload, dict) else None
            pool_cookies = len(items) if isinstance(items, list) else 0
            if isinstance(payload, dict):
                account_email = payload.get("account_email")
        except (OSError, ValueError):
            pool_cookies = 0

    return {
        "account": account,
        "pool": pool_name,
        "account_email": account_email,
        "user_data_dir": str(udd),
        "profile_exists": udd.is_dir(),
        "cookies_in_db": cookie_count,
        "has_1psid": has_psid,
        "pool_file": str(pool_file),
        "pool_exists": pool_file.is_file(),
        "pool_cookies": pool_cookies,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="cookie_sync",
        description="Tự động lấy/cập nhật cookie Gemini từ Chrome riêng của orchestrator.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_adopt = sub.add_parser(
        "adopt", help="cấy cookie từ file export Cookie-Editor vào Chrome riêng (1 lần)"
    )
    p_adopt.add_argument("--account", required=True, help="tên account (thư mục Chrome riêng)")
    p_adopt.add_argument("--export", required=True, type=Path, help="file JSON export")
    p_adopt.add_argument("--pool", default=None, help="tên pool trong pools/")
    p_adopt.add_argument("--email", default=None, help="email tài khoản")

    p_sync = sub.add_parser("sync", help="đọc cookie live từ Chrome riêng và ghi pools/")
    p_sync.add_argument("--account", required=True)
    p_sync.add_argument("--pool", default=None)
    p_sync.add_argument("--email", default=None)

    p_status = sub.add_parser("status", help="xem trạng thái Chrome riêng + pool")
    p_status.add_argument("--account", required=True)
    p_status.add_argument("--pool", default=None)

    p_login = sub.add_parser(
        "login",
        help="mở Chrome riêng để đăng nhập Google (thay cho Cookie-Editor)",
    )
    p_login.add_argument("--account", required=True)

    args = ap.parse_args(argv)

    try:
        if args.cmd == "adopt":
            out = adopt(
                args.account,
                args.export,
                pool_name=args.pool,
                account_email=args.email,
            )
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if out.get("signed_in") else 1
        if args.cmd == "sync":
            out = sync(args.account, pool_name=args.pool, account_email=args.email)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if out.get("ok") else 1
        if args.cmd == "status":
            print(
                json.dumps(
                    status(args.account, pool_name=args.pool),
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if args.cmd == "login":
            out = login(args.account)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
    except Exception as e:
        print(f"LỖI: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
