# Kế hoạch: MCP Server App (.NET) — hướng mới

> Ngày: 2026-11-15. Được tạo khi user chuyển hướng sang project **MCP Server App**
> mới tạo trong Visual Studio (`my-geminiweb-mcp/`).

---

## 1. Vì sao chuyển hướng

Hệ thống Python hiện tại đã làm được: cookie bridge, auto-login 4 acc, Deep
Research, chat nhiều lượt, attach file, tải file Gemini sinh ra, GUI, `.exe`.
Nhưng **bề mặt lỗi lớn và phần lớn code là hạ tầng tự chế** không liên quan
tới giá trị cốt lõi (điều khiển Gemini):

| Vùng | Vấn đề | Cơ sở |
|---|---|---|
| CDP/cookie | app-bound encryption → phải tự quản lý profile Chrome riêng, harvest, purge cache, ưu tiên domain cookie | đã đụng 4 bug |
| Hằng ngày | `1PSIDTS` stale → `UNAUTHENTICATED (1016)` giả, phải `harvest all` | verify 2026-11-15 |
| Tiến trình | phải spawn Chrome + subprocess + watchdog + retry 1097/1013 | nhiều edge case |
| Song song | 4 client cùng lúc → rate-limit, DR plan fail | verify 2026-11-15 |
| Đóng gói | PyInstaller + `.spec` + data-root split-brain (đã có `paths.py` để vá) | bug thật |
| MCP | viết tay bằng `FastMCP` → không có manifest, không publish được, IDE không tự nhận | sự thật |

Bản .NET dùng **SDK MCP chính thức** của Microsoft → có manifest
`.mcp/server.json`, đóng gói NuGet self-contained, IDE nhận cấu hình
`dnx`/`dotnet run` chuẩn.

---

## 2. Điều MCP Server App **không** giải quyết

Cần nói thẳng để tránh kỳ vọng sai:

| Vấn đề | MCP Server App có giải quyết? |
|---|---|
| Cookie/app-bound encryption | ❌ Vẫn phải lấy cookie từ trong Chrome (CDP hoặc extension) |
| `1PSIDTS` stale | ❌ Vẫn phải xử lý, chỉ khác ngôn ngữ |
| Google đổi endpoint | ❌ Vẫn phải theo upstream `gemini_webapi` |
| Điều khiển Chrome (Canvas) | ❌ Vẫn cần CDP/Playwright |
| Deep Research polling | ❌ Vẫn phải poll |
| "Evil Clone" | ❌ Vẫn phải profile riêng |

**Nó chỉ giải quyết phần "vỏ":** cách đóng gói, cách khai báo tool, cách IDE
nhận server, vòng đời process, cấu hình input. Đó là phần **ổn định & bảo
trì** — vì đó mới là chỗ ta đang bế tắc.

---

## 3. Phạm vi dự kiến

### Phase 0 — Chứng minh đường đi (1 ngày)
- Dùng template: đổi `RandomNumberTools` → tool `gemini_chat` gọi `gemini_webapi` (Python) qua child process, hoặc viết lại client bằng C#.
- Mục tiêu: **tool chạy được trong Visual Studio Copilot Chat** → xác nhận toàn bộ giả định về SDK + manifest.

### Phase 1 — Port lõi (3–5 ngày)
- `gemini_chat`, `gemini_upload` (file đính kèm), `gemini_files` (tải file sinh ra)
- Quản lý pool + cookie đọc từ `pools/*.json` (giữ nguyên format hiện tại)
- Auto-refresh `1PSIDTS` + fallback sang pool khác

### Phase 2 — Tương tác nâng cao (nếu cần)
- `gemini_deep_research` (plan + confirm + poll)
- `gemini_chat` nối tiếp hội thoại (`chat_metadata`)

### Phase 3 — Bỏ Python
- Khi C# đã phủ hết tool, `my-geminiweb-orchestrator/` chỉ còn là archive.

---

## 4. Quyết định còn mở (cần user chốt)

| # | Câu hỏi | Vì sao quan trọng |
|---|---|---|
| Q1 | C# hay Python làm lõi? | C#: 1 ngôn ngữ, NuGet, self-contained. Python: tái dùng `gemini_webapi` ngay (nhưng phải giữ child process) |
| Q2 | MCP server chạy **cục bộ** hay **remote/HTTP**? | Remote → multi-device, nhưng phải xử lý auth + cookie nằm trên máy chủ |
| Q3 | Có cần GUI/Tkinter nữa không? | GUI là nơi phát sinh nhiều bug nhất (split-brain data-root) |
| Q4 | Bao nhiêu tool trong 1 server? | MCP clients (VS Copilot) giới hạn số tool hiển thị; 1 server 5 tool là mức hợp lý |
| Q5 | Publish NuGet (public) hay chỉ dùng local? | Public → cần đóng gói cookie-store cẩn thận, không để lộ |

---

## 5. Rủi ro

| Rủi ro | Mức | Xử lý |
|---|---|---|
| Viết lại client Gemini bằng C# tốn công, upstream đổi format liên tục | ⚠️ Cao | Phase 0 chứng minh giả định trước; giữ Python fallback |
| MCP client không hỗ trợ hết tính năng (progress, resource) | 🟡 | Thiết kế tool theo giới hạn client |
| Packaging NuGet: cần publish secret store ra ngoài | ⚠️ | Phase 0 dùng local path; publish chỉ khi Q5 = public |

---

## 6. Trạng thái

- [x] Tạo project template trong Visual Studio
- [ ] Phase 0 — chứng minh 1 tool Gemini chạy được trong VS Copilot Chat
- [ ] Chốt Q1–Q5
- [ ] Phase 1 — port lõi
