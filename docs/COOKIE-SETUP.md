# COOKIE-SETUP.md — Hướng dẫn lấy cookie Gemini

Đây là **bước duy nhất còn thiếu** để chạy Deep Research. Không có cookie hợp lệ thì mọi thứ dừng ở giá trị 0.

---

## 1. Vì sao không đọc thẳng từ cookie DB

Chrome ≥ 127 mã hoá cookie bằng **app-bound encryption** (prefix `v20`):

```
__Secure-1PSID   plain=0  enc=216  prefix=763230
```

`value` rỗng, dữ liệu nằm ở `encrypted_value` và **chỉ Chrome mới giải mã được**. Đã thử và thất bại:

| Cách | Lỗi |
|---|---|
| `shutil.copyfile` | `PermissionError` |
| `CreateFileW` + `FILE_SHARE_*` | error 32 (sharing violation) |
| `esentutl /y` | `JET_errFileAccessDenied` |
| SQLite `mode=ro&immutable=1` | `unable to open database file` |
| `robocopy /B` | không copy được file đang lock |

Đây là hạn chế đã biết, không phải lỗi máy:
<https://github.com/steipete/oracle/issues/51>

→ Phải lấy cookie **từ bên trong tiến trình Chrome**.

---

## 2. Cách A — Cookie-Editor (khuyến nghị, đơn giản nhất)

1. Cài extension [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/iphcbtmnmbpfljmdbjelpaamkflkchhh)
   - **Làm trong TỪNG profile Chrome** (extension cài riêng cho từng profile)
2. Mở tab `https://gemini.google.com/app` và đảm bảo **đã đăng nhập**
3. Bấm icon Cookie-Editor → **Export** → **Export as JSON**
4. Lưu file:

```
D:\GitHub\my-gemini-pro\pools\chrome-default.json      ← Chrome profile "Default"  (mmeo44244@gmail.com)
D:\GitHub\my-gemini-pro\pools\chrome-profile1.json     ← Chrome profile "Vĩnh"    (catkingmv@gmail.com)
```

### Format JSON của Cookie-Editor

```json
[
  {
    "domain": ".google.com",
    "expirationDate": 1799999999.9,
    "hostOnly": false,
    "httpOnly": true,
    "name": "__Secure-1PSID",
    "path": "/",
    "sameSite": "no_restriction",
    "secure": true,
    "session": false,
    "storeId": "0",
    "value": "..."
  },
  { "name": "__Secure-1PSIDTS", "value": "...", "domain": ".google.com", ... }
]
```

`setup_pool.py` sẽ tự lọc `__Secure-1PSID` + `__Secure-1PSIDTS` và bỏ qua phần còn lại.

### ⚠️ Kiểm tra trước khi export

Chạy script để biết chắc profile nào đang đăng nhập acc nào:

```powershell
python scripts\scan_browser_cookies.py
```

Kết quả hiện tại:

```
--- Chrome/Default      account: mmeo44244@gmail.com   gaia: 100722739602672374612
--- Chrome/Profile 1    account: catkingmv@gmail.com   gaia: 107894897885205192113
--- Edge/Default        account: kimvinhh6@gmail.com   gaia: 81dea17dfd148f0b
--- Edge/Profile 1      (không đăng nhập Google)
```

---

## 3. Cách B — Tự động qua CDP (không cần extension)

Yêu cầu: **đóng hoàn toàn Chrome**, rồi mở lại với cổng debug.

```powershell
# 1. Đóng Chrome sạch
Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force

# 2. Mở lại với debugging port
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
    --remote-debugging-port=9222 `
    --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data"
```

⚠️ `--user-data-dir` phải trỏ đúng thư mục sẵn có, nếu không sẽ tạo profile mới rỗng.

Sau đó đọc cookie qua HTTP endpoint của DevTools:

```
GET http://127.0.0.1:9222/json          → danh sách target (tab)
GET http://127.0.0.1:9222/json/version  → webSocketDebuggerUrl
```

Rồi dùng WebSocket `Network.getAllCookies` / `Storage.getCookies` để lấy cookie của `https://gemini.google.com`.

> Script `scripts/setup_pool.py --cdp` sẽ làm việc này (chưa viết — xem HANDOFF.md §8).

---

## 4. ⚠️ Rủi ro "Evil Clone" — ĐỌC TRƯỚC KHI CHẠY

Google nhận diện session trùng lặp. Nếu bạn **vừa copy cookie từ profile đang mở** rồi chạy `gemini_webapi` cùng lúc, Google có thể:

1. coi là session bị hijack
2. **kill switch → logout Google khỏi MỌI thiết bị**

Nguồn: <https://github.com/TNLegend/Chimera>

> *"Google will see two identical sessions doing different things at the exact same time... Google will assume your session was hijacked by a hacker and will immediately hit the kill switch, logging you out of your Google account across all devices."*

### Quy tắc an toàn

| Nên | Không nên |
|---|---|
| Đóng Chrome trước khi chạy automation | Chạy automation khi profile đó đang mở |
| Dùng 1 profile Chrome riêng cho automation | Dùng profile cá nhân chính |
| Test trước với 1 pool, 1 câu ngắn | Chạy song song 2 pool ngay |

---

## 5. Vòng đời cookie

| Cookie | TTL | Xử lý |
|---|---|---|
| `__Secure-1PSID` | vài tháng | Chết → phải export lại |
| `__Secure-1PSIDTS` | ngắn (xoay thường xuyên) | `gemini_webapi` tự refresh mỗi 600s qua `accounts.google.com/RotateCookies` |

Nếu `deep_research()` báo lỗi auth → export lại cookie.

---

## 6. Kiểm tra pool đã tạo

```powershell
python scripts\setup_pool.py --validate
```

Expected output:

```
pool 'windows'  → mmeo44244@gmail.com  ✓ 1PSID ok, 1PSIDTS ok
pool 'wsl'      → catkingmv@gmail.com  ✓ 1PSID ok, 1PSIDTS ok
```

Sau đó chạy thử 1 câu deep research:

```powershell
python scripts\setup_pool.py --probe
```
