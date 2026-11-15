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
import random
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

    #: Domain ưu tiên khi cùng tên cookie tồn tại ở nhiều domain.
    #:
    #: Cookie `__Secure-1PSIDTS` của Google được set cho `.google.com` VÀ
    #: `.youtube.com`. Bản `.youtube.com` thường **cũ hơn** (không được
    #: rotate khi bạn vào Gemini). `Storage.getCookies` trả về theo thứ tự
    #: không bảo đảm → `setdefault` (first-wins) có thể lấy nhầm bản stale →
    #: server trả `UNAUTHENTICATED` (1016) dù session thật vẫn sống.
    #:
    #: Verify (2026-11-14, account `kimperiod0126`):
    #:   `.youtube.com` → `sidts-CjQBnJZzaqMYoVZ...`  (stale  → 1016)
    #:   `.google.com`  → `sidts-CjYBnJZzaq6z_iZn...` (fresh  → 1000 AVAILABLE)
    _COOKIE_DOMAIN_PRIORITY = (
        ".google.com",
        ".google.com.vn",
        ".google.com.tr",
        ".youtube.com",
        "",
    )

    def get_cookies(self, domain: str | None = None) -> dict[str, str]:
        """Đọc cookie mọi domain → `{name: value}`.

        Args:
            domain: nếu cho, chỉ lấy cookie của domain này (vd `.google.com`).

        Ghi chú: cùng 1 tên cookie có thể tồn tại ở nhiều domain. Ta **ưu tiên
        domain Google** vì bản `.youtube.com` của `__Secure-1PSIDTS` hay cũ hơn
        (xem `_COOKIE_DOMAIN_PRIORITY`).
        """
        res = self.call("Storage.getCookies")
        buckets: dict[str, list[tuple[int, str]]] = {}
        for c in res.get("cookies") or []:
            name, value = c.get("name"), c.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            dom = (c.get("domain") or "").lstrip(".")
            if domain:
                if dom != domain.lstrip("."):
                    continue
                buckets.setdefault(name, []).append((0, value))
                continue
            prio = self._domain_priority(dom)
            buckets.setdefault(name, []).append((prio, value))

        out: dict[str, str] = {}
        for name, vals in buckets.items():
            vals.sort(key=lambda t: t[0])
            out[name] = vals[0][1]
        return out

    @classmethod
    def _domain_priority(cls, domain: str) -> int:
        """Số thứ tự ưu tiên của domain (nhỏ hơn = ưu tiên cao hơn)."""
        if not domain:
            return len(cls._COOKIE_DOMAIN_PRIORITY)
        for i, d in enumerate(cls._COOKIE_DOMAIN_PRIORITY):
            if domain == d.lstrip("."):
                return i
        return len(cls._COOKIE_DOMAIN_PRIORITY)

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
        return self.page_text(session_id)

    # -- Điền form (dùng cho đăng nhập Google tự động) --------------------

    def evaluate(
        self, session_id: str, expression: str, *, await_promise: bool = False
    ) -> Any:
        """Chạy JS trong trang và trả về giá trị (mặc định `returnByValue=True`)."""
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            session_id=session_id,
        )
        if res.get("exceptionDetails"):
            raise CDPError(
                f"JS lỗi: {res['exceptionDetails'].get('text')} "
                f"{res['exceptionDetails'].get('exception', {}).get('description', '')}"
            )
        return (res.get("result") or {}).get("value")

    def page_text(self, session_id: str, limit: int = 2000) -> str:
        """Text hiển thị của trang (để detect trạng thái đăng nhập)."""
        value = self.evaluate(
            session_id,
            f"document.body ? document.body.innerText.slice(0, {int(limit)}) : ''",
        )
        return value if isinstance(value, str) else ""

    def current_url(self, session_id: str) -> str:
        """URL hiện tại của tab."""
        value = self.evaluate(session_id, "location.href")
        return value if isinstance(value, str) else ""

    def wait_for(
        self,
        session_id: str,
        expression: str,
        *,
        timeout: float = 20.0,
        interval: float = 0.5,
    ) -> Any:
        """Chờ tới khi biểu thức JS trả về giá trị truthy.

        Raises:
            CDPError: hết thời gian mà biểu thức vẫn falsy.
        """
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            try:
                last = self.evaluate(session_id, expression)
            except CDPError:
                last = None
            if last:
                return last
            time.sleep(interval)
        raise CDPError(f"wait_for timeout sau {timeout}s (biểu thức: {expression[:80]})")

    def focus_selector(self, session_id: str, selector: str) -> bool:
        """Focus element theo selector (dùng `DOM.*` để chính xác)."""
        try:
            doc = self.call("DOM.getDocument", {"depth": 0}, session_id=session_id)
            root = (doc.get("root") or {}).get("nodeId")
            if not root:
                return False
            res = self.call(
                "DOM.querySelector",
                {"nodeId": root, "selector": selector},
                session_id=session_id,
            )
            node_id = res.get("nodeId")
            if not node_id:
                return False
            self.call("DOM.focus", {"nodeId": node_id}, session_id=session_id)
            return True
        except CDPError:
            # Fallback: focus bằng JS
            try:
                res = self.evaluate(
                    session_id,
                    f"(() => {{ const e = document.querySelector({selector!r}); "
                    "if (!e) return false; e.focus(); return true; })()",
                )
                return bool(res)
            except CDPError:
                return False

    def type_text(self, session_id: str, selector: str, text: str) -> bool:
        """Gõ text vào ô nhập (nhanh, không jitter).

        Dùng `DOM.focus` → `Input.insertText`. Gán `.value` bằng JS không đủ
        vì React/Google đọc event `input`, mà `insertText` sinh đúng event đó.
        """
        if not self.focus_selector(session_id, selector):
            return False
        try:
            self.call("Input.insertText", {"text": text}, session_id=session_id)
            return True
        except CDPError:
            # Fallback: set value + dispatch event
            try:
                payload = json.dumps(text)
                self.evaluate(
                    session_id,
                    f"(() => {{ const e = document.querySelector({selector!r}); "
                    f"if (!e) return false; e.value = {payload}; "
                    "e.dispatchEvent(new Event('input', {bubbles: true})); "
                    "e.dispatchEvent(new Event('change', {bubbles: true})); "
                    "return true; })()",
                )
                return True
            except CDPError:
                return False

    def type_text_human(
        self,
        session_id: str,
        selector: str,
        text: str,
        *,
        min_delay: float = 0.04,
        max_delay: float = 0.11,
    ) -> bool:
        """Gõ text **từng ký tự** kèm jitter ngẫu nhiên.

        Vì sao không gõ 1 lần: Google dùng Botguard để phân tích nhịp gõ.
        `Input.insertText` sinh event `isTrusted: true` (nên không bị flag như
        gán `.value` bằng JS), nhưng gõ tức thời 1 lần vẫn giống bot.
        Gõ từng ký tự với jitter 40-120 ms là mức tối thiểu để không bị đánh
        dấu.

        Vẫn **không** bypass CAPTCHA — chỉ điền form.
        """
        if not self.focus_selector(session_id, selector):
            return False
        try:
            for ch in text:
                self.call("Input.insertText", {"text": ch}, session_id=session_id)
                time.sleep(random.uniform(min_delay, max_delay))
            return True
        except CDPError:
            return False

    def rect_center(self, session_id: str, selector: str) -> tuple[float, float] | None:
        """Tâm của element (theo `getBoundingClientRect`), `None` nếu không có."""
        try:
            res = self.evaluate(
                session_id,
                f"(() => {{ const e = document.querySelector({selector!r}); "
                "if (!e) return null; const r = e.getBoundingClientRect(); "
                "if (!r || (r.width === 0 && r.height === 0)) return null; "
                "return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()",
            )
        except CDPError:
            return None
        if not isinstance(res, dict):
            return None
        x, y = res.get("x"), res.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None
        return float(x), float(y)

    def click_at(self, session_id: str, x: float, y: float) -> None:
        """Click chuột tại toạ độ (sinh event `isTrusted: true`)."""
        try:
            self.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y},
                session_id=session_id,
            )
            time.sleep(random.uniform(0.03, 0.08))
            self.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
                session_id=session_id,
            )
            time.sleep(random.uniform(0.04, 0.09))
            self.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                },
                session_id=session_id,
            )
        except CDPError:
            pass

    def press_enter(self, session_id: str) -> None:
        """Nhấn Enter (submit form).

        Chuỗi 3 sự kiện đúng chuẩn Blink: `rawKeyDown` (kèm `text`) →
        `char` → `keyUp`. Thiếu `char` thì form không submit.
        """
        for ev_type, extra in (
            ("rawKeyDown", {"text": "\r"}),
            ("char", {"text": "\r", "unmodifiedText": "\r"}),
            ("keyUp", {}),
        ):
            params: dict[str, Any] = {
                "type": ev_type,
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            }
            params.update(extra)
            try:
                self.call("Input.dispatchKeyEvent", params, session_id=session_id)
            except CDPError:
                pass
            if ev_type == "rawKeyDown":
                time.sleep(random.uniform(0.05, 0.1))

    def click_selector(self, session_id: str, selector: str) -> bool:
        """Click element theo selector.

        Dùng `getBoundingClientRect` + `Input.dispatchMouseEvent` thay vì
        `element.click()` bằng JS — sự kiện chuột do CDP sinh có
        `isTrusted: true`, còn `.click()` bằng JS thì không.
        """
        center = self.rect_center(session_id, selector)
        if center is None:
            return False
        self.click_at(session_id, center[0], center[1])
        return True

    def close_browser(self) -> None:
        """Yêu cầu Chrome đóng graceful (để flush cookie vào DB)."""
        try:
            self.call("Browser.close")
        except Exception:
            pass
