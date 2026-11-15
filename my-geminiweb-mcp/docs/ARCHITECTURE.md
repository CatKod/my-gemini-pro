# my-geminiweb-mcp — Kiến trúc & kế hoạch

> MCP Server App (.NET) cho Gemini Web — sản phẩm NuGet, chạy độc lập, không cần Python.

---

## 1. Quyết định hướng (đã chốt với user)

| Quyết định | Giá trị |
|---|---|
| Mục tiêu | Publish **NuGet** cho người khác dùng: ổn định, nhanh, thực dụng |
| GUI/Tkinter | **Bỏ** (đúng hướng — GUI không đóng góp cho sản phẩm NuGet) |
| Multi-account | **Có** — pool nhiều account + auto-failover |
| Deep Research | **Để sau** — phiên bản đầu chỉ tối ưu chat thường |

Hệ quả: **không được phụ thuộc Python runtime**. Python orchestrator cũ được giữ
lại như archive tham khảo ở `my-geminiweb-orchestrator/`.

---

## 2. Spike đã verify (không phải giả định)

Mọi kết luận dưới đây đều chạy thật và có output.

### 2.1 TLS impersonation **KHÔNG** bắt buộc ✅

`gemini_webapi` dùng `curl_cffi` với `impersonate="chrome145"` (tránh Device Bound
Session Credentials). Người ta có thể lo rằng .NET sẽ bị chặn.

**Kết quả:** .NET `HttpClient` (TLS thuần, HTTP/1.1) lấy được token `SNlM0e` +
build label + session id **ngay lập tức**.

```
[init] http=200 len=927952
Token     : YES
BuildLabel: YES
```

→ Không cần thư viện TLS fingerprint cho .NET. Đây là rủi ro lớn nhất của bản
port C# và nó đã được loại trừ bằng thực nghiệm, không bằng suy đoán.

### 2.2 Chat end-to-end từ C# ✅

`POST .../StreamGenerate` với payload JSPB 81 slot, parse streaming frame length-prefixed.
Server AOT trả lời đúng:

```
[chat] http=200 rawLen=22831 frames=11
[chat] cid=c_68210903c9ff5964 rid=r_e61a02a1bafd4bfa textLen=11
---- TEXT ----
SPike_OK_42
RESULT: PASS
```

### 2.3 Bẫy frame parser (off-by-one) ⚠️

Parser Python đặt `payload_start = len(length_val)` — payload **bắt đầu tại ký tự
`\n`**, và độ dài tính **cả** newline. Bỏ newline đi → mọi frame fail, không báo lỗi,
chỉ ra `frames=0`.

Đã đối chiếu trực tiếp bằng `reports/_frame_probe.py` trên cùng file raw.

→ `Gemini/StreamingFrameParser.cs` implement đúng quy tắc này, có comment giải thích.

### 2.4 Headers bắt buộc

Ba thứ thiếu là HTTP 400 hoặc rỗng:

| Header | Giá trị |
|---|---|
| `X-Same-Domain` | `1` |
| `x-goog-ext-525005358-jspb` | `["<uuid>",1]` — uuid phải khớp `inner[59]` |
| `Content-Type` | `application/x-www-form-urlencoded;charset=utf-8` |

Query params: `hl`, `_reqid` (random 5 chữ số, **không** random 7 chữ số),
`rt=c`, `bl`, `f.sid`.

### 2.5 Metadata conversation là **10 slot**, không phải 3 ⚠️

`inner_req_list[2]` = metadata gồm 10 phần tử; `cid`/`rid`/`rcid` nằm ở slot
0/1/2, còn lại rỗng. Trả về `[cid, rid, rcid]` (3 phần tử) sẽ sai.

### 2.6 AOT chạy được, nhưng JSON phải AOT-safe ⚠️

`PublishAot=true` → `JsonSerializer.Serialize` với anonymous type **vỡ lúc runtime**
(IL3050). Đã chuyển toàn bộ sang `JsonNode` / `JsonArray`. Build sạch 0 warning IL.

### 2.7 Conversation handle phải mang account ⚠️

Conversation id (`cid`) chỉ có ý nghĩa trên đúng account đã tạo ra nó. Nếu pool xoay
account giữa 2 lượt → lượt 2 rỗng.

→ Handle encode kèm account: `{"account":"...","cid":"...","rid":"..."}`, và lượt tiếp
theo ghim đúng account đó.

### 2.8 Quy mô binary

| Artifact | Kích thước |
|---|---|
| `my-geminiweb-mcp.exe` (AOT, single-file, win-x64) | **16.0 MB** |

Không kèm .NET runtime → cài đặt cho người dùng chỉ là 1 file exe.

### 2.9 Session chết **không** báo lỗi — im lặng ⚠️⚠️

Đây là bẫy nguy hiểm nhất của cơ chế session.

| Gửi tới `GET /app` | HTTP | Token `SNlM0e` |
|---|---|---|
| Chỉ `__Secure-1PSID` | **200** | ❌ **không có** |
| `1PSID` + `1PSIDTS` sai | **200** | ❌ **không có** |
| `1PSID` + `1PSIDTS` đúng | 200 | ✅ có |

→ **HTTP 200 không đồng nghĩa đã đăng nhập.** Một phiên hết hạn trả về trang HTML
bình thường, chỉ khác là không có token. Nếu chỉ kiểm tra status code thì sẽ tưởng
mọi thứ ổn rồi người dùng nhận reply rỗng.

→ `GeminiWebClient.InitializeAsync` **phải** kiểm tra sự hiện diện của token
`SNlM0e`, không được dựa vào status code. Đây là lý do `gemini_check` báo
`needsReharvest` cho một phiên chết.

### 2.10 `RotateCookies` **không** cấp `1PSIDTS` mới ⚠️

Giả định phổ biến: "gọi `accounts.google.com/RotateCookies` để xoay
`__Secure-1PSIDTS`". Đoạn đó **sai** — đã verify:

| Gọi `RotateCookies` | Kết quả |
|---|---|
| Chỉ 2 cookie auth | **200**, `Set-Cookie: __Secure-1PSIDCC` |
| Full jar | **200**, `Set-Cookie: __Secure-1PSIDCC` |
| `1PSIDTS` sai / rỗng | **200**, `Set-Cookie: __Secure-1PSIDCC` |
| Thiếu body | **400** |

`RotateCookies` **không bao giờ** trả `1PSIDTS` mới. Nó chỉ:
1. đẩy lùi mốc hết hạn phiên ở phía server,
2. cấp lại cặp `__Secure-1PSIDCC` / `__Secure-3PSIDCC`.

`1PSIDTS` mới **không** xuất hiện từ bất kỳ request nào của server-side:

| Nguồn | Có cấp `1PSIDTS` mới? |
|---|---|
| `RotateCookies` | ❌ |
| `GET /app` với 2 cookie auth | ❌ (trả nguyên giá trị cũ) |
| `GET /app` với full jar | ❌ |
| **Browser thật đã đăng nhập** | ✅ |

→ **Kết luận sản phẩm:** nguồn `1PSIDTS` sống duy nhất là **trình duyệt của người
dùng**. Đó là lý do extension trình duyệt là kiến trúc đúng, không phải tiện lợi:
nó không chỉ "tiện hơn", mà nó là **cơ chế duy nhất** cấp lại phiên.

Hệ quả cho thiết kế:
- `RotateCookies` vẫn giữ (kéo dài phiên server-side, giảm tần suất phải harvest).
- `SessionKeepAlive` gọi `GET /app` + `RotateCookies` định kỳ để hạn chế số lần
  session chết.
- Khi phiên chết → đánh dấu `needsReharvest`, chờ extension gửi cookie mới.

### 2.11 Cookie tối thiểu

| Bộ cookie gửi tới `GET /app` | Session OK? |
|---|---|
| `1PSID` + `1PSIDTS` | ✅ |
| `1PSID` + `1PSIDTS` + `NID` | ✅ |
| `1PSID` + `1PSIDTS` + `3PSID` + `3PSIDTS` | ✅ |
| `1PSID` + `3PSIDTS` (thiếu `1PSIDTS`) | ❌ |
| `1PSID` một mình | ❌ |

→ 2 cookie auth là **đủ** cho `GET /app`. Nhưng vẫn nên giữ **full jar** trong pool:
đó là thứ cho phép `RotateCookies` hoạt động đúng và là nguồn dự phòng khi Google
yêu cầu `__Secure-3PSID` (Device Bound Session Credentials).

### 2.12 Extension là đường vào duy nhất

Chrome/Edge ≥ 127 mã hoá cookie trên đĩa bằng **App-Bound Encryption** (prefix
`v20`), gắn với tiến trình trình duyệt. Không process nào ngoài trình duyệt đọc
được — kể cả `Cookie-Editor` chạy ngoài. Extension chạy **bên trong** trình duyệt
nên dùng được `chrome.cookies`, tức chính browser giải mã giúp.

Đã verify trong dự án Python (`my-geminiweb-orchestrator/docs/COOKIE-BRIDGE.md`):
đọc DB cookie ngoài tiến trình ❌, DPAPI thuần ❌, copy profile rồi CDP ❌ (Chrome
xoá cookie do DBSC), extension ✅.

---

## 3. Kiến trúc

```
┌─────────────────────────────────────────────────────────┐
│  MCP Client (Cursor / Claude Code / VS Code / ...)     │
└───────────────────────┬─────────────────────────────────┘
                        │ stdio JSON-RPC
┌───────────────────────▼─────────────────────────────────┐
│  my-geminiweb-mcp.exe  (Native AOT, self-contained)      │
│                                                          │
│  Tools/GeminiTools.cs      gemini_chat                  │
│                            gemini_accounts               │
│                            gemini_check                  │
│                            gemini_refresh                │
│                            gemini_bridge                 │
│  Bridge/CookieBridge.cs   HTTP loopback :9478/harvest   │
│  Gemini/SessionKeepAlive.cs  giữ phiên sống nền         │
│  Accounts/AccountPool.cs   round-robin + failover       │
│  Gemini/GeminiWebClient.cs protocol client              │
│  Gemini/StreamingFrameParser.cs  length-prefixed frames  │
│  AppPaths.cs               %LOCALAPPDATA%\MyGeminiWebMcp │
└──────┬──────────────────────────────┬──────────────────┘
       │ stdio JSON-RPC                │ HTTP 127.0.0.1
       │                        ┌───────▼──────────────────┐
       │                        │ Chrome + extension       │
       │                        │ (đọc cookie bằng chính    │
       │                        │  browser, qua app-bound) │
       │                        └──────────────────────────┘
       │ HTTPS
┌──────▼─────────────────────────────────────────────────┐
│  gemini.google.com                                        │
└──────────────────────────────────────────────────────────┘
```

### 3.1 Cấu trúc thư mục

| File | Vai trò |
|---|---|
| `Program.cs` | Host + DI + đăng ký MCP tools + start bridge/keep-alive |
| `AppPaths.cs` | Resolve `%LOCALAPPDATA%\MyGeminiWebMcp` (override bằng `GEMINIWEB_MCP_HOME`) |
| `Gemini/GeminiWebClient.cs` | Init session + `StreamGenerate` + parse reply |
| `Gemini/StreamingFrameParser.cs` | Parse frame length-prefixed (UTF-16 units) |
| `Gemini/CookieJar.cs` | Cookie jar + absorb `Set-Cookie` |
| `Gemini/CookieRotator.cs` | Gọi `RotateCookies` (kéo dài phiên server-side) |
| `Gemini/SessionKeepAlive.cs` | Vòng lặp nền giữ phiên + persist cookie mới |
| `Gemini/GeminiSession.cs` | Record chứa token/build label/session id |
| `Accounts/AccountPool.cs` | Round-robin, cool-down, failover, upsert từ bridge |
| `Accounts/AccountConfig.cs` | Shape của 1 account trong `accounts.json` |
| `Bridge/CookieBridge.cs` | HTTP loopback nhận cookie từ extension |
| `Tools/GeminiTools.cs` | 5 MCP tool |
| `browser_extension/geminiweb-mcp-bridge/` | Extension Chrome/Edge đọc cookie + POST về bridge |

### 3.2 Cấu hình pool

```json
{
  "accounts": [
    {
      "name": "account1",
      "email": "user@gmail.com",
      "psid": "<__Secure-1PSID>",
      "psidts": "<__Secure-1PSIDTS>",
      "cookies": {
        "NID": "...",
        "__Secure-3PSID": "...",
        "__Secure-3PSIDTS": "..."
      },
      "lastRefreshed": "2026-11-15T13:30:42Z"
    }
  ]
}
```

Vị trí: `%LOCALAPPDATA%\MyGeminiWebMcp\accounts.json` (hoặc `GEMINIWEB_MCP_HOME`).

**Không** đọc cookie từ profile Chrome của user: Chrome ≥127 dùng app-bound encryption,
đọc ngoài tiến trình là bất khả thi (đã verify trong dự án Python — xem
`my-geminiweb-orchestrator/docs/COOKIE-BRIDGE.md`). Extension là cách hợp lệ duy nhất.

---

## 4. Chiến lược cookie cho người dùng NuGet

Ba lựa chọn đã cân nhắc:

| Cách | Trải nghiệm người dùng | Đánh giá |
|---|---|---|
| **A. Dán cookie thủ công** | Tệ — mở DevTools, copy 2 giá trị | Vẫn hỗ trợ (fallback) |
| **B. Extension trình duyệt** | Tốt — 1 click là xong | **Đã triển khai** |
| **C. Nhúng browser (Playwright)** | Nặng, AOT + Chromium = không khả thi | Loại |

**B đã triển khai** (`browser_extension/geminiweb-mcp-bridge/`):
- Extension đọc cookie `.google.com` qua `chrome.cookies`.
- POST tới `http://127.0.0.1:9478/harvest` kèm token.
- `CookieBridge` xác thực token, khớp account (theo tên, email, hoặc `1PSID`),
  ghi vào `accounts.json` (atomic).
- Chế độ "tự gửi mỗi khi mở tab Gemini" để người dùng không phải bấm tay.

### 4.1 Auto-heal

| Tình huống | Xử lý |
|---|---|
| `1PSIDTS` sắp hết hạn | `SessionKeepAlive` gọi `GET /app` + `RotateCookies` mỗi ~20 phút (có jitter) |
| Phiên chết (fail im lặng) | `InitializeAsync` phát hiện thiếu token → đánh dấu `needsReharvest`, cooldown 60 phút |
| Account hỏng tạm thời | Failover sang account khác, cooldown 10 phút |
| Tất cả account chết | Báo rõ, yêu cầu chạy extension gửi cookie mới |
| Harvest mới về | `Upsert` ghi đè cookie cũ + xoá cờ `needsReharvest` |

**Không** nhúng mật khẩu trong sản phẩm — người dùng dán cookie, không gửi mật khẩu.

---

## 5. Các phase tiếp theo

### Phase 1 — Cookie bridge + giữ phiên (đã xong 2026-11-15)
- [x] Port client C# — init + chat + parse frame
- [x] Pool + rotation + failover
- [x] 5 MCP tool (`gemini_chat`, `gemini_accounts`, `gemini_check`, `gemini_refresh`, `gemini_bridge`)
- [x] AOT publish + E2E verify
- [x] Browser extension (Chrome/Edge) đọc cookie + POST về bridge loopback
- [x] `CookieBridge` — nhận cookie, khớp account, ghi atomic vào `accounts.json`
- [x] `SessionKeepAlive` — vòng lặp nền giữ phiên + persist cookie mới
- [x] Phát hiện phiên chết (fail im lặng) + đánh dấu `needsReharvest` + cooldown 60 phút
- [ ] Upload file đính kèm (`content-push.googleapis.com/upload`, header `X-Tenant-Id: bard-storage`)
- [ ] Download file Gemini sinh ra (raw candidate field 59 → `contribution.usercontent.google.com/download`)
- [ ] Retry + backoff khi lỗi 1013 (temporary error)

### Phase 2 — Đóng gói NuGet
- [x] `PackageId` = `MyGeminiWebMcp`
- [x] README + `.mcp/server.json` manifest
- [ ] LICENSE + icon
- [ ] `dotnet pack` + publish lên nuget.org

### Phase 3 — Nâng cấp
- [ ] Model selection (Flash / Pro / Extended Thinking)
- [ ] Streaming output tăng dần (hiện chỉ trả reply cuối)
- [ ] Bridge tự tắt khi không dùng (idle timeout)

### Phase 4 — Deep Research (để sau, như yêu cầu)

---

## 6. Rủi ro còn lại

| Rủi ro | Mức độ | Ghi chú |
|---|---|---|
| Google đổi protocol JSPB | Cao | Đã xảy ra nhiều lần trong lịch sử `gemini_webapi`. Cần bám sát upstream |
| `1PSIDTS` ngắn hạn | Đã xử lý | Không tự xoay được server-side → extension harvest là nguồn duy nhất (mục 2.10) |
| Người dùng không cài extension | Trung bình | Vẫn hỗ trợ dán cookie thủ công; `gemini_check` hướng dẫn cụ thể |
| 429 / IP bị chặn | Thấp | Không giải pháp nào ngoài proxy |
| AOT + thư liện mới | Thấp | Đã xử lý JSON; phải giữ `JsonNode` thay vì reflection |

---

## 7. Bằng chứng

| Script | Nội dung |
|---|---|
| `my-geminiweb-orchestrator/reports/_tls_probe.py` | TLS impersonation có bắt buộc không |
| `my-geminiweb-orchestrator/reports/_ground_truth.py` | Capture chính xác request Python gửi đi |
| `my-geminiweb-orchestrator/reports/_frame_probe.py` | Đối chiếu quy tắc frame parser |
| `my-geminiweb-orchestrator/reports/_rotate_raw.py` | `Set-Cookie` thô của `RotateCookies` |
| `my-geminiweb-orchestrator/reports/_psidts_truth.py` | 1PSIDTS đổi hay không sau `GET /app` |
| `my-geminiweb-orchestrator/reports/_rotate_trigger.py` | `RotateCookies` có cấp 1PSIDTS mới không |
| `my-geminiweb-orchestrator/reports/_min_cookies.py` | Bộ cookie tối thiểu cho session |
| `my-geminiweb-orchestrator/reports/_mcp_e2e.py` | E2E: bridge token → harvest → 5 tool → chat |
| `my-geminiweb-orchestrator/reports/_mcp_e2e2.py` | E2E: full jar, multi-account, refresh |
| `my-geminiweb-orchestrator/reports/_mcp_e2e3.py` | E2E: phiên chết → needsReharvest → failover |
