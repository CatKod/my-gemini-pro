# my-gemini-pro

Hệ thống **điều khiển Gemini Deep Research thông qua gói Google AI Pro (Gemini Advanced) đã trả phí** — không dùng AI Studio Interactions API (vốn tính phí pay-as-you-go riêng).

## Mục tiêu

| Hệ thống | Đi đường nào | Được tính vào đâu |
|---|---|---|
| `agy` / Antigravity CLI | OAuth Antigravity | Quota Antigravity (Windows + WSL) |
| AI Studio Interactions API | `x-goog-api-key` | **Tiền riêng** ❌ — đã loại |
| **`gemini_webapi`** (repo này) | Cookie `gemini.google.com` | **Gói Gemini Advanced / AI Pro của bạn** ✅ |

## Trạng thái hiện tại

- [x] Xác minh `ChatCmd` (`int04/ChatCmd`) **không dùng được** cho Gemini — repo chỉ có bridge ChatGPT, 0 tham chiếu Gemini
- [x] Xác định giải pháp: `HanaokaYuzu/Gemini-API` (`gemini_webapi`), 3.8k stars, cập nhật 2026-11-10
- [x] Quét cookie: **cả 2 profile Chrome đều đã đăng nhập Gemini** (`mmeo44244`, `catkingmv`)
- [ ] Export cookie từ trong Chrome (Cookie-Editor) → `pools/`
- [ ] Viết `tools/gemini_dr_mcp.py` (pool kép + auto-routing)
- [ ] Tích hợp vào `gemini_mcp.py` hiện có

## Cấu trúc

```
my-gemini-pro/
├── README.md
├── docs/
│   ├── HANDOFF.md              ← bàn giao cho phiên mới (đọc file này trước)
│   ├── ARCHITECTURE.md
│   ├── FINDINGS.md             ← kết quả nghiên cứu + nguồn
│   └── COOKIE-SETUP.md
├── tools/
│   ├── gemini_dr_mcp.py        ← MCP server Deep Research (chưa viết)
│   └── gemini_pool.py          ← pool config + auto-select (chưa viết)
├── scripts/
│   ├── continue.py             ← entrypoint cho phiên mới (nhắn "continue")
│   ├── scan_browser_cookies.py ← quét trạng thái cookie trên máy
│   └── setup_pool.py           ← tạo/validate/probe pool
├── research/
│   └── chatcmd-analysis.md     ← phân tích int04/ChatCmd (đã loại)
├── pools/                      ← cookie export (gitignore, KHÔNG commit)
└── .cursor/rules/
    └── gemini-pro.mdc          ← rule tự động cho phiên mới
```

## Bắt đầu nhanh

```powershell
# 0. Phiên mới: xem trạng thái hiện tại
python scripts\continue.py

# 1. Quét trạng thái cookie hiện tại
python scripts\scan_browser_cookies.py

# 2. Sau khi export cookie (Cookie-Editor):
python scripts\setup_pool.py setup --export <file.json> --name windows --account mmeo44244@gmail.com
python scripts\setup_pool.py validate
python scripts\setup_pool.py probe

# 3. Test một deep research thật
python -c "import asyncio; from gemini_webapi import GeminiClient; ..."
```

## Về "nhắn continue ở phiên khác"

Repo được thiết kế để phiên mới nạp lại context chỉ bằng 1 từ khoá:

1. **`docs/HANDOFF.md`** — toàn bộ context, bằng chứng, kế hoạch, việc chưa làm
2. **`.cursor/rules/gemini-pro.mdc`** — rule tự động: đọc HANDOFF.md trước, không dùng AI Studio API, không đọc cookie ngoài Chrome, không commit cookie
3. **`scripts/continue.py`** — in trạng thái hiện tại + bước tiếp theo
4. **agentmemory** — đã lưu quyết định kiến trúc (project `my-gemini-pro`, agent `cursor`)

Mở repo này trong Cursor → nhắn `continue` → agent sẽ đọc rule, đọc HANDOFF.md, chạy `scripts/continue.py` và báo đúng trạng thái.

## Rủi ro đã biết

| Rủi ro | Mức | Xử lý |
|---|---|---|
| "Evil Clone" logout — Google kill-switch nếu 2 session trùng | ⚠️ Cao | Chrome profile riêng cho automation, không mở song song profile chính |
| Vi phạm ToS Google | ⚠️ | Chấp nhận rủi ro, dùng tài khoản phụ |
| Cookie `v20` app-bound encryption (Chrome ≥127) | 🟡 | Bắt buộc export từ bên trong Chrome, không đọc DB ngoài được |
| Google đổi endpoint `batchexecute` | 🟡 | `gemini_webapi` cập nhật thường xuyên — theo dõi upstream |

## License

MIT
