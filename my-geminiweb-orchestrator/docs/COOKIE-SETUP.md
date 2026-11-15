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

## 3. ⭐ Cách C — Tự động hoá hoàn toàn (orchestrator, KHÔNG cần extension)

Đây là cách **được khuyến nghị** hiện nay. Bạn chỉ cần làm **một lần** cho mỗi
tài khoản; sau đó orchestrator tự lấy cookie mới mỗi khi cần.

### 3.1. Nguyên tắc

Orchestrator sở hữu **một Chrome riêng** cho mỗi account, đặt tại:

```
%LOCALAPPDATA%\OrchestratorChrome\<account>\
```

Chrome đó **không** dùng chung profile với Chrome của bạn → không lo
"Evil Clone", không xoá session cá nhân, không cần đóng Chrome.

### 3.2. Quy trình 2 bước

**Bước 1 — login (cách ưu tiên, không cần extension):**

```powershell
python -m orchestrator.cookie_sync login --account catkingmv
```

Chrome riêng của orchestrator mở ra. Bạn đăng nhập Google bình thường, mở
tab `https://gemini.google.com/app` để chắc chắn đã đăng nhập, rồi **đóng
Chrome**. Xong.

**Bước 1' — adopt (nếu đã có file export Cookie-Editor):**

```powershell
python -m orchestrator.cookie_sync adopt `
    --account mmeo `
    --export "D:\GitHub\.gemini-pool\chrome-default.json" `
    --pool windows `
    --email "mmeo44244@gmail.com"
```

Kết quả mong đợi:

```json
{
  "ok": true,
  "cookies_seeded": 15,
  "cookies_live": 15,
  "signed_in": true,
  "pool_file": "...\\pools\\windows.json"
}
```

`signed_in: true` nghĩa là browser đã đăng nhập thật (trang Gemini hiện
"Thư viện", "Đã lên lịch", lịch sử chat).

**Bước 2 — sync (mỗi lần cần cookie mới):**

```powershell
python -m orchestrator.orch_cli cookie sync --account mmeo
```

> `--pool` là **optional** — orchestrator tự map account → pool
> (`mmeo` → `windows`, `catkingmv` → `wsl`). Chỉ cần chỉ định khi dùng
> account mới chưa có trong mapping.

Hoặc từ agent qua MCP:

```
orch_cookie_sync(action="sync", account="mmeo")
```

### 3.3. Vì sao các cách "hiển nhiên" không dùng được (đã verify thật)

| Cách | Kết quả thực nghiệm |
|---|---|
| Đọc `Network/Cookies` ngoài tiến trình | Không giải mã được (app-bound `v20`) |
| **Copy** profile sang user-data-dir khác | Chrome **xoá cookie** (65 → 0): Device Bound Session không khớp |
| `--remote-debugging-port` với user-data-dir default | Chrome ≥136 **từ chối** (không mở port) |
| `--remote-debugging-port` + **short path 8.3** (`USERDA~1`) | Port mở được, nhưng cookie vẫn không giải mã (Chrome so sánh path đã resolve) |
| `--remote-debugging-pipe` | Trên Windows cần handle riêng, `stdin` không dùng được |
| **Chrome riêng + cấy cookie qua CDP** | ✅ **Thành công** |

Nguyên tắc rút ra: Google bind session (Device Bound Session) vào *đúng
user-data-dir nơi nó được tạo*. Nên phải **làm việc trong chính Chrome đó**,
đừng copy đi.

### 3.4. ⚠️ Đóng Chrome đúng cách

Chrome chỉ ghi session cookie vào DB khi thoát **graceful**. Nếu kill cứng
(`terminate()`), cookie biến mất và lần sau phải `adopt` lại.

Module `cookie_sync` luôn đóng bằng CDP `Browser.close` → thoát mã `0` →
cookie được flush. Nếu bạn tự kill Chrome của orchestrator, hãy chạy lại
`sync` (cookie vẫn còn trong phiên nếu Chrome chưa đóng) hoặc `adopt`.

> Khi dùng `login`, bạn đóng Chrome bằng cửa sổ → Chrome thoát graceful →
> cookie được flush → `sync` đọc được. Đừng kill cứng từ Task Manager.

### 3.5. Bảng tóm tắt

| Lệnh | Khi nào | Cần extension? |
|---|---|---|
| `cookie_sync login --account X` | Account mới, chưa từng có cookie | ❌ |
| `cookie_sync adopt --account X --export f.json` | Đã có file export Cookie-Editor | ✅ (1 lần) |
| `cookie_sync sync --account X` | Mỗi lần cần cookie mới | ❌ |
| `cookie_sync status --account X` | Kiểm tra trạng thái | ❌ |

Mapping account → pool tự động (`orchestrator/cookie_sync.py`, `ACCOUNT_POOLS`):
`mmeo` → `windows`, `catkingmv` → `wsl`. Cần account mới thì thêm vào dict
này (hoặc truyền `--pool`).

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
