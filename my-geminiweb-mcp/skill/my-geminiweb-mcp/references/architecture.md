# Architecture — MyGeminiWebMcp

Bản đồ để **đọc code** nhanh khi cần sửa hoặc debug. Không cần đọc file này khi chỉ dùng
tool.

Source: `D:\GitHub\my-gemini-pro\my-geminiweb-mcp\my-geminiweb-mcp\`

```
my-geminiweb-mcp/
├── Program.cs              # Host, DI, --bridge-only, MCP stdio server
├── AppPaths.cs             # Đường dẫn (pool file, bridge token, downloads)
├── Accounts/
│   ├── AccountConfig.cs    # name / email / psid / psidts / extraCookies
│   └── AccountPool.cs      # Load, Next(), Status(), MarkHealthy/Failed/AuthFailed
├── Bridge/
│   ├── CookieBridge.cs     # HTTP listener loopback, POST /harvest, token auth
│   └── AccountEmailResolver.cs
├── Gemini/
│   ├── GeminiWebClient.cs  # Core client: bootstrap, StreamGenerate, model header
│   ├── ModelRegistry.cs    # Runtime model discovery từ GET_USER_STATUS
│   ├── GeminiModels.cs     # GeminiModel record + aliases
│   ├── StreamingFrameParser.cs
│   ├── CookieJar.cs
│   ├── CookieRotator.cs
│   ├── SessionKeepAlive.cs
│   └── FileTransfer.cs     # Upload attachment / download file Gemini sinh ra
└── Tools/
    └── GeminiTools.cs      # 6 MCP tool: chat / refresh / bridge / accounts / models / check
```

---

## Luồng một lượt chat

```
gemini_chat(prompt)
  └─ GeminiTools.Chat
       ├─ ParseConversation(conversation)   → handle {account, cid, rid}
       ├─ Chọn account:
       │    handle có account  → pin vào account đó (không rotate)
       │    account param     → pin cứng, chết thì ném lỗi
       │    không có          → pool.Next() (xoay vòng + failover)
       ├─ new GeminiWebClient(psid, psidts, extraCookies)
       ├─ client.InitializeAsync()          → GET /app, lấy SNlM0e token, GET_USER_STATUS
       │     └─ ModelRegistry.ApplyUserStatus(body)  → danh sách model + capacity tier
       ├─ TryRotateAsync(client, account)   → RotateCookiesAsync, best-effort
       ├─ client.GenerateAsync(prompt, metadata, model, attachments, extendedThinking, downloads)
       │     ├─ ResolveModelHeaderAsync(model)   → header 81 slot
       │     ├─ POST StreamGenerate (batchexecute)
       │     ├─ StreamingFrameParser → text / thoughts / files
       │     └─ ExtractServedModel(payload)      → servedModel + servedModelConfirmed
       ├─ pool.MarkHealthy / PersistCookies
       └─ Trả JSON: account, reply, conversation, servedModel, ...
```

**Điểm quan trọng:** `InitializeAsync` **không** chỉ khởi tạo session — nó gọi
`GET_USER_STATUS` (`otAQ7b`), và đó là nơi danh sách model đến. Không có request nào
khác lấy được danh sách model.

---

## RPC và endpoint

| Mục đích | RPC / URL |
|---|---|
| Bootstrap session | `GET https://gemini.google.com/app` (lấy `SNlM0e` tokens) |
| Danh sách model + tier | `GET_USER_STATUS`, rpc id `otAQ7b` (qua `batchexecute`) |
| Chat | `StreamGenerate` — `assistant.lamda.BardFrontendService/StreamGenerate` |
| Batch execute | `POST https://gemini.google.com/_/BardChatUi/data/batchexecute` |
| Xoay cookie | `accounts.google.com/RotateCookies` |
| Upload file | `content-push.googleapis.com/upload` |

---

## Header model (81 slot)

Gửi kèm header `x-goog-ext-525001261-jspb`. Cấu trúc:

- slot 0: `cid` (conversation id)
- slot 1: `rid` (reply id)
- slot 2: `rcid`
- slot 3: model id (hex 16 ký tự, vd `e6fa609c3fa255c0`)
- slot 4: capacity tier
- slot 5: (trống)
- slot 6: **model id lặp lại** ở vị trí khác
- slot 7/8: chuỗi rỗng
- slot 9: model number
- slot 10: capacity field
- slot 11: display label (vd `3.1 Pro`)

Sai slot → Google **không** báo lỗi, chỉ mở stream rồi im lặng. Đây là lý do phải
discover model runtime thay vì hardcode id.

---

## Timeout (vì sao stream treo thành lỗi thay vì treo vô hạn)

| Timeout | Giá trị | Bắt được trường hợp |
|---|---|---|
| `StreamIdleTimeout` | 120 s | Model id cũ → stream mở nhưng không gửi frame nào |
| `TurnTimeout` | 300 s | Model bị khoá → Google giữ stream + gửi keep-alive, không bao giờ idle |

Đã đo: request model `4 Carbon` bị khoá giữ stream **901 giây**. Turn timeout đổi cái treo
vô hạn thành lỗi retryable.

---

## Model discovery (chi tiết)

`ModelRegistry.ApplyUserStatus(body)`:

- `body[15]` = danh sách model
- `body[16]` = tier flags → capacity tier
- `body[17]` = capability flags → capacity tier
- `body[24]` (policy entry) = **launch lineup** hiện tại: tên model mới theo model number

Mỗi model trong `body[15]`:

| Slot | Ý nghĩa |
|---|---|
| 0 | model id (hex) |
| 1 / 10 | category (code name) |
| 2 / 12 | description (vd `3.1 Pro`) |
| 4 | capacity riêng của model (ghi đè tier account) |
| 6 | **mảng id của tier được dùng** — `null` ⇒ account KHÔNG chọn được model này |
| 9 / 17 | model number |
| 11 / 19 | display label |

**`slot 6` là nguồn của `selectable: false`.** Không có slot này thì code sẽ coi mọi
model là chọn được và request sẽ treo.

### Capacity tier (quyết định request trừ vào allowance nào)

`ModelRegistry.ComputeCapacity(tierFlags, capabilityFlags)`:

| Điều kiện | Capacity | Field |
|---|---|---|
| tier có 21 | 1 | 13 |
| tier có 22 | 2 | 13 |
| capability có 115 | 4 | 12 (Plus) |
| tier có 16 hoặc capability có 106 | 3 | 12 (Pro) |
| tier có 8 hoặc capability có 19 | 2 | 12 (Advanced) |
| mặc định | 1 | 12 (Free) |

Sai tier ⇒ request trừ nhầm allowance.

### Tên model và alias

Tên do Google đặt, thay đổi không báo trước:

| Ngày | Thay đổi |
|---|---|
| 2026-11-16 | `flash` → `hydrogen` |
| 2026-11-18 | `carbon` → `argon` |
| 2026-11-19 | `pro` → `neon`, `flash-lite` → `tự-động` |

`DeriveAliases` sinh ra nhiều cách viết cho cùng một model: code name, UI label, mô tả,
hex id, bản bỏ số phiên bản (`4 Hydrogen` → `hydrogen`), và tên trong launch lineup.
`Resolve` thử: hex id → tên chính xác → alias → substring duy nhất.

---

## `servedModel` (vì sao không bao giờ null)

`ExtractServedModel` duyệt payload tìm model id (hex 16 ký tự) rồi đọc display label ở
slot +5. Nó đệ quy vào JSON string lồng nhau, sâu tối đa 12.

Google **không phải lúc nào cũng** echo model id (reply ngắn, lượt chỉ có thinking →
thường không có). Chuỗi fallback trong `GenerateAsync`:

```
payload match  →  tên model đã yêu cầu  (servedModelConfirmed = false)
     ↓ không có
account default (nếu có fallbackReason) → requested model description
```

`servedModelConfirmed = true` **chỉ** khi đọc được trực tiếp từ payload.

---

## Bridge (cookie hand-off)

```
CookieBridge
  ├─ Start(port) → bind 127.0.0.1, tự dời port nếu bận, giữ token trong bridge_token.txt
  ├─ POST /harvest  → nhận {token, account, email, psid, psidts, cookies, sessions}
  │     └─ pool.UpdateCookies(account, psidts, extras)
  └─ GET /ping      → kiểm tra sống
```

Chỉ bind loopback, token bắt buộc. Không ghi log cookie.

---

## Keep-alive

`SessionKeepAlive` chạy nền, mỗi ~20 phút (có jitter) refresh `__Secure-1PSIDTS` cho mọi
account qua `RotateCookiesAsync`. Nó đồng thời là cơ chế hồi sinh pool mà không cần mở
browser.

---

## Build & publish

```powershell
# build local (Native AOT, không cần runtime)
dotnet publish my-geminiweb-mcp/my-geminiweb-mcp.csproj -c Release -r win-x64

# pack
dotnet pack my-geminiweb-mcp/my-geminiweb-mcp.csproj -c Release
```

CI: `.github/workflows/nuget-publish.yml` — matrix RID (`win-x64` trên windows-latest,
`linux-x64` trên ubuntu, `osx-arm64` trên macos), OIDC Trusted Publishing, không có
API key dài hạn. `RuntimeIdentifiers` trong `.csproj` phải khớp đúng các RID thực sự
được publish, nếu không `dotnet tool install` fail trên máy không có RID đó.

Native AOT **không cross-compile**: build Windows phải chạy trên Windows, build macOS phải
chạy trên macOS.

---

## Test

| Script | Kiểm tra gì |
|---|---|
| `tests/verify_served_model.py` | Mọi reply đều có `servedModel` + `servedModelConfirmed` |
| `tests/verify_locked_model.py` | Model `selectable: false` bị từ chối, không treo |
| `tests/verify_mcp_stdio.py` | Server stdio khởi tạo và list tool |
| `tests/verify_packaged_tool.py` | `.nupkg` cài được và trả lời MCP |
| `tests/e2e_chat_all.py` | Chat trên mọi account |
| `tests/e2e_multi_account.py` | Failover, rotation |
| `tests/e2e_dead_session.py` | Xử lý session chết |
| `tests/e2e_extension.py` | Extension hand-off cookie |
| `tests/e2e_bridge_security.py` | Bridge chặn sai token / endpoint sai |

Tất cả script tự discover danh sách model từ `gemini_models` — **đừng hardcode tên model**
trong test, vì Google đổi tên không báo trước và test sẽ fail vì lý do sai.
