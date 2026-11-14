# Quản lý tài khoản & cookie Gemini — thiết kế lại

> Thay thế cơ chế cũ (3 nguồn trạng thái rời rạc + Cookie-Editor thủ công).
> Ngày: 2026-11-14.

---

## 1. Vì sao làm lại

Cơ chế cũ có **3 nguồn trạng thái** rời rạc, lệch nhau theo thời gian:

| Nguồn | Nội dung | Vấn đề |
|---|---|---|
| `pools/<pool>.json` | cookie + email + `profile` | thêm account phải tự tạo file |
| `pools/.account_pools.json` | mapping account → pool | mất khi copy máy, không có trong `.exe` |
| (không có) | mật khẩu | không lưu được → không tự đăng nhập lại được |

Thêm 1 account tốn **3 thao tác rời rạc** (`add` + `adopt` + `sync`), dễ sót →
user phải làm thủ công, và không có cách nào tự phục hồi khi session chết.

Hệ quả: user không biết cookie để nhập vào đâu, và mỗi account mới đều cần
can thiệp tay.

---

## 2. Kiến trúc mới

### 2.1. Một nguồn sự thật

```
<pools>/accounts.json          ← registry: danh sách tài khoản (nguồn sự thật)
<pools>/<pool>.json            ← cache cookie (đọc bởi gemini_webapi)
%LOCALAPPDATA%\Orchestrator\credentials.json   ← mật khẩu (mã hoá DPAPI)
```

`orchestrator/accounts.py` — registry. Entry mỗi account:

```json
{
  "account": "t1vodich",
  "email": "user@gmail.com",
  "pool": "acc3",
  "auth": "password",
  "created_at": "...",
  "last_ok_at": "...",
  "last_error": "..."
}
```

### 2.2. Hai loại tài khoản

| | **Loại 1 — có xác thực** | **Loại 2 — chỉ email + mật khẩu** |
|---|---|---|
| `auth` | `"oauth"` | `"password"` |
| Mật khẩu | không lưu | lưu mã hoá DPAPI |
| Đăng nhập | user tay 1 lần trong Chrome riêng | orchestrator tự điền form |
| Dùng lại session | `cookie sync` tự động | `account login` tự động |
| Khi session chết | user đăng nhập tay lại | `account login` lại — không cần user |

Cả hai dùng **chung 1 entry registry**, cùng profile Chrome riêng, cùng pool.

### 2.3. Vì sao phải có Chrome (không dùng OAuth HTTP thuần)

Đã nghiên cứu và **verify thực nghiệm** (`reports/research-oauth-cookies.md`):

1. **OAuth ROPC không tồn tại ở Google.** Endpoint `accounts.google.com/o/oauth2/v2/auth`
   không nhận username/password. Gọi bằng HTTP client (curl_cffi, impersonate
   `chrome124`) → Google trả `302` về `accounts.google.com/v3/signin/identifier`.
   Cookie jar sau đó chỉ có `__Host-GAPS`.
2. **Botguard + reCAPTCHA Enterprise** chặn mọi HTTP POST thô form login →
   `403`, checkpoint SMS, hoặc *"This browser or app may not be secure"*.
3. **Bearer token `ya29...` không dùng được** cho `gemini.google.com` — web app
   xác thực bằng cookie `__Secure-1PSID` + CSRF `SNlM0e`.
4. **`__Secure-1PSIDTS` không sinh ra trong OAuth flow** — nó được sinh từ
   `RotateCookiesPage` iframe trên `gemini.google.com` (hoặc `RotateCookies`).
5. **App-Bound Encryption (Chrome ≥127, Windows)**: file `Network/Cookies` không
   còn giải mã được bằng `CryptUnprotectData` thường → `browser-cookie3` chết.
   Đọc cookie **từ chính Chrome qua CDP** là cách hợp lệ duy nhất.

→ Kết luận: **Chrome + CDP là đường đúng**, không phải lựa chọn vì "dễ làm".

### 2.4. DBSC — vì sao mỗi account 1 profile riêng vĩnh viễn

Google **Device Bound Session Credentials** (Chrome ≥127) bind session vào cặp
khoá bất đối xứng sinh trong **TPM** của máy:

- Khoá private **non-exportable** → không copy được ra ngoài Chrome.
- `__Secure-1PSIDTS` được refresh bằng chữ ký TPM → HTTP client độc lập
  (`gemini_webapi`) **không refresh được** sau 2–6 giờ.

→ **Không bao giờ** copy cookie giữa 2 user-data-dir. Mỗi account có profile
riêng vĩnh viễn tại `%LOCALAPPDATA%\OrchestratorChrome\<account>`.

Nguồn: <https://w3c.github.io/webappsec-dbsc/>,
<https://blog.google/products/chrome/protecting-users-from-cookie-theft/>.

*Lưu ý:* `gemini_webapi` tự gọi `accounts.google.com/RotateCookies` qua HTTP để
xoay `__Secure-1PSIDTS` (xem `utils/rotate_1psidts.py`) — nên session vẫn sống
đủ lâu **miễn là** `__Secure-1PSID` còn hiệu lực.

---

## 3. Luồng đăng nhập tự động (tài khoản loại 2)

Module: `orchestrator/auto_login.py`.

```
1. Mở Chrome (profile riêng của account) → accounts.google.com/ServiceLogin?hl=en
2. Chờ ô email → gõ TỪNG KÝ TỰ (Input.insertText + jitter 40-120ms)
3. Click #identifierNext button
4. Chờ ô password → gõ từng ký tự → click #passwordNext button
5. Đọc cookie từ Storage.getCookies → ghi pool file → probe session
```

### 3.1. Kỹ thuật gõ — vì sao không gán `.value` bằng JS

Google dùng Polymer/Lit. Gán `element.value = "..."` bằng `Runtime.evaluate`:

- Không cập nhật internal state của framework.
- Event `input` sinh ra có `isTrusted: false` → **Botguard đánh dấu ngay**.

Đúng cách: `DOM.focus` → `Input.insertText` từng ký tự → event `isTrusted: true`.
Gõ 1 lần tức thời vẫn giống bot → cần **jitter ngẫu nhiên 40–120 ms/ký tự**.

Click cũng vậy: `Input.dispatchMouseEvent` tại toạ độ tâm element (lấy qua
`getBoundingClientRect`) thay vì `element.click()` bằng JS.

Enter: chuỗi 3 sự kiện `rawKeyDown` (kèm `text: "\r"`) → `char` → `keyUp`.
Thiếu `char` thì form không submit.

### 3.2. Phát hiện trạng thái — dùng DOM + URL, không chỉ text

Script `STATE_SCRIPT` trả về 1 trong:

| State | Nghĩa | Xử lý |
|---|---|---|
| `PASSWORD_READY` | tới bước mật khẩu | điền tiếp |
| `CAPTCHA` | có reCAPTCHA | **dừng**, báo user |
| `CHALLENGE` | 2FA thật (`totp`/`ipp`/`oaz`/`kpe`) | **dừng**, báo user |
| `BLOCKED` | *"This browser or app may not be secure"* | báo user, gợi ý đổi IP / chờ |
| `CONSENT` | màn hình passkey / recovery | bấm "Not now" tự động |
| `ERROR:<msg>` | Google báo lỗi | phân loại rate-limit vs sai mật khẩu |
| `LOGGED_IN` | đã ra khỏi `accounts.google.com` | lấy cookie |

**Bẫy đã gặp (đã sửa):**

1. `img#captchaimg` và `input[name="ca"]` **luôn nằm sẵn trong DOM** của Google
   (ẩn). Check `!!document.querySelector(...)` → **false-positive CAPTCHA**.
   → Phải kiểm tra **hiển thị** (`offsetParent !== null` hoặc bounding box > 0).
2. URL bước nhập mật khẩu cũng là `/signin/challenge/pwd` — **không** phải
   challenge 2FA. Regex phải loại trừ `pwd`: `/\/signin\/challenge\/(?!pwd\b)/`.
3. Vùng `aria-live="assertive"` chứa cả **heading** ("Welcome", "Sign in") và
   text ngôn ngữ ("English (United States)") → phải lọc ra, nếu không sẽ báo
   "Google báo: Welcome".
4. `account_status` thành công là **`AVAILABLE` (1000)**, không phải `ACTIVE`.
   Xem `gemini_webapi.constants.AccountStatus`.

### 3.3. Ranh giới đạo đức

Module **không bypass CAPTCHA, không bypass 2FA, không giải mã challenge**.
Khi gặp challenge → trả `state: "needs_human"`, giữ Chrome mở, báo user xử lý
tay. Đây là hành động của chính user, không phải vượt kiểm soát.

---

## 4. Credential store — DPAPI

Module: `orchestrator/credentials.py`.

- Dùng **Windows DPAPI** (`CryptProtectData` qua `ctypes`) — không thêm dependency.
- **User-scope**: chỉ user Windows hiện tại trên máy này giải mã được.
- **Entropy bổ sung** theo tên account → ciphertext của account A không giải mã
  được cho account B.
- DPAPI có sẵn **integrity** → ciphertext bị sửa sẽ fail decrypt.
- File: `%LOCALAPPDATA%\Orchestrator\credentials.json` (mặc định
  `~/.cursor-agents/orchestrator/`), ACL chỉ cho user hiện tại (best-effort).
- **Không bao giờ** log mật khẩu, **không bao giờ** trả mật khẩu trong JSON
  response của CLI/GUI.

---

## 5. CLI

```powershell
# Thêm tài khoản loại 2 + tự đăng nhập luôn
python -m orchestrator.orch_cli account add `
  --email user@gmail.com --account acc3 --pool acc3 --password "..."

# Đăng nhập lại (session chết)
python -m orchestrator.orch_cli account login --account acc3

# Đặt mật khẩu cho account đã có (mã hoá DPAPI)
python -m orchestrator.orch_cli account set-password --account acc3 --password "..."

# Xem / xoá
python -m orchestrator.orch_cli account list
python -m orchestrator.orch_cli account show --account acc3
python -m orchestrator.orch_cli account remove --account acc3 --delete-pool
```

> `--password` truyền trực tiếp sẽ lộ trong process listing. An toàn hơn: nhập
> trong GUI (mật khẩu không nằm trong command line), hoặc dùng
> `account set-password` sau đó `account login` không cần mật khẩu.

Lệnh cũ vẫn chạy (`cookie sync|adopt|status|login`) cho tương thích.

---

## 6. GUI

Cửa sổ "Tài khoản Gemini" (nút trên header):

- Bảng: account · pool · email · loại · session · cập nhật lần cuối.
- Form **Thêm & đăng nhập**: email + (tên account) + (pool) + mật khẩu →
  tự đăng nhập.
- Form **Mật khẩu**: chọn account → nhập mật khẩu → "Lưu & đăng nhập".
- Hành động: đăng nhập tay · nhập cookie (file) · đồng bộ · kiểm tra session ·
  xoá tài khoản.

Kết quả đăng nhập tự động hiển thị theo `state`:
`ok` → thông báo thành công · `needs_human` → cảnh báo + hướng dẫn ·
`blocked` → lỗi + gợi ý.

---

## 7. Kiểm thử đã chạy (2026-11-14)

| Hạng mục | Kết quả |
|---|---|
| DPAPI encrypt/decrypt | OK (round-trip, sai account không giải mã được) |
| Registry add/list/remove | OK |
| Auto-login: mở Chrome + dọn instance treo | OK (sau 3 lần sửa) |
| Auto-login: điền email + submit | OK → tới `/signin/challenge/pwd` |
| Auto-login: điền password + submit | OK → 21 cookie trong profile |
| `cookie sync` → pool file | OK (`pools/acc3.json`) |
| `_probe_session` | OK → `account_status=AVAILABLE`, `session_ok=true` |
| Chat thường (`gemini-flash-lite`) | OK → `PING_OK` |
| **Deep Research** (`create_deep_research_plan`) | **OK** → plan "Capital of France" |

→ Tài khoản loại 2 đăng nhập tự động **không cần user thao tác**, và Deep
Research chạy được với account đó.

---

## 8. Hạn chế đã biết

| Vấn đề | Xử lý |
|---|---|
| Google chặn ("not secure") khi IP bị nghi ngờ | báo user: chờ 10–15 phút, đổi IP, hoặc đăng nhập tay |
| Rate limit: > ~3 account/giờ từ cùng IP | nên có cooldown giữa các lần đăng nhập |
| CAPTCHA / 2FA | dừng, báo user — **không** bypass |
| Chrome cũ bị treo giữ user-data-dir | `ChromeSession._kill_stale_instances()` dọn trước khi spawn |
| `__Secure-1PSIDTS` hết hạn sau vài giờ | `gemini_webapi` tự rotate qua `RotateCookies`; nếu `__Secure-1PSID` chết thì `account login` lại |
| Copy cookie sang máy khác | **không được** — DBSC bind TPM + ABE |

---

## 9. File liên quan

| File | Vai trò |
|---|---|
| `orchestrator/accounts.py` | registry tài khoản (nguồn sự thật) |
| `orchestrator/credentials.py` | kho mật khẩu DPAPI |
| `orchestrator/auto_login.py` | đăng nhập tự động qua CDP |
| `orchestrator/cdp.py` | CDP client + primitives điền form |
| `orchestrator/cookie_sync.py` | sync cookie, probe session, quản lý Chrome |
| `orchestrator/gui.py` | cửa sổ "Tài khoản Gemini" |
| `orchestrator/orch_cli.py` | lệnh `account` |
| `reports/research-oauth-cookies.md` | nghiên cứu OAuth HTTP flow (bất khả thi) |
| `reports/research-google-login-cdp.md` | nghiên cứu CDP + selector + rào cản |
