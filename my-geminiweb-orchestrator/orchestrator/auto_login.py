"""Đăng nhập Google **tự động** bằng email + password (tài khoản loại 2).

Bối cảnh
---------
Hai loại tài khoản Gemini:

- **Loại 1 — có xác thực (2FA / passkey / CAPTCHA)**: user đăng nhập tay 1 lần
  trong Chrome riêng của orchestrator → `cookie_sync.login()`. Sau đó cookie
  được sync tự động mãi.
- **Loại 2 — chỉ email + password**: user chỉ muốn nhập email + mật khẩu, hệ
  thống tự điền form và lấy cookie, **không cần mở Chrome thủ công**.

Module này loại 2.

Nguyên tắc
----------
1. **Không bypass CAPTCHA / 2FA.** Nếu gặp challenge → dừng lại, trả về trạng
   thái `needs_human` để user xử lý tay trong cửa sổ Chrome đang mở.
2. **Mỗi account 1 profile Chrome riêng vĩnh viễn** — Google Device Bound
   Session (DBSC, Chrome ≥127) bind session vào user-data-dir qua khoá phần
   cứng TPM, nên copy cookie sang profile khác sẽ chết.
   Nguồn: <https://w3c.github.io/webappsec-dbsc/> và
   <https://blog.google/products/chrome/protecting-users-from-cookie-theft/>
3. **Mật khẩu không bao giờ được ghi ra log hay file plaintext** — lấy từ
   `orchestrator.credentials` (DPAPI).
4. **Locale `hl=en`** — Google dùng Polymer/Lit, DOM đổi theo locale. Ép
   tiếng Anh giúp selector & text detect ổn định.

Luồng
-----
```
1. Mở Chrome (profile riêng của account) → accounts.google.com/ServiceLogin?hl=en
2. Điền email → Next
3. Dò trạng thái bằng DOM (không chỉ text):
     - CAPTCHA   (img#captchaimg / iframe[src*=recaptcha])
     - BLOCKED   (URL chứa /rejected, hoặc heading "Couldn't sign you in")
     - 2FA       (URL chứa /signin/challenge/, heading "Verify it's you")
     - READY     (ô password xuất hiện)
4. Điền password → Next
5. Lấy cookie từ profile → ghi pool file → probe session
```

Trạng thái trả về
-----------------
```python
{"ok": bool, "state": "ok"|"needs_human"|"failed"|"blocked"|"unknown",
 "reason": str, "hint": str, "steps": [...]}
```
"""

from __future__ import annotations

import time
from typing import Any

from orchestrator import accounts as account_registry
from orchestrator import cookie_sync

#: URL đăng nhập. Ép `hl=en` để DOM ổn định (xem docstring module).
SIGNIN_URL = "https://accounts.google.com/ServiceLogin?hl=en"

#: Selector của form đăng nhập Google — thử lần lượt theo thứ tự.
#:
#: Google đổi id theo A/B test nên ta dùng nhiều selector dự phòng cho mỗi
#: field. Nguồn: SeleniumBase UC Mode, nodriver
#: (<https://github.com/seleniumbase/SeleniumBase>,
#:  <https://github.com/ultrafunkamsterdam/nodriver>)
SELECTORS = {
    "email": [
        "input#identifierId",
        "input[name='identifier']",
        "input[type='email']",
        "input[autocomplete='username']",
    ],
    "password": [
        "input[name='Passwd']",
        "input[type='password']",
        "input[autocomplete='current-password']",
    ],
    "next": [
        "#identifierNext button",
        "#passwordNext button",
        "#identifierNext",
        "#passwordNext",
        "button[jsname='LgbsSe']",
        "button[type='submit']",
    ],
}

#: Script dò trạng thái trang — chạy trong `Runtime.evaluate`.
#:
#: Dùng **DOM + URL**, không chỉ text, vì text thay đổi theo locale và A/B.
#: Trả về 1 trong: `PASSWORD_READY`, `CAPTCHA`, `BLOCKED`, `CHALLENGE`,
#: `ERROR:<msg>`, `WAITING`, `LOGGED_IN`.
STATE_SCRIPT = r"""
(() => {
  const url = location.href;
  if (url.includes('/rejected') || url.includes('deniedsigninrejected')) {
    return 'BLOCKED';
  }
  // CAPTCHA: phải kiểm tra **hiển thị**, không chỉ có trong DOM.
  // `img#captchaimg` và `input[name="ca"]` luôn nằm sẵn trong template của
  // Google (ẩn) → check bằng `!!document.querySelector` sẽ báo false-positive.
  const visible = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return false;
    if (e.offsetParent !== null) return true;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  if (visible('img#captchaimg')
      || visible('iframe[src*="recaptcha"]')
      || visible('#identifier-captcha-input')
      || visible('input[name="ca"]')
      || visible('div.g-recaptcha')) {
    return 'CAPTCHA';
  }
  // Lỗi hiển thị trong vùng assertive — kiểm tra TRƯỚC, vì khi sai mật khẩu
  // ô mật khẩu vẫn hiện (nên check PASSWORD_READY trước sẽ nuốt mất lỗi).
  // Lưu ý: vùng assertive cũng chứa heading ("Welcome", "Sign in") → chỉ lấy
  // text khác heading và khác text ngôn ngữ ("English (United States)").
  const headingEl = document.querySelector('#headingText');
  const h = headingEl ? headingEl.innerText : '';
  const errs = Array.from(document.querySelectorAll('div[aria-live="assertive"]'))
    .map(e => (e.innerText || '').trim())
    .filter(t => t && t !== h
                 && !/^(english|tiếng việt|français|deutsch)/i.test(t)
                 && !/^(welcome|chào mừng|sign in|đăng nhập)$/i.test(t));
  if (errs.length) {
    return 'ERROR:' + errs.join(' | ').slice(0, 200);
  }

  // Bước nhập mật khẩu. Lưu ý: URL của bước này cũng là
  // `/signin/challenge/pwd` — KHÔNG phải challenge 2FA (challenge thật là
  // `totp`, `ipp`, `oaz`, `kpe`...).
  const pwd = document.querySelector('input[name="Passwd"]')
           || document.querySelector('input[type="password"]');
  if (pwd && pwd.offsetParent !== null) return 'PASSWORD_READY';

  // 2FA / challenge thật.
  const isChallenge = /\/signin\/challenge\/(?!pwd\b)/.test(url);
  if (isChallenge) return 'CHALLENGE';
  if (/verify it's you|verify its you|2-step verification|two-step verification/i.test(h)) {
    return 'CHALLENGE';
  }
  if (/couldn't sign you in|could not sign you in/i.test(h)) return 'BLOCKED';
  if (h && /simplify your sign-in|confirm your recovery|protect your account/i.test(h)) {
    return 'CONSENT';
  }
  if (!url.includes('accounts.google.com/signin')
      && !url.includes('accounts.google.com/v3/signin')
      && !url.includes('accounts.google.com/ServiceLogin')) {
    return 'LOGGED_IN';
  }
  return 'WAITING';
})()
"""

#: Script bấm "Not now" / "Confirm" ở màn hình consent/passkey.
#:
#: Google chèn màn hình "Simplify your sign-in" (tạo passkey) và "Confirm your
#: recovery email" khi đăng nhập lần đầu trên profile mới. Bấm "Not now" là
#: hành động của chính user, không phải bypass.
CONSENT_SCRIPT = r"""
(() => {
  const wanted = ['Not now', 'Để sau', 'Skip', 'Bỏ qua'];
  for (const btn of document.querySelectorAll('button')) {
    const t = (btn.innerText || '').trim();
    if (wanted.some(w => t === w)) {
      btn.click();
      return t;
    }
  }
  return null;
})()
"""

#: Script accept Terms of Service của Gemini (màn hình "Lưu ý" + nút
#: "Tôi hiểu" / "I understand").
#:
#: Tài khoản MỚI (chưa từng dùng Gemini) bị chặn bằng `TOS_PENDING` cho tới
#: khi user bấm nút đồng ý. Đây là **hành động của chính user** (chấp nhận
#: điều khoản dịch vụ), không phải bypass.
#:
#: Verify (2026-11-14, account `reigin483`): sau khi bấm "Tôi hiểu" →
#: `account_status` chuyển từ `TOS_PENDING` sang `AVAILABLE`.
#:
#: Lưu ý: so khớp bằng **regex ASCII-safe** (`/^Tôi hiểu$/` vẫn phụ thuộc
#: Unicode normalization của chuỗi gửi qua CDP). Ta dùng `normalize('NFC')`
#: ở cả 2 vế + so khớp `textContent` (nút Material có thể đặt text ở child).
TOS_SCRIPT = r"""
(() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim()
                            .normalize('NFC');
  const re = /^(Tôi hiểu|Đã hiểu|I understand|Got it)$/u;
  const en = /^I understand$/;
  for (const btn of document.querySelectorAll('button, [role=button]')) {
    const t = norm(btn.innerText) || norm(btn.textContent);
    if (!t) continue;
    if (re.test(t) || en.test(t)) {
      btn.click();
      return t;
    }
  }
  return null;
})()
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _try_selectors(cdp: Any, session_id: str, key: str) -> str | None:
    """Trả về selector đầu tiên tồn tại **và hiển thị** trong trang."""
    for sel in SELECTORS[key]:
        try:
            found = cdp.evaluate(
                session_id,
                f"(() => {{ const e = document.querySelector({sel!r}); "
                "return !!(e && e.offsetParent !== null); })()",
            )
        except Exception:
            continue
        if found:
            return sel
    return None


def _wait_for_selector(
    cdp: Any,
    session_id: str,
    selectors: list[str],
    *,
    timeout: float = 30.0,
    poll: float = 0.6,
) -> str | None:
    """Chờ một trong các selector xuất hiện (và hiển thị).

    Trả về selector đã tìm thấy, hoặc `None` nếu hết thời gian.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                found = cdp.evaluate(
                    session_id,
                    f"(() => {{ const e = document.querySelector({sel!r}); "
                    "return !!(e && e.offsetParent !== null); })()",
                )
            except Exception:
                continue
            if found:
                return sel
        time.sleep(poll)
    return None


def _state(cdp: Any, session_id: str) -> str:
    """Đọc trạng thái trang hiện tại."""
    try:
        val = cdp.evaluate(session_id, STATE_SCRIPT)
    except Exception:
        return "WAITING"
    return val if isinstance(val, str) else "WAITING"


def _fill(
    cdp: Any,
    session_id: str,
    field_key: str,
    value: str,
    *,
    next_selectors: list[str],
) -> tuple[bool, str]:
    """Điền 1 field rồi bấm nút Next. Trả `(ok, detail)`."""
    sel = _try_selectors(cdp, session_id, field_key)
    if not sel:
        return False, f"Không tìm thấy ô nhập {field_key}"

    # Gõ từng ký tự kèm jitter — `Input.insertText` sinh event `isTrusted`
    # (nên không bị Botguard flag như gán `.value` bằng JS), nhưng gõ 1 lần
    # tức thời vẫn giống bot.
    if not cdp.type_text_human(session_id, sel, value):
        return False, f"Không gõ được vào {sel}"

    time.sleep(0.4)

    clicked = ""
    for ns in next_selectors:
        if cdp.click_selector(session_id, ns):
            clicked = ns
            break
    if not clicked:
        cdp.press_enter(session_id)
        return True, f"đã điền {field_key} ({sel}), submit bằng Enter"
    return True, f"đã điền {field_key} ({sel}), bấm {clicked}"


def _dismiss_consent(cdp: Any, session_id: str) -> str | None:
    """Bấm "Not now" nếu màn hình consent/passkey xuất hiện."""
    try:
        val = cdp.evaluate(session_id, CONSENT_SCRIPT)
        return val if isinstance(val, str) else None
    except Exception:
        return None


def _accept_tos(cdp: Any, session_id: str) -> str | None:
    """Bấm nút đồng ý Điều khoản ("Tôi hiểu" / "I understand") nếu xuất hiện.

    Tài khoản mới bị Gemini chặn với `TOS_PENDING` cho tới khi đồng ý điều
    khoản. Không có bước này thì account dù đăng nhập đúng vẫn không dùng
    được API.
    """
    try:
        val = cdp.evaluate(session_id, TOS_SCRIPT)
        return val if isinstance(val, str) else None
    except Exception:
        return None


def _wait_state(
    cdp: Any,
    session_id: str,
    wanted: set[str],
    *,
    timeout: float = 30.0,
    poll: float = 0.6,
) -> str:
    """Chờ trạng thái trang nằm trong `wanted`. Trả trạng thái cuối."""
    deadline = time.time() + timeout
    last = "WAITING"
    while time.time() < deadline:
        last = _state(cdp, session_id)
        if last in wanted:
            return last
        # Trong lúc chờ, dọn màn hình consent nếu có.
        _dismiss_consent(cdp, session_id)
        time.sleep(poll)
    return last


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def auto_login(
    account: str,
    *,
    password: str | None = None,
    keep_open: bool = True,
    wait_ms: int = 6000,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Đăng nhập Google tự động cho 1 account loại 2.

    Args:
        account: tên account trong registry.
        password: mật khẩu. Nếu `None` → lấy từ credential store (DPAPI).
        keep_open: giữ Chrome mở khi cần người dùng xử lý tay (mặc định có).
        wait_ms: thời gian chờ sau khi mở trang đăng nhập.
        timeout: thời gian chờ tối đa cho mỗi bước.

    Returns:
        dict trạng thái — xem docstring module.
    """
    info = account_registry.get(account)
    if info is None:
        return {
            "ok": False,
            "state": "failed",
            "reason": (
                f"account {account!r} chưa có trong registry — dùng "
                "`account add --email ... --password ...` trước"
            ),
        }

    email = info.get("email")
    if not email:
        return {
            "ok": False,
            "state": "failed",
            "reason": f"account {account!r} chưa có email",
        }

    pwd = password
    if pwd is None:
        pwd = account_registry.get_password(account)
    if not pwd:
        return {
            "ok": False,
            "state": "failed",
            "reason": (
                f"account {account!r} chưa lưu mật khẩu — dùng "
                "`account set-password` hoặc chuyển sang tài khoản loại 1 "
                "(đăng nhập tay trong cửa sổ Chrome)."
            ),
        }

    udd = cookie_sync.account_dir(account)
    udd.mkdir(parents=True, exist_ok=True)

    # **Reuse instance đang chạy**: Chrome không cho 2 tiến trình dùng chung
    # 1 user-data-dir — nếu đã có Chrome mở sẵn cho account này thì ta bắt
    # vào nó, không spawn thêm (Chrome mới sẽ thoát ngay và không mở cổng).
    port = cookie_sync._find_free_port(cookie_sync._account_port_base(account))
    session = cookie_sync.ChromeSession(
        udd, port=port, start_url="about:blank", reuse_existing=True
    )
    try:
        session.start()
    except Exception as e:
        return {
            "ok": False,
            "state": "failed",
            "reason": f"không khởi động được Chrome: {type(e).__name__}: {e}",
        }

    cdp = session._cdp  # noqa: SLF001 — cùng package, chấp nhận
    steps: list[str] = []

    try:
        target_id = cdp.open_page("about:blank")
        sid = cdp.attach(target_id)
        cdp.call("Page.enable", {}, session_id=sid)
        cdp.call("DOM.enable", {}, session_id=sid)
        # `open_page` chỉ tạo tab about:blank — phải tự điều hướng.
        cdp.call("Page.navigate", {"url": SIGNIN_URL}, session_id=sid)
        time.sleep(wait_ms / 1000)

        # Chụp cookie 1PSID **trước** khi điền form. Nếu profile đã có cookie
        # cũ (đã chết), ta cần biết để phân biệt "cookie mới" với "cookie cũ"
        # khi kiểm tra kết quả. Xem bước 4.
        try:
            cookies_before = cdp.get_cookies()
        except Exception:
            cookies_before = {}

        # -- Bước 1: email -------------------------------------------------
        # Chờ ô email xuất hiện (không phải PASSWORD_READY — ô email chưa
        # được điền nên màn hình vẫn là bước 1).
        if not _wait_for_selector(cdp, sid, SELECTORS["email"], timeout=timeout):
            st = _state(cdp, sid)
            if st == "CAPTCHA":
                return _human(
                    steps, session, "Google hiện CAPTCHA ở bước nhập email", keep_open
                )
            if st == "BLOCKED":
                return _blocked(steps)
            if st == "CHALLENGE":
                return _human(
                    steps, session, "Google yêu cầu xác thực ở bước nhập email", keep_open
                )
            if st == "LOGGED_IN" and cookies_before.get(cookie_sync.REQUIRED):
                # **Profile đã đăng nhập sẵn** (Google redirect thẳng khỏi
                # trang signin). Không cần điền form — nhảy thẳng sang bước
                # verify (TOS + sync).
                #
                # Verify (2026-11-14): `reigin483` đã đăng nhập ở lần trước
                # → `account login` lần 2 báo "không thấy ô nhập email".
                steps.append("profile đã đăng nhập sẵn — bỏ qua form đăng nhập")
                return _verify_and_finish(
                    account, info, email, session, cdp, sid, steps, keep_open
                )
            return _unknown(steps, f"không thấy ô nhập email ({st})")

        ok, detail = _fill(
            cdp, sid, "email", email, next_selectors=SELECTORS["next"]
        )
        steps.append(detail)
        if not ok:
            return _fail(steps, "failed", detail)

        # -- Bước 2: chờ trạng thái -----------------------------------------
        st = _wait_state(
            cdp,
            sid,
            {"PASSWORD_READY", "CAPTCHA", "BLOCKED", "CHALLENGE", "ERROR:INVALID"},
            timeout=timeout,
        )
        if st == "CAPTCHA":
            return _human(steps, session, "Google hiện CAPTCHA ở bước nhập email", keep_open)
        if st == "BLOCKED":
            return _blocked(steps)
        if st == "CHALLENGE":
            return _human(steps, session, "Google yêu cầu xác thực 2 bước", keep_open)
        if st.startswith("ERROR:"):
            msg = st[6:].strip()
            if _is_rate_limited(msg):
                return _human(
                    steps,
                    session,
                    f"Google báo: {msg}",
                    keep_open,
                )
            return _fail(steps, "failed", f"Google báo: {msg}")
        if st != "PASSWORD_READY":
            return _unknown(steps, f"không vào được bước mật khẩu ({st})")

        # -- Bước 3: password -----------------------------------------------
        ok, detail = _fill(
            cdp, sid, "password", pwd, next_selectors=SELECTORS["next"]
        )
        steps.append(detail)
        if not ok:
            return _fail(steps, "failed", detail)

        # -- Bước 4: chờ hoàn tất + dọn consent ----------------------------
        st = _wait_state(
            cdp,
            sid,
            {"LOGGED_IN", "CAPTCHA", "BLOCKED", "CHALLENGE", "CONSENT"},
            timeout=timeout,
        )
        if st == "CAPTCHA":
            return _human(steps, session, "Google hiện CAPTCHA sau khi gửi mật khẩu", keep_open)
        if st == "BLOCKED":
            return _blocked(steps)
        if st == "CHALLENGE":
            return _human(steps, session, "Google yêu cầu xác thực 2 bước", keep_open)

        # Chờ thêm cho cookie 1PSID **MỚI** xuất hiện.
        #
        # Bẫy đã gặp (2026-11-14): profile Chrome đã có sẵn cookie 1PSID **cũ
        # đã chết** từ lần đăng nhập trước → vòng lặp thấy cookie ngay lập tức
        # → báo "đăng nhập thành công" dù thực tế trang Gemini vẫn hiện
        # "Đăng nhập" và session vẫn `UNAUTHENTICATED`.
        #
        # Sửa: so sánh với cookie 1PSID **trước khi** điền form; chỉ coi là
        # thành công khi cookie đã **đổi** hoặc trang Gemini xác nhận đăng nhập.
        psid_before = cookies_before.get(cookie_sync.REQUIRED)
        deadline = time.time() + 25
        cookies: dict[str, str] = {}
        psid_changed = False
        while time.time() < deadline:
            cookies = cdp.get_cookies()
            psid_now = cookies.get(cookie_sync.REQUIRED)
            if psid_now and psid_now != psid_before:
                psid_changed = True
                break
            _dismiss_consent(cdp, sid)
            time.sleep(1.0)

        if cookie_sync.REQUIRED not in cookies:
            return _unknown(
                steps,
                "Sau khi điền form vẫn không thấy cookie 1PSID — có thể còn "
                "bước consent/challenge. Kiểm tra cửa sổ Chrome.",
            )

        # -- Bước 5: verify + sync ------------------------------------------
        return _verify_and_finish(
            account,
            info,
            email,
            session,
            cdp,
            sid,
            steps,
            keep_open,
            psid_changed=psid_changed,
            cookies=cookies,
        )
    except Exception as e:
        return _fail(steps, "failed", f"{type(e).__name__}: {e}")
    finally:
        if not keep_open:
            try:
                session.close()
            except Exception:
                pass


def _verify_and_finish(
    account: str,
    info: dict[str, Any],
    email: str,
    session: Any,
    cdp: Any,
    sid: str,
    steps: list[str],
    keep_open: bool,
    *,
    psid_changed: bool = False,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Sync cookie → accept TOS nếu cần → probe session → trả kết quả.

    Dùng chung cho 2 đường:
      - đăng nhập mới qua form (đã có cookie mới)
      - profile **đã đăng nhập sẵn** (bỏ qua form)

    Thứ tự quan trọng: **Chrome phải còn mở** khi accept TOS (cần bấm nút
    trên trang), nên ta sync → xử lý TOS → sync lại → mới đóng Chrome.
    """
    if cookies is None:
        try:
            cookies = cdp.get_cookies()
        except Exception:
            cookies = {}

    pool = info.get("pool") or account
    sync_result = cookie_sync.sync(
        account, pool_name=pool, account_email=email
    )

    # -- Accept Terms of Service ------------------------------------------
    #
    # Tài khoản mới bị Gemini chặn `TOS_PENDING` cho tới khi đồng ý điều
    # khoản. Mở trang Gemini → bấm "Tôi hiểu" / "I understand" → sync lại.
    #
    # Verify (2026-11-14, account `reigin483`): trước khi có bước này →
    # `TOS_PENDING`; sau khi bấm "Tôi hiểu" → `AVAILABLE`.
    if sync_result.get("account_status") == "TOS_PENDING":
        tos_clicked = None
        try:
            cdp.call("Page.navigate", {"url": cookie_sync.GEMINI_URL}, session_id=sid)
            time.sleep(6)
            for _ in range(5):
                tos_clicked = _accept_tos(cdp, sid)
                if tos_clicked:
                    steps.append(f"đã đồng ý điều khoản: bấm '{tos_clicked}'")
                    time.sleep(3)
                    break
                _dismiss_consent(cdp, sid)
                time.sleep(1.5)
            # Sync lại sau khi đồng ý (cookie 1PSIDTS có thể vừa rotate).
            if tos_clicked:
                sync_result = cookie_sync.sync(
                    account, pool_name=pool, account_email=email
                )
        except Exception:
            pass

    # Đóng Chrome graceful để flush cookie vào profile.
    try:
        cdp.close_browser()
    except Exception:
        pass

    # **Xác minh thật**: session phải còn sống (gọi API thật), không chỉ
    # "có cookie". Cookie cũ vẫn còn trong profile nên kiểm tra bằng
    # `account_status` mới đáng tin.
    session_ok = bool(sync_result.get("session_ok"))
    if not session_ok:
        account_registry.note_error(
            account,
            "đăng nhập xong nhưng session vẫn không hợp lệ "
            f"(account_status={sync_result.get('account_status')})",
        )
        return {
            "ok": False,
            "state": "needs_human",
            "reason": (
                "Đã điền form nhưng Google vẫn chưa cấp session hợp lệ "
                f"(account_status={sync_result.get('account_status')}). "
                "Có thể còn màn hình consent/2FA, hoặc Google đã khoá "
                "tài khoản."
            ),
            "hint": (
                "Kiểm tra cửa sổ Chrome: đăng nhập xong hãy mở "
                "https://gemini.google.com/app và xem có hiện 'Thư viện' "
                "không. Nếu có → bấm 'Đồng bộ cookie'."
            ),
            "steps": steps,
            "pool": pool,
            "session_ok": False,
            "account_status": sync_result.get("account_status"),
            "cookies": sync_result.get("cookies"),
            "psid_changed": psid_changed,
        }

    account_registry.note_ok(account)

    return {
        "ok": True,
        "state": "ok",
        "reason": "đăng nhập thành công",
        "detail": sync_result.get("warning") or "",
        "steps": steps,
        "pool": pool,
        "session_ok": sync_result.get("session_ok"),
        "account_status": sync_result.get("account_status"),
        "psid_changed": psid_changed,
        "cookies": sync_result.get("cookies"),
    }


# ---------------------------------------------------------------------------
# Kết quả
# ---------------------------------------------------------------------------

def _is_rate_limited(msg: str) -> bool:
    """Google báo "quá nhiều lần thử" / "unusual activity" → cần chờ, không phải sai mật khẩu."""
    t = msg.lower()
    return any(
        m in t
        for m in (
            "too many failed attempts",
            "unusual activity",
            "quá nhiều lần",
            "traffic hoặc điều khoản",
            "try again later",
            "thử lại sau",
        )
    )


def _fail(steps: list[str], state: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "state": state, "reason": reason, "steps": steps}


def _unknown(steps: list[str], reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "state": "unknown",
        "reason": reason,
        "hint": "Mở lại cửa sổ Chrome bằng `cookie login` để kiểm tra thủ công.",
        "steps": steps,
    }


def _blocked(steps: list[str]) -> dict[str, Any]:
    return {
        "ok": False,
        "state": "blocked",
        "reason": (
            "Google chặn: 'This browser or app may not be secure' / "
            "'Couldn't sign you in'. Thường do IP bị nghi ngờ hoặc Chrome "
            "bị gắn cờ automation."
        ),
        "hint": (
            "Thử: (1) đợi 10-15 phút rồi thử lại, (2) dùng `cookie login` để "
            "đăng nhập tay trong cửa sổ Chrome thay vì tự động, (3) đổi IP."
        ),
        "steps": steps,
    }


def _human(
    steps: list[str],
    session: Any,
    reason: str,
    keep_open: bool,
) -> dict[str, Any]:
    """Trạng thái cần người dùng xử lý tay (CAPTCHA / 2FA / consent)."""
    hint = (
        "Cửa sổ Chrome đang mở. Hãy xử lý thủ công (CAPTCHA / mã 2FA / "
        "nút Continue), đăng nhập xong rồi chạy `cookie sync`."
    )
    if not keep_open:
        try:
            session.close()
        except Exception:
            pass
        hint = (
            "Cần bạn xử lý tay trong Chrome (CAPTCHA / 2FA). Chạy "
            "`cookie login` để mở cửa sổ Chrome, đăng nhập xong rồi "
            "`cookie sync`."
        )
    return {
        "ok": False,
        "state": "needs_human",
        "reason": reason,
        "hint": hint,
        "steps": steps,
    }
