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
| `my-geminiweb-mcp.exe` (AOT, single-file, win-x64) | **15.7 MB** |

Không kèm .NET runtime → cài đặt cho người dùng chỉ là 1 file exe.

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
│  Accounts/AccountPool.cs   round-robin + failover       │
│  Gemini/GeminiWebClient.cs protocol client              │
│  Gemini/StreamingFrameParser.cs  length-prefixed frames  │
│  AppPaths.cs               %LOCALAPPDATA%\MyGeminiWebMcp │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTPS
┌───────────────────────▼─────────────────────────────────┐
│  gemini.google.com                                        │
└──────────────────────────────────────────────────────────┘
```

### 3.1 Cấu trúc thư mục

| File | Vai trò |
|---|---|
| `Program.cs` | Host + DI + đăng ký MCP tools |
| `AppPaths.cs` | Resolve `%LOCALAPPDATA%\MyGeminiWebMcp` (override bằng `GEMINIWEB_MCP_HOME`) |
| `Gemini/GeminiWebClient.cs` | Init session + `StreamGenerate` + parse reply |
| `Gemini/StreamingFrameParser.cs` | Parse frame length-prefixed (UTF-16 units) |
| `Gemini/GeminiSession.cs` | Record chứa token/build label/session id |
| `Accounts/AccountPool.cs` | Round-robin, cool-down 10 phút, auto-failover |
| `Accounts/PoolConfig.cs` | Shape của `accounts.json` |
| `Tools/GeminiTools.cs` | 3 MCP tool |

### 3.2 Cấu hình pool

```json
{
  "accounts": [
    {
      "name": "account1",
      "email": "user@gmail.com",
      "psid": "<__Secure-1PSID>",
      "psidts": "<__Secure-1PSIDTS>"
    }
  ]
}
```

Vị trí: `%LOCALAPPDATA%\MyGeminiWebMcp\accounts.json` (hoặc `GEMINIWEB_MCP_HOME`).

**Không** đọc cookie từ profile Chrome của user: Chrome ≥127 dùng app-bound encryption,
đọc ngoài tiến trình là bất khả thi (đã verify trong dự án Python — xem
`my-geminiweb-orchestrator/docs/COOKIE-BRIDGE.md`).

---

## 4. Chiến lược cookie cho người dùng NuGet

Đây là **quyết định sản phẩm** quan trọng nhất còn lại. Ba lựa chọn:

| Cách | Trải nghiệm người dùng | Đánh giá |
|---|---|---|
| **A. Dán cookie thủ công** | Tệ — người dùng phải mở DevTools, copy 2 giá trị | Chấp nhận được ở v1, không mở rộng được |
| **B. Extension trình duyệt** (đã có sẵn `browser_extension/orchestrator-bridge/`) | Tốt — 1 click là xong | **Khuyến nghị** |
| **C. Nhúng browser (Playwright)** | Tốt nhưng nặng, AOT + Chromium = không khả thi | Loại |

→ Kế hoạch: **A cho v1** (đủ để publish), **B là bước nâng cấp tự nhiên** vì code
extension đã có sẵn và chạy được.

### 4.1 Auto-heal

| Tình huống | Xử lý |
|---|---|
| `1PSIDTS` hết hạn (thường xuyên) | Rotation tự động: `accounts.google.com/RotateCookies` |
| Session chết (`UNAUTHENTICATED`) | Failover sang account khác, account chết bị cooldown 10 phút |
| Tất cả account chết | Báo rõ, yêu cầu user cấp cookie mới |

**Không** nhúng mật khẩu trong sản phẩm — người dùng dán cookie, không gửi mật khẩu.

---

## 5. Các phase tiếp theo

### Phase 1 — Hoàn thiện chat (đang làm)
- [x] Port client C# — init + chat + parse frame
- [x] Pool + rotation + failover
- [x] 3 MCP tool
- [x] AOT publish + E2E verify
- [ ] Upload file đính kèm (`content-push.googleapis.com/upload`, header `X-Tenant-Id: bard-storage`)
- [ ] Download file Gemini sinh ra (raw candidate field 59 → `contribution.usercontent.google.com/download`)
- [ ] `1PSIDTS` rotation tự động
- [ ] Retry + backoff khi lỗi 1013 (temporary error)

### Phase 2 — Đóng gói NuGet
- [ ] `PackageId` = `MyGeminiWebMcp` (hiện còn là `SampleMcpServer`)
- [ ] README + LICENSE + icon
- [ ] `dotnet pack` + publish lên nuget.org
- [ ] `.mcp/server.json` manifest hoàn chỉnh

### Phase 3 — Nâng cấp
- [ ] Extension trình duyệt đồng bộ cookie
- [ ] Model selection (Flash / Pro / Extended Thinking)
- [ ] Streaming output tăng dần (hiện chỉ trả reply cuối)

### Phase 4 — Deep Research (để sau, như yêu cầu)

---

## 6. Rủi ro còn lại

| Rủi ro | Mức độ | Ghi chú |
|---|---|---|
| Google đổi protocol JSPB | Cao | Đã xảy ra nhiều lần trong lịch sử `gemini_webapi`. Cần bám sát upstream |
| `1PSIDTS` ngắn hạn | Trung bình | Đã có cơ chế rotation trong Python, cần port sang C# |
| 429 / IP bị chặn | Thấp | Không giải pháp nào ngoài proxy |
| AOT + thư viện mới | Thấp | Đã xử lý JSON; phải giữ `JsonNode` thay vì reflection |

---

## 7. Bằng chứng

| Script | Nội dung |
|---|---|
| `my-geminiweb-orchestrator/reports/_tls_probe.py` | TLS impersonation có bắt buộc không |
| `my-geminiweb-orchestrator/reports/_ground_truth.py` | Capture chính xác request Python gửi đi |
| `my-geminiweb-orchestrator/reports/_frame_probe.py` | Đối chiếu quy tắc frame parser |
| `my-geminiweb-orchestrator/reports/_mcp_e2e_test.py` | E2E: accounts → check → chat 2 lượt |
| `my-geminiweb-orchestrator/reports/_mcp_e2e_test2.py` | E2E: 3 lượt, rotation, pin, failover, error handling |
