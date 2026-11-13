"""CDP client tối giảu — không phụ thuộc Playwright.

Vì sao không dùng Playwright cho `cookie_sync`:
    Playwright kéo theo driver Node (~109 MB) → bản `.exe` phình to gấp đôi.
    Nhưng `cookie_sync` chỉ cần 4 lệnh CDP:

        - `Storage.getCookies`        (đọc cookie mọi domain)
        - `Network.setCookie`         (cấy cookie vào profile)
        - `Page.navigate`             (mở Gemini để kiểm tra đăng nhập)
        - `Runtime.evaluate`          (đọc text trang)

    → tự implement bằng `websocket-client` (thuần Python, ~80 KB).

Lưu ý Chrome ≥136 chặn `--remote-debugging-port` khi `--user-data-dir` trỏ
đúng thư mục default. Orchestrator dùng user-data-dir **riêng** nên không bị
chặn (xem `cookie_sync.py`).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class CDPError(RuntimeError):
    """Lỗi khi gọi CDP method."""


class CDPClient:
    """Client CDP tối giểu, đủ dùng cho đọc/cấy cookie."""

    def __init__(self, port: int, *, host: str = "127.0.0.1", timeout: float = 30.0) -> None:
        self.port = port
        self.host = host
        self.timeout = timeout
        self._id = 0
        self._ws: Any = None

    # -- Kết nối ----------------------------------------------------------

    def _http_json(self, path: str) -> Any:
        url = f"http://{self.host}:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def wait_until_ready(self, timeout: float = 60.0) -> None:
        """Chờ Chrome mở cổng debug."""
        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                self._http_json("/json/version")
                return
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
                time.sleep(0.5)
        raise CDPError(f"Chrome không mở cổng debug {self.port}: {last}")

    def connect(self) -> None:
        """Mở WebSocket tới browser-level endpoint."""
        import websocket  # websocket-client

        info = self._http_json("/json/version")
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CDPError("Chrome không trả webSocketDebuggerUrl")
        self._ws = websocket.create_connection(
            ws_url,
            timeout=self.timeout,
            suppress_origin=True,
            max_size=None,
        )

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self) -> "CDPClient":
        self.wait_until_ready()
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Gọi method -------------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None) -> dict[str, Any]:
        """Gọi 1 CDP method, chờ kết quả."""
        if self._ws is None:
            raise CDPError("Chưa connect.")
        self._id += 1
        msg_id = self._id
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self._ws.send(json.dumps(payload))

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            raw = self._ws.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != msg_id:
                continue  # event hoặc response của request khác
            if "error" in msg:
                err = msg["error"]
                raise CDPError(f"{method}: {err.get('message', err)}")
            return msg.get("result") or {}
        raise CDPError(f"{method}: timeout")

    # -- API tiện ích -----------------------------------------------------

    def get_cookies(self) -> dict[str, str]:
        """Đọc cookie mọi domain → `{name: value}`."""
        res = self.call("Storage.getCookies")
        out: dict[str, str] = {}
        for c in res.get("cookies") or []:
            name, value = c.get("name"), c.get("value")
            if isinstance(name, str) and isinstance(value, str):
                out.setdefault(name, value)
        return out

    def set_cookie(
        self,
        name: str,
        value: str,
        *,
        session_id: str,
        domain: str = ".google.com",
        path: str = "/",
        secure: bool = True,
        http_only: bool = True,
        expires: int | None = None,
    ) -> None:
        """Cấy 1 cookie vào profile.

        `Network.setCookie` thuộc page domain → **bắt buộc** phải có
        `session_id` (attach vào target). Gọi ở browser-level sẽ báo lỗi.
        """
        params: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": secure,
            "httpOnly": http_only,
        }
        if expires is not None:
            params["expires"] = expires
        self.call("Network.setCookie", params, session_id=session_id)

    def open_page(self, url: str) -> str:
        """Tạo tab mới, điều hướng tới url, trả về target id."""
        res = self.call("Target.createTarget", {"url": "about:blank"})
        target_id = res.get("targetId")
        if not target_id:
            raise CDPError("Không tạo được target.")
        return str(target_id)

    def attach(self, target_id: str) -> str:
        """Attach vào target → trả sessionId."""
        res = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = res.get("sessionId")
        if not sid:
            raise CDPError("Không attach được target.")
        return str(sid)

    def navigate(self, session_id: str, url: str, *, wait_ms: int = 7000) -> str:
        """Điều hướng và trả về text trang (dùng để kiểm tra đăng nhập)."""
        self.call("Page.enable", {}, session_id=session_id)
        self.call("Page.navigate", {"url": url}, session_id=session_id)
        time.sleep(wait_ms / 1000)
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": "document.body ? document.body.innerText.slice(0, 600) : ''",
                "returnByValue": True,
            },
            session_id=session_id,
        )
        value = (res.get("result") or {}).get("value")
        return value if isinstance(value, str) else ""

    def close_browser(self) -> None:
        """Yêu cầu Chrome đóng graceful (để flush cookie vào DB)."""
        try:
            self.call("Browser.close")
        except Exception:
            pass
