---
name: my-geminiweb-mcp
description: "Chat and research with Gemini on gemini.google.com through the paid Google AI Pro subscription, using the MyGeminiWebMcp MCP server (C#/.NET Native AOT). Use this skill whenever the user wants to talk to Gemini, test Gemini web chat, check multi-account pool health, send or receive attachments, discover which models an account is offered, harvest cookies via the browser extension, install the tool from NuGet, or when a Gemini call returns an unknown-model error, a null servedModel, a dead session, or a 'no access token' error. Triggers include: 'gemini chat', 'gemini_check', 'gemini_accounts', 'gemini_models', 'my-geminiweb-mcp', 'geminiweb', 'thu hoi cookie', 'harvest cookie', 'test gemini web', 'pool gemini', 'cai mygeminiwebmcp', 'gemini pro allowance'."
metadata:
  author: kimvi
  version: "2.0.0"
---

# MyGeminiWebMcp — Gemini Web qua gói AI Pro đã trả

MCP server C# .NET 10 Native AOT (`MyGeminiWebMcp`, ~16 MB single file): chat Gemini Web
bằng cookie `__Secure-1PSID` / `__Secure-1PSIDTS`, multi-account pool, failover tự động,
giữ hội thoại liên tục, tự refresh session. **Không** dùng AI Studio Interactions API —
billing riêng, không nằm trong gói AI Pro (đã verify: HTTP 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT`).

Repo: <https://github.com/CatKod/my-gemini-pro> — thư mục `my-geminiweb-mcp/`.
NuGet: <https://www.nuget.org/packages/MyGeminiWebMcp> (bản hiện tại: **0.1.1-beta**).

---

## Bắt đầu phiên mới: 4 bước, 60 giây

Đừng đoán trạng thái pool — hãy hỏi server.

1. **Cài MCP server** (chỉ làm 1 lần cho mỗi máy):

   ```jsonc
   // mcp.json  — hoặc dùng lệnh `dnx` bên dưới
   {
     "servers": {
       "geminiweb": {
         "type": "stdio",
         "command": "dnx",
         "args": ["MyGeminiWebMcp", "--version", "0.1.1-beta", "--yes"]
       }
     }
   }
   ```

   Hoặc cài cố định: `dotnet tool install -g MyGeminiWebMcp --version 0.1.1-beta`

2. **Kiểm tra pool** — tool `gemini_check`. Trả về `ok` / `needsReharvest` cho từng account.

3. **Nếu có account `ok:false`** → dùng tool `gemini_refresh` thử 1 lần. Vẫn chết thì
   cần cookie mới: xem "Thu hồi cookie" bên dưới.

4. **Liệt kê model** — tool `gemini_models`. **Luôn gọi tool này trước khi chọn model**
   (xem "Model: đừng hardcode" bên dưới).

---

## 6 tool (không phải 5)

| Tool | Dùng để | Trả về |
|---|---|---|
| `gemini_chat` | Gửi tin nhắn, nhận reply | `account`, `reply`, `conversation`, `servedModel`, `servedModelConfirmed`, `quotaNotice?`, `fallbackReason?`, `files?` |
| `gemini_models` | Liệt kê model account được cấp | `models[]` (kèm `selectable`, `currentName`, `aliases`), `unselectable[]` |
| `gemini_accounts` | Liệt kê account + sức khỏe | `poolFile`, `count`, `accounts[]` |
| `gemini_check` | Test từng account có tới được Gemini không | `results[]` (`ok`, `language`, `buildLabel` hoặc `error`) |
| `gemini_refresh` | Refresh `__Secure-1PSIDTS` (giữ session sống) | `results[]` (`refreshed`, `rotated`, `needsReharvest?`) |
| `gemini_bridge` | Endpoint + token cho browser extension | `running`, `endpoint`, `token`, `poolFile` |

### `gemini_chat` — tham số

```jsonc
{
  "prompt": "Giải thích đoạn code này",
  "account": "kimperiod0126",  // optional: pin cứng — chết thì báo lỗi, KHÔNG tự đổi acc
  "conversation": "{...}",      // optional: handle từ reply trước, để nối tiếp hội thoại
  "model": "hydrogen",         // optional: tên lấy từ gemini_models, hoặc "default"
  "attachments": ["./a.py"],   // optional: đường dẫn file local
  "extendedThinking": false
}
```

Reply (rút gọn):

```jsonc
{
  "account": "kimperiod0126",
  "reply": "...",
  "conversation": "{\"account\":\"...\",\"cid\":\"c_...\",\"rid\":\"r_...\"}",
  "servedModel": "4 Hydrogen",
  "servedModelConfirmed": true,
  "files": [{ "fileName": "report.xlsx", "savedPath": ".../downloads/report.xlsx",
              "size": 1234, "mimeType": "...", "source": "blob" }]
}
```

**Quy tắc sử dụng:**

- **Giữ `conversation`.** Bỏ nó là mất context — muốn nói tiếp phải gửi lại handle.
- **Pin là nghiêm ngặt.** Có `account` thì chỉ dùng account đó; chết thì báo lỗi rõ ràng,
  không tự chuyển sang account khác (nếu không sẽ báo "thành công" cho sai account).
- **Model phải lấy từ `gemini_models`.** Xem mục dưới.
- **Đọc `servedModelConfirmed`.** `true` = Google tự echo tên model; `false` = server suy
  ra từ model đã yêu cầu. Khi `false`, đừng khẳng định trong báo cáo rằng model đó thực sự
  đã phục vụ.
- **Đọc `quotaNotice`.** Có nghĩa là Google đã hạ cấp model (thường vì hết quota của model
  đó) — đây là lý do "câu trả lời ngắn bất thường", và là thông tin cần báo lại cho user.
- **`files[].savedPath`** — file Gemini sinh ra đã nằm trên đĩa, dùng đường dẫn đó.

---

## Model: đừng hardcode — Google đổi lineup không báo trước

Đây là nguồn lỗi lớn nhất của lane này. Google đổi tên/đổi số model **không cần báo trước** —
ngày 2026-11-16 `flash` đã thành `hydrogen`, ngày 2026-11-18 `carbon` thành `argon`, và
"pro" thành "neon". Một bảng model hardcode sẽ hỏng âm thầm: id cũ làm Google mở một
stream **không bao giờ có câu trả lời** (đã verify: stream treo 901 giây).

**Cách đúng duy nhất:** gọi `gemini_models` rồi dùng đúng tên trả về.

```jsonc
// gemini_models trả về
{ "models": [
    { "name": "gemini-tự-động", "displayName": "Tự động",
      "description": "Thích ứng với nhu cầu của bạn",
      "selectable": true,  "currentName": null, "aliases": [...] },
    { "name": "gemini-4-hydrogen", "displayName": "4 Hydrogen", "selectable": true },
    { "name": "gemini-neon", "displayName": "4 Neon", "selectable": true },
    { "name": "gemini-carbon", "displayName": "4 Carbon", "selectable": false }
  ],
  "unselectable": ["4 Carbon"] }
```

- `selectable: false` → account **thấy** model nhưng **không được chọn**. Chọn nó = stream
  treo vô hạn. Server đã tự chặn, nhưng nếu tự gọi RPC thì phải tự kiểm tra.
- `name` là tên canonical (`gemini-<slug>`) — dùng tên này cho tham số `model`.
- Server cũng chấp nhận: tên code (`hydrogen`), nhãn UI (`4 Hydrogen`), mô tả (`3.1 Pro`),
  hex id, và `default` (để Google tự chọn). Nếu tên lạ → server trả lỗi kèm
  `availableModels` — đọc field đó, đừng đoán.

**Một account có thể không có model bạn đợi.** Account của một người dùng khác có thể bị
chặn (`4 Carbon` là ví dụ đã gặp). Kiểm tra `selectable` trước khi hứa kết quả.

---

## Thu hồi cookie (khi account chết)

Cookie Chrome/Edge dùng **app-bound encryption** (prefix `v20`) từ Chrome ≥127 —
**không** đọc được từ tiến trình ngoài. Đường duy nhất là browser extension.

1. Gọi tool `gemini_bridge` → lấy `endpoint` + `token`.
2. Extension: `my-geminiweb-mcp/browser_extension/geminiweb-mcp-bridge/`
   (`chrome://extensions` → Developer mode → Load unpacked).
3. Mở popup extension → dán endpoint + token → bấm **Gửi cookie Gemini** trên tab
   `gemini.google.com` đã đăng nhập.
4. Extension **tự đồng bộ ngầm mỗi 20 phút** (`chrome.alarms`) — không cần tab Gemini
   mở, không cần bấm popup. Bật tuỳ chọn "Tự gửi khi mở tab" + "đồng bộ ngầm mỗi 20 phút"
   để pool tự hồi phục.

### Port bridge — lấy từ `gemini_bridge`, đừng đoán

- Mặc định `9478`, **nhưng** server tự dời sang `9479`, `9480`… nếu `9478` đã bị chiếm
  (instance `--bridge-only` cũ, hay MCP client khác).
- **Nguồn chuẩn duy nhất:** output của tool `gemini_bridge` (`endpoint`) và
  `bridge_config.json`. `bridge-stderr.log` có thể ghi endpoint cũ — không dùng làm chuẩn.
- Extension tự dò port (`portCandidates`), nên không cần sửa tay.
- Nếu `POST /harvest` bị từ chối mà server vẫn `running: true` → kiểm tra đang POST đúng
  port trong `gemini_bridge` chưa.

### `--bridge-only` — chế độ chạy nền

```powershell
my-geminiweb-mcp.exe --bridge-only
```

MCP server nói chuyện qua stdio nên sống cùng client: nó chết khi client đóng stdin.
Extension thì cần một bridge **luôn có mặt**. `--bridge-only` giải quyết đúng việc đó.

⚠️ **Không chạy `--bridge-only` song song với MCP server** — hai tiến trình tranh nhau
port, extension sẽ gửi nhầm port. Chỉ chạy một instance.

### Test thu hồi cookie bằng tay (idempotent, không phá pool)

```powershell
$body = @{ token="<token>"; account="<name>"; email="<email>";
  psid="<psid>"; psidts="<psidts>"; cookies=@{};
  sessions=@(); source="manual-test" } | ConvertTo-Json -Depth 5
Invoke-WebRequest -Uri "<endpoint>" -Method POST -Body $body `
  -ContentType "application/json" -UseBasicParsing
```

Pass khi: HTTP 200 + `ok:true` + `hasPsidts:true`; pool file vẫn đúng số account,
`lastRefreshed` của account đó được cập nhật.

---

## Cấu hình

| Biến | Ý nghĩa |
|---|---|
| `GEMINIWEB_MCP_HOME` | Thư mục chứa `accounts.json` (mặc định `%LOCALAPPDATA%\MyGeminiWebMcp`) |
| `GEMINIWEB_MCP_BRIDGE_PORT` | Port bridge (mặc định `9478`) |

Pool file (`%LOCALAPPDATA%\MyGeminiWebMcp\accounts.json`):

```json
{ "accounts": [{ "name": "acc1", "email": "you@gmail.com",
  "psid": "<__Secure-1PSID>", "psidts": "<__Secure-1PSIDTS>" }] }
```

File Gemini sinh ra nằm ở `%LOCALAPPDATA%\MyGeminiWebMcp\downloads\`.

---

## Cài từ NuGet (đã publish, Trusted Publishing OIDC)

| Package | Version hiện tại |
|---|---|
| `MyGeminiWebMcp` (tool metapackage) | 0.1.0-beta, 0.1.1-beta |
| `MyGeminiWebMcp.win-x64` | 0.1.0-beta, 0.1.1-beta |
| `MyGeminiWebMcp.osx-arm64` | 0.1.0-beta, 0.1.1-beta |
| `MyGeminiWebMcp.linux-x64` | 0.1.0-beta, 0.1.1-beta |

```bash
dotnet tool install -g MyGeminiWebMcp --version 0.1.1-beta
# hoặc local manifest
dotnet new tool-manifest
dotnet tool install MyGeminiWebMcp --version 0.1.1-beta
```

RID đã publish: `win-x64`, `osx-arm64`, `linux-x64`. **Chưa có** `win-arm64`,
`linux-arm64`, `linux-musl-x64` — cài trên các máy đó sẽ lỗi "package not found".

Bản đã publish dùng **Trusted Publishing (OIDC)** trong GitHub Actions — không có
`NUGET_API_KEY` dài hạn. Workflow: `.github/workflows/nuget-publish.yml`, trigger push tag
`v*`. Đẩy RID packages trước → chờ ~90s cho nuget.org index → đẩy tool package.

**Lưu ý vận hành:**
- nuget.org index **bất đồng bộ ~10–20 phút**. Thấy "version not found" ngay sau khi push
  là bình thường — đừng push lại.
- `dotnet tool install` kẹt file lock (`...nupkg because it is being used by another
  process`) là do tiến trình `dotnet` treo. Kill `dotnet` rồi xoá thư mục cache trong
  `%USERPROFILE%\.nuget\packages\` rồi cài lại.

---

## Kịch bản test nhanh (máy mới)

1. `gemini_accounts` — pool có bao nhiêu account.
2. `gemini_check` — account nào `ok`, account nào `needsReharvest`.
3. `gemini_models` — liệt kê model lấy từ server (không hardcode).
4. `gemini_chat` 2 lượt: lượt 1 lấy `conversation`, lượt 2 gửi lại handle
   ("Tên tôi là gì?") — pass khi nhớ đúng context.
5. `gemini_chat` kèm `attachments` (file `.txt`/`.py` nhỏ) — pass khi Gemini đọc đúng
   nội dung file.
6. `gemini_chat` với `model` lấy từ bước 3 — kiểm tra `servedModel` có mặt và
   `servedModelConfirmed` có mặt.
7. Account `needsReharvest` → mở tab Gemini account đó → bấm Gửi cookie → chạy lại
   `gemini_check`.

---

## Hai pool, hai file — phải đồng bộ

Đây là **nguyên nhân gốc** của hiện tượng "chat treo / `no access token`" (đã chẩn đoán
2026-11-18):

| Hệ thống | Nơi lưu cookie |
|---|---|
| MCP server C# | `%LOCALAPPDATA%\MyGeminiWebMcp\accounts.json` |
| Orchestrator Python | `my-geminiweb-orchestrator/pools/<name>.json` |

Hai file này **không tự liên kết**. Khi orchestrator harvest lại cookie, MCP server vẫn giữ
cookie cũ → mọi request trả `no access token`, dù trên web vẫn đăng nhập được.

**Sau khi harvest bằng orchestrator, phải đồng bộ sang MCP server:**

```powershell
cd my-geminiweb-orchestrator
python -m orchestrator.orch_cli mcp-sync --apply
```

Không có `--apply` thì lệnh chỉ báo cáo khác biệt, không ghi gì. Sau khi đồng bộ, chạy lại
`gemini_check` — kết quả phải là `ok: true` cho các account vừa harvest.

**Hiện trạng đã kiểm chứng (2026-11-19):** 4 account trùng tên giữa hai pool
(`kimperiod0126`, `meobo930`, `reigin483`, `yarho3155`) → đã đồng bộ.

---

## Giới hạn đã biết

- **Cookie app-bound** — không đọc ngoài tiến trình được; bắt buộc qua extension.
- **Model là per-account, per-build** — Google đổi không báo. Đã có fallback về default,
  nhưng cách duy nhất đáng tin là `gemini_models`.
- **Model bị khoá** (`selectable: false`) — account thấy nhưng không chọn được; chọn là
  treo stream.
- **Bản C# hiện chưa có Deep Research.** `gemini_dr_plan` / `gemini_dr_research` chỉ có
  trong orchestrator Python (`my-geminiweb-orchestrator/tools/gemini_dr_mcp.py`, đã archive).
  Nếu cần Deep Research, dùng orchestrator Python hoặc port sang C#.
- **Extension auto-sync 20 phút** nên pool tự hồi phục khi Chrome còn chạy. Session chết
  trước khi auto-sync kịp chạy thì cần harvest tay.

---

## Tham chiếu thêm

- `references/troubleshooting.md` — bảng lỗi → nguyên nhân → cách xử lý
- `references/architecture.md` — bản đồ code C#, RPC, và luồng xử lý chat
- Repo: `D:\GitHub\my-gemini-pro\my-geminiweb-mcp\`
- Tài liệu bàn giao đầy đủ: `my-geminiweb-orchestrator/docs/HANDOFF.md`

---

## Dành cho agent đọc skill này

Bốn điều đáng nhớ nhất — chỉ cần nhớ bốn thì dùng được:

1. **Gọi `gemini_models` trước khi chọn model.** Tên model của Google thay đổi không báo
   trước, và model bị khoá làm stream treo vô hạn (đã đo 901 giây).
2. **Giữ `conversation` handle** nếu muốn nói tiếp — bỏ nó là mất context.
3. **`servedModelConfirmed: false`** nghĩa là tên model là *suy ra*, không phải *xác nhận*.
   Đừng khẳng định nó trong báo cáo.
4. **Cookie chết thì phải harvest qua extension** — không có đường nào khác. Sau đó đồng bộ
   pool Python → C# bằng `orch_cli mcp-sync --apply`.

**Báo cáo cuối luôn kèm:** account đã dùng, model đã phục vụ (kèm mức confirmed), và
`quotaNotice` nếu có. Nếu đã dùng quota Gemini Advanced (gói AI Pro), nói rõ account nào
đã trừ — người dùng theo dõi allowance của họ.

