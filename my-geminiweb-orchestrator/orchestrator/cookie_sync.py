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

#: Thư mục gốc dữ liệu — dùng chung với mọi module (xem `orchestrator.paths`).
#:
#: TRƯỚC ĐÂY module này tự tìm `pools/` riêng (`_resolve_pools_dir()` chỉ check
#: `cwd`), khác với `accounts._data_root()` và `tools/gemini_pool._resolve_dir()`
#: (walk up cây thư mục). Hệ quả: chạy `dist/orchestrator-gui/orchestrator-gui.exe`
#: thì GUI ghi cookie vào `%LOCALAPPDATA%\\Orchestrator\\pools\\` còn dashboard đọc
#: `<repo>\\pools\\` → split-brain, bảng hiện UNAVAILABLE dù popup báo thành công.
#: GIỜ: mọi module dùng chung `orchestrator.paths` → không còn split-brain.
from orchestrator import paths  # noqa: E402

POOLS_DIR = paths.POOLS_DIR

REQUIRED = "__Secure-1PSID"
OPTIONAL = "__Secure-1PSIDTS"

#: `account_status` hợp lệ (session dùng được) — xem `gemini_webapi.constants.
#: AccountStatus`. `AVAILABLE` (1000) là trạng thái **thành công**; các giá trị
#: khác nghĩa là session bị chặn / hết hạn / chưa xác thực.
_OK_ACCOUNT_STATUSES = frozenset(
    {
        "AVAILABLE",
        "ACTIVE",  # tương thích ngược
    }
)

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

    def _kill_stale_instances(self) -> int:
        """Giết Chrome cũ đang giữ user-data-dir này.

        Chrome cho phép **một** tiến trình duy nhất dùng 1 user-data-dir. Nếu
        còn instance cũ (do lần chạy trước bị kill cứng khi exception), Chrome
        mới sẽ nhận lệnh "mở tab trong instance cũ" rồi thoát → cổng debug
        không bao giờ mở → `CDPError: Chrome không mở cổng debug`.

        Chỉ kill tiến trình Chrome **của orchestrator** (đúng user-data-dir),
        không đụng Chrome cá nhân của user.
        """
        target = str(self.user_data_dir).lower()
        killed = 0

        try:
            import subprocess as _sp

            # Dùng PowerShell + CIM để lấy CommandLine (wmic đã bị bỏ).
            # `-like` dùng wildcard, nên backslash KHÔNG cần escape.
            script = (
                "$t = '" + target.replace("'", "''") + "';"
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Where-Object { "
                "  $_.CommandLine -and "
                "  $_.CommandLine.ToLower().Contains($t) -and "
                "  $_.CommandLine -notlike '*--type=*' "
                "} | ForEach-Object { "
                "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
                "  $_.ProcessId "
                "}"
            )
            out = _sp.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            killed = len([ln for ln in (out.stdout or "").splitlines() if ln.strip()])
        except Exception:
            killed = 0

        if killed:
            # Đợi Chrome giải phóng user-data-dir.
            import time as _time

            _time.sleep(2.5)

        return killed

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

        # Chrome không cho 2 tiến trình dùng chung 1 user-data-dir: nếu còn
        # instance cũ (do lần chạy trước bị kill cứng), Chrome mới sẽ thoát
        # ngay và không mở cổng debug. → Dọn instance cũ rồi thử lại.
        self._kill_stale_instances()

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
        try:
            self._cdp.wait_until_ready()
            self._cdp.connect()
        except Exception:
            # Có thể instance cũ vẫn giữ user-data-dir → thử dọn rồi spawn lại.
            self._close_browser_process()
            self._kill_stale_instances()
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._cdp = cdp.CDPClient(self.port)
            self._cdp.wait_until_ready()
            self._cdp.connect()
        # Ghi port để lần sau nhận ra instance đang chạy.
        (self.user_data_dir / "orch-cdp-port.txt").write_text(
            str(self.port), encoding="utf-8"
        )

    def _close_browser_process(self) -> None:
        """Đóng tiến trình Chrome ta vừa spawn (nếu còn)."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        except Exception:
            pass
        self._proc = None

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


#: Thư mục lưu **bản sao JSON cookie** mà user đã nhập, để không mất khi
#: pool file bị ghi đè bởi `sync()`. Đường dẫn: `<data root>/cookie_files/`.
#:
#: - `cookie_files/<pool>.json`      → bản mới nhất (đè lại)
#: - `cookie_files/history/<pool>-<timestamp>.json` → lịch sử, giữ 20 bản
COOKIE_ARCHIVE_DIRNAME = "cookie_files"

#: Số bản lịch sử cookie giữ lại cho mỗi pool.
COOKIE_HISTORY_KEEP = 20


def cookie_archive_dir() -> Path:
    """Thư mục lưu bản sao JSON cookie."""
    return paths.COOKIE_ARCHIVE_DIR


def save_cookie_archive(
    account: str,
    raw_text: str,
    *,
    pool_name: str | None = None,
    account_email: str | None = None,
) -> Path:
    """Lưu bản sao JSON cookie người dùng vừa nhập vào đĩa.

    Mục đích: pool file (`pools/<pool>.json`) bị `sync()` ghi đè mỗi lần với
    cookie live. Bản gốc user paste vào (lúc nhập acc) là thứ **không thể
    tái tạo** — nếu session chết mà pool file đã bị ghi đè thì mất trắng.

    Args:
        account: tên account.
        raw_text: nội dung JSON cookie gốc.
        pool_name: tên pool (mặc định: pool của account).

    Returns:
        Path của file mới nhất đã ghi.
    """
    name = pool_name or default_pool_for(account)
    root = cookie_archive_dir()
    root.mkdir(parents=True, exist_ok=True)

    try:
        payload = json.loads(raw_text)
    except ValueError:
        # Không parse được → vẫn lưu nguyên bản text để không mất dữ liệu.
        payload = {"_raw_text": raw_text}

    envelope = {
        "name": name,
        "profile": account,
        "account_email": account_email,
        "source": "orchestrator-paste-cookie",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cookies": payload,
    }
    latest = root / f"{name}.json"
    _write_json_atomic(latest, envelope)

    # Lịch sử: xoay vòng, giữ tối đa COOKIE_HISTORY_KEEP bản.
    hist = root / "history"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    _write_json_atomic(hist / f"{name}-{stamp}.json", envelope)
    olds = sorted(hist.glob(f"{name}-*.json"))
    for old in olds[:-COOKIE_HISTORY_KEEP]:
        old.unlink(missing_ok=True)

    return latest


def list_cookie_archives() -> list[dict[str, Any]]:
    """Liệt kê các bản cookie đã lưu (mới nhất trước)."""
    root = cookie_archive_dir()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(root.glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            payload = _read_json(f)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        out.append(
            {
                "file": str(f),
                "name": payload.get("name"),
                "profile": payload.get("profile"),
                "saved_at": payload.get("saved_at"),
            }
        )
    return out


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
        # CHỈ 2 cookie xác thực — xem `_cookie_list()`.
        "cookies": _cookie_list(live),
    }
    _write_json_atomic(POOLS_DIR / f"{name}.json", payload)
    set_account_pool(account, name)

    # Lưu bản gốc để không mất khi `sync()` ghi đè pool file.
    archive = save_cookie_archive(
        account,
        json.dumps(
            [{"name": n, "value": cookies[n], "domain": ".google.com"} for n in cookies],
            ensure_ascii=False,
        ),
        pool_name=name,
        account_email=account_email,
    )

    # Xoá cache cookie cũ — nếu không, nó che cookie mới vừa cấy.
    purge_gemini_cache(live.get(REQUIRED))

    return {
        "ok": True,
        "account": account,
        "user_data_dir": str(udd),
        "cookies_seeded": added,
        "cookies_live": len(live),
        "signed_in": signed_in,
        "pool_file": str(POOLS_DIR / f"{name}.json"),
        "cookie_archive": str(archive),
        "page_preview": page_text.replace("\n", " ")[:200],
    }


def _cookie_list(cookies: dict[str, str]) -> list[dict[str, str]]:
    """Chuyển dict cookie → list shape của pool file.

    CHỈ giữ **2 cookie xác thực** (`__Secure-1PSID`, `__Secure-1PSIDTS`).

    Vì sao KHÔNG lưu toàn bộ cookie (đã verify bằng script thật, 2026-11-14):

    | tập cookie                      | kết quả `account_status` |
    |---------------------------------|--------------------------|
    | 2 cookie (`1PSID` + `1PSIDTS`) | **AVAILABLE** ✅           |
    | 35 cookie (toàn bộ profile)     | UNAUTHENTICATED ❌        |
    | 31 cookie (bỏ `__Host-*`)       | UNAUTHENTICATED ❌        |

    Lý do: `gemini_webapi` chỉ gửi `1PSID` + `1PSIDTS` lên server
    (`client.py:192-197`, `tools/gemini_pool.py::Pool.new_client`). Nhưng
    `get_access_token.py` dựng `Cookies` jar từ **toàn bộ** dict ta đưa vào
    → các cookie khác (`SID`, `HSID`, `APISID`, `__Host-*`...) đi kèm và
    request bị Google từ chối → `UNAUTHENTICATED`.

    Cookie đầy đủ vẫn được giữ trong **profile Chrome** (đó là nguồn sự thật)
    và trong bản archive `cookie_files/`; pool file chỉ cần 2 cookie auth.
    """
    return [
        {"name": n, "value": cookies[n], "domain": ".google.com"}
        for n in (REQUIRED, OPTIONAL)
        if cookies.get(n)
    ]


def _parse_cookie_json(raw_text: str) -> dict[str, str]:
    """Parse JSON cookie **dán trực tiếp** từ clipboard.

    Chấp nhận mọi shape mà người dùng có thể copy:
      - Cookie-Editor export:  `[{"domain":..., "name":..., "value":...}, ...]`
      - Pool file:             `{"cookies": [{"name":..., "value":...}, ...]}`
      - Map phẳng:            `{"__Secure-1PSID": "...", ...}`
      - Có bọc trong thuộc tính lạ: `{"data": [...]}` / `{"items": [...]}`

    Raises:
        ValueError: JSON hỏng hoặc không tìm thấy cookie nào.
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Chưa có nội dung JSON.")

    try:
        raw = json.loads(text)
    except ValueError as e:
        # Gợi ý cụ thể vì lỗi phổ biến nhất là copy kèm text thừa.
        raise ValueError(f"JSON không hợp lệ: {e}") from e

    # Bỏ bọc 1-2 lớp nếu có key chứa list/dict.
    if isinstance(raw, dict):
        for key in ("data", "items", "cookies", "result"):
            inner = raw.get(key)
            if isinstance(inner, (list, dict)):
                raw = inner
                break

    cookies = extract_cookies(raw)
    if not cookies:
        raise ValueError(
            "Không tìm thấy cookie nào trong JSON. Cần ít nhất "
            f"'{REQUIRED}' (export từ tab gemini.google.com đã đăng nhập)."
        )
    if REQUIRED not in cookies:
        raise ValueError(
            f"JSON không có cookie '{REQUIRED}'. "
            "Hãy export từ tab gemini.google.com đã đăng nhập."
        )
    return cookies


def adopt_json(
    account: str,
    raw_text: str,
    *,
    pool_name: str | None = None,
    account_email: str | None = None,
    port: int | None = None,
    verify_login: bool = True,
) -> dict[str, Any]:
    """Cấy cookie từ **JSON dán trực tiếp** (không cần file tạm).

    Đây là biến thể "paste" của `adopt()` — cùng hệt logic, chỉ khác nguồn
    input: chuỗi JSON thay vì đường dẫn file. GUI dùng hàm này khi user
    bấm "Nhập cookie" → popup textarea → paste → lưu.

    Nếu Chrome riêng của account đã tồn tại, ta **không xoá** profile (khác
    `adopt()`): profile đó có thể đang giữ DBSC key của session cũ. Xoá
    profile sẽ phá session đang hoạt động.

    Args:
        account: tên account (thư mục Chrome riêng).
        raw_text: nội dung JSON cookie.
        pool_name: tên pool để ghi (mặc định: pool của account).
        account_email: email để ghi vào pool file.
        port: cổng debug (mặc định: tự chọn).
        verify_login: mở Gemini để xác nhận đã đăng nhập.

    Returns:
        dict kết quả — cùng shape với `adopt()`.
    """
    cookies = _parse_cookie_json(raw_text)

    udd = account_dir(account)
    udd.mkdir(parents=True, exist_ok=True)

    port = port or _find_free_port(_account_port_base(account))
    # Profile đã tồn tại → dùng lại (không xoá) để giữ DBSC key.
    reuse = udd.is_dir() and any(udd.iterdir())
    with ChromeSession(udd, port=port, reuse_existing=reuse) as s:
        added = s.set_cookies(cookies)
        live = s.cookies()
        page_text = s.goto(GEMINI_URL) if verify_login else ""

    signed_in = REQUIRED in live and not _looks_signed_out(page_text)

    name = pool_name or default_pool_for(account)
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
        # CHỈ 2 cookie xác thực — xem `_cookie_list()`.
        "cookies": _cookie_list(live),
    }
    _write_json_atomic(POOLS_DIR / f"{name}.json", payload)
    set_account_pool(account, name)

    # Lưu bản gốc user paste (không thể tái tạo từ pool file).
    archive = save_cookie_archive(
        account, raw_text, pool_name=name, account_email=account_email
    )

    # Xoá cache cookie cũ — nếu không, nó che cookie mới vừa cấy.
    purge_gemini_cache(live.get(REQUIRED))

    return {
        "ok": True,
        "account": account,
        "user_data_dir": str(udd),
        "cookies_seeded": added,
        "cookies_live": len(live),
        "signed_in": signed_in,
        "pool_file": str(POOLS_DIR / f"{name}.json"),
        "cookie_archive": str(archive),
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
            # CHỈ lưu 2 cookie xác thực — xem `_cookie_list()` để biết vì sao
            # lưu toàn bộ cookie làm session bị `UNAUTHENTICATED`.
            #
            # Bẫy đã gặp (2026-11-14): trước đây lưu 35 cookie → mọi pool báo
            # `UNAVAILABLE` dù session thật sự còn sống. Verify:
            #   2 cookie  → AVAILABLE
            #   35 cookie → UNAUTHENTICATED
            "cookies": _cookie_list(live),
        }
    )
    _write_json_atomic(pool_file, payload)
    set_account_pool(account, name)

    # Xoá cache cookie cũ của `gemini_webapi` — nó che (shadow) `1PSIDTS` mới
    # và làm session báo `UNAUTHENTICATED` giả. Xem `purge_gemini_cache()`.
    purged = purge_gemini_cache(live.get(REQUIRED))

    # Kiểm tra session thật (không chỉ cookie còn sống).
    #
    # Bẫy đã gặp: cookie vẫn còn (21 cookie) nhưng Google đã hết hạn session
    # → `gemini_webapi` trả `UNAUTHENTICATED`, mọi Deep Research fail với
    # "Permission denied". Chỉ đọc cookie là không đủ.
    session = _probe_session(_auth_cookies(live))

    return {
        "ok": True,
        "account": account,
        "cookies": len(live),
        "cookies_saved": len(payload["cookies"]),
        "pool_file": str(pool_file),
        "updated_at": payload["updated_at"],
        "session_ok": session.get("ok"),
        "account_status": session.get("account_status"),
        "cache_purged": len(purged),
        "warning": session.get("warning"),
    }


def _auth_cookies(cookies: dict[str, str]) -> dict[str, str]:
    """Chỉ giữ 2 cookie xác thực từ tập cookie live.

    `gemini_webapi` chỉ dùng `1PSID` + `1PSIDTS`. Truyền thêm cookie khác
    làm request bị Google từ chối → `UNAUTHENTICATED` (đã verify).
    """
    return {n: cookies[n] for n in (REQUIRED, OPTIONAL) if cookies.get(n)}


def _probe_session(cookies: dict[str, str]) -> dict[str, Any]:
    """Kiểm tra session Gemini còn sống không (gọi API thật).

    Trả về `{"ok": bool, "account_status": str|None, "warning": str|None}`.
    Không raise — lỗi mạng chỉ là `ok=False` + warning.

    **Tự xoá cache cookie cũ** của `gemini_webapi` trước khi probe — nếu không,
    cache sẽ che `1PSIDTS` mới và probe luôn trả `UNAUTHENTICATED` giả.
    """
    if REQUIRED not in cookies:
        return {"ok": False, "account_status": None, "warning": "thiếu cookie 1PSID"}

    # Xoá cache cũ (nếu có) để probe dùng đúng cookie hiện tại.
    purge_gemini_cache(cookies[REQUIRED])

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

    ok = status in _OK_ACCOUNT_STATUSES
    warning = None
    if not ok:
        warning = (
            f"Session KHÔNG còn hiệu lực (account_status={status}). "
            "Cookie vẫn còn nhưng Google đã hết hạn session → mọi Deep Research "
            "sẽ fail với 'Permission denied'. Cần đăng nhập lại: với tài khoản "
            "loại 2 dùng `account login` (tự động), với loại 1 dùng `cookie login`."
        )
    return {"ok": ok, "account_status": status, "warning": warning}


# ---------------------------------------------------------------------------
# Cache cookie của gemini_webapi (nguồn gây "session chết giả")
# ---------------------------------------------------------------------------

#: `gemini_webapi` cache cookie tại `%TEMP%\gemini_webapi\.cached_cookies_<1PSID>.json`.
#:
#: Cơ chế `get_access_token.py` (Phase 1):
#:   1. Nạp jar từ cache → `Cookies(base_jar)` rồi `set()` cookie trong cache
#:      → ghi **đè** `1PSIDTS` mới trong pool file.
#:   2. Thử nhóm "Cache" **trước** nhóm "Base Cookies".
#:
#: → Nếu cache còn `1PSIDTS` cũ (đã hết hạn), nó **che (shadow)** cookie mới
#: và mọi request dùng session cũ → `UNAUTHENTICATED`, dù pool file đúng.
#:
#: Verify (2026-11-14, script `reports/_shadow_test.py`):
#:   cache có 1PSIDTS cũ  → UNAUTHENTICATED
#:   xoá cache            → AVAILABLE
def _gemini_cache_dir() -> Path:
    """Thư mục cache cookie của `gemini_webapi`."""
    env = os.environ.get("GEMINI_COOKIE_PATH")
    if env:
        return Path(env)
    import tempfile

    return Path(tempfile.gettempdir()) / "gemini_webapi"


def purge_gemini_cache(psid: str | None = None) -> list[str]:
    """Xoá cache cookie của `gemini_webapi` (bị lỗi thời che session mới).

    Args:
        psid: chỉ xoá cache của 1 session cụ thể. `None` → xoá tất cả.

    Returns:
        Danh sách file đã xoá.
    """
    d = _gemini_cache_dir()
    if not d.is_dir():
        return []
    pattern = f".cached_cookies_{psid}.json" if psid else ".cached_cookies_*.json"
    removed: list[str] = []
    for f in d.glob(pattern):
        try:
            f.unlink()
            removed.append(str(f))
        except OSError:
            pass
    return removed


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
