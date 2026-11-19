# Troubleshooting — MyGeminiWebMcp

Bảng lỗi → nguyên nhân → cách xử lý. Đọc khi tool trả lỗi hoặc test fail.

---

## "Gemini returned no access token. The session cookies are probably expired or invalid."

**Đây là lỗi phổ biến nhất.** Session cookie hết hạn (thường vài giờ tới vài ngày).

**Nhận biết:** mọi account trả cùng lỗi này cùng lúc → cookie chết hàng loạt, không phải
lỗi từng account.

**Cách xử lý (theo thứ tự):**

1. Tool `gemini_refresh` — thử làm mới `__Secure-1PSIDTS` từ server side. Miễn phí, không
   tốn quota. Nếu account vẫn sống thì đây là cách duy nhất cần.
2. Nếu `gemini_refresh` cũng báo lỗi → cookie thật sự chết, phải harvest tay:
   - Tool `gemini_bridge` → lấy `endpoint` + `token`.
   - Mở `gemini.google.com` trong browser đã đăng nhập account đó.
   - Extension `geminiweb-mcp-bridge` → popup → dán endpoint + token → bấm **Gửi cookie**.
   - Tool `gemini_check` lại.
3. Nếu browser **không còn đăng nhập** account đó → phải đăng nhập lại thủ công. Đây là
   bước duy nhất cần con người; không có cách nào tự động hoá (không bypass CAPTCHA/2FA).

**Không làm:** copy cookie từ profile Chrome đang chạy sang profile khác ("Evil Clone") —
rủi ro logout toàn bộ Google. Đọc cookie ngoài tiến trình cũng không được (app-bound
encryption, prefix `v20`, từ Chrome ≥127).

---

## "Unknown model ..." / `error` + `availableModels` trong response

Server không resolve được tên model → trả về error kèm `availableModels`.

**Cách xử lý:** đọc `availableModels` trong response và dùng đúng tên đó, hoặc gọi lại tool
`gemini_models` để lấy danh sách mới. Tên model cũ đã bị Google thu hồi.

**Không đoán tên.** Google đã đổi `flash`→`hydrogen`, `carbon`→`argon`, `pro`→`neon` trong
vài ngày.

---

## Stream treo (chat không trả lời, chờ hàng phút)

Gần như luôn là **model bị khoá** (`selectable: false`): account thấy model trong picker
nhưng không được chọn. Google mở stream rồi không bao giờ gửi frame.

**Đã đo:** stream treo 901 giây trước khi bị cắt.

**Cách xử lý:** chỉ dùng model có `selectable: true` trong output của `gemini_models`.
Server đã chặn sẵn, nhưng nếu bạn tự gọi RPC thì phải tự kiểm tra.

---

## `servedModel` null hoặc thiếu

Đã xử lý ở server (commit `d68d36a`): `servedModel` luôn có mặt, và
`servedModelConfirmed` cho biết nó đọc được từ payload hay suy ra.

- `servedModelConfirmed: true` → Google tự echo tên model → khẳng định được trong báo cáo.
- `servedModelConfirmed: false` → server suy ra từ model đã yêu cầu → nói "model đã yêu
  cầu là X", **không** nói "X đã phục vụ".

**Nếu gặp `servedModel: null`:** đó là bản cũ chưa có fallback — cần build lại từ source
hoặc nâng lên 0.1.1-beta.

---

## `quotaNotice` có mặt trong response

Google đã hạ cấp model: quota của model đã yêu cầu hết, nên nó trả bằng model rẻ hơn.

**Cách xử lý:** báo lại cho user (họ cần biết allowance đang cạn), và cân nhắc thử model
khác từ `gemini_models`.

---

## Tool trả `"An error occurred invoking 'gemini_xxx'."`

Đây là **thông báo chung** của MCP SDK khi handler ném exception — message thật nằm ở
**stderr của server**, không phải response.

**Cách xử lý:** đọc stderr. Khi chạy server thủ công:

```powershell
my-geminiweb-mcp.exe 2> server-stderr.log
```

Nếu chạy qua MCP client, đọc log của client (Cursor: Developer Tools → Output → MCP).

**Lưu ý:** server được viết để **không** ném exception cho các lỗi nghiệp vụ — chúng được
trả về dạng JSON (`error` + dữ liệu liên quan). Chỉ lỗi thật sự mới ném.

---

## Bridge không nhận được cookie (`POST /harvest` bị từ chối)

1. **Sai port.** Server dời port khi `9478` bị chiếm. Lấy endpoint từ tool `gemini_bridge`
   — không đoán, không đọc `bridge-stderr.log` (có thể là log cũ).
2. **Sai token.** Token lưu ở `bridge_token.txt`, tự tạo lần đầu và giữ nguyên sau đó.
3. **Hai instance tranh port.** Không chạy `--bridge-only` song song với MCP server.
4. **Extension chưa có quyền.** Kiểm tra `chrome://extensions` → extension có bật
   `alarms` không (cho auto-sync 20 phút).

---

## Auto-sync không hoạt động

- Extension v1.4.0 dùng `chrome.alarms` → Chrome có thể **disable** alarm cho extension khi
  trình duyệt chạy lâu. Kiểm tra `chrome://extensions` → `Service worker` / `alarms`.
- Cần bật tuỳ chọn "Tự gửi khi mở tab" + "đồng bộ ngầm mỗi 20 phút" trong popup.
- Chrome đã đóng → không ai harvest được. Đó là lý do session chết khi bạn để máy qua đêm.

---

## Test script fail dù server vẫn chạy

`my-geminiweb-mcp/tests/verify_served_model.py` cần binary đã build:

```
my-geminiweb-mcp/bin/Release/net10.0/win-x64/publish/my-geminiweb-mcp.exe
```

Build lại: `dotnet publish my-geminiweb-mcp/my-geminiweb-mcp.csproj -c Release -r win-x64`

**Lưu ý:** script tự discover danh sách model từ `gemini_models`, nên không hardcode tên.
Nếu bạn sửa script và hardcode tên model → test fail vì lý do sai.

---

## "The process cannot access the file ... .nupkg because it is being used by another process"

Không phải lỗi NuGet — tiến trình `dotnet` còn treo giữ file cache.

```powershell
Get-Process dotnet | Stop-Process -Force
Remove-Item -Recurse -Force "$env:USERPROFILE\.nuget\packages\mygeminiwebmcp.win-x64\<version>"
```

Rồi cài lại. Cũng xảy ra khi bạn vừa `dotnet tool install` xong lại install lần nữa.

---

## "Version X of package mygeminiwebmcp.win-x64 is not found in NuGet feeds"

nuget.org index **bất đồng bộ 10–20 phút** sau khi push. Version đã tồn tại trên server
nhưng chưa hiện trong flatcontainer.

**Cách xử lý:** chờ rồi thử lại. **Đừng push lại** — version trùng sẽ bị từ chối.

Kiểm tra nhanh:

```powershell
Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/mygeminiwebmcp/index.json" -UseBasicParsing
```

---

## Cài tool trên máy không có RID đã publish

Đã publish: `win-x64`, `osx-arm64`, `linux-x64`.
Chưa publish: `win-arm64`, `linux-arm64`, `linux-musl-x64`.

Lỗi: `Package ... is not found in NuGet feeds` cho đúng RID đó. Chạy trên máy đó sẽ không
cài được tool.
