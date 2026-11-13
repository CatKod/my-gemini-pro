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
│   └── gemini_pool.py          ← pool config + auto-select
├── scripts/
│   ├── scan_browser_cookies.py ← công cụ quét cookie (đã dùng để nghiên cứu)
│   └── setup_pool.py           ← tạo pool từ cookie export
├── research/
│   └── chatcmd-analysis.md     ← phân tích int04/ChatCmd
├── pools/                      ← cookie export (gitignore, KHÔNG commit)
└── .cursor/rules/
    └── gemini-pro.mdc          ← rule tự động cho phiên mới
```

## Bắt đầu nhanh

```powershell
# 1. Đọc bàn giao
Get-Content docs\HANDOFF.md

# 2. Quét lại trạng thái cookie
python scripts\scan_browser_cookies.py
```

## Rủi ro đã biết

| Rủi ro | Mức | Xử lý |
|---|---|---|
| "Evil Clone" logout — Google kill-switch nếu 2 session trùng | ⚠️ Cao | Chrome profile riêng cho automation, không mở song song profile chính |
| Vi phạm ToS Google | ⚠️ | Chấp nhận rủi ro, dùng tài khoản phụ |
| Cookie `v20` app-bound encryption (Chrome ≥127) | 🟡 | Bắt buộc export từ bên trong Chrome, không đọc DB ngoài được |
| Google đổi endpoint `batchexecute` | 🟡 | `gemini_webapi` cập nhật thường xuyên — theo dõi upstream |

## License

MIT
