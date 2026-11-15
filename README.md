# my-gemini-pro

Hệ thống **điều khiển Gemini Deep Research thông qua gói Google AI Pro (Gemini Advanced) đã trả phí** — không dùng AI Studio Interactions API (vốn tính phí pay-as-you-go riêng).

Repo nay chứa **2 project độc lập** cùng tồn tại:

| Thư mục | Ngôn ngữ | Vai trò | Trạng thái |
|---|---|---|---|
| `my-geminiweb-mcp/` | C# / .NET | **MCP Server App** — hướng mới (SDK `ModelContextProtocol` chính thức, đóng gói NuGet) | 🆕 mới tạo |
| `my-geminiweb-orchestrator/` | Python | Orchestrator CDP/cookie + GUI + CLI + MCP server (Python) | 📦 đã archive, vẫn chạy được |

---

## 1. `my-geminiweb-mcp/` — MCP Server App (hướng mới)

Project .NET tạo bằng template **MCP Server App** của Visual Studio, dùng SDK C# chính thức [`ModelContextProtocol`](https://csharp.sdk.modelcontextprotocol.io/).

```
my-geminiweb-mcp/
└── my-geminiweb-mcp/
    ├── Program.cs                     ← host + stdio transport + đăng ký tools
    ├── Tools/RandomNumberTools.cs     ← tool mẫu (sẽ thay bằng tool Gemini)
    ├── .mcp/server.json               ← manifest publish lên NuGet
    ├── my-geminiweb-mcp.csproj
    └── my-geminiweb-mcp.slnx
```

**Vì sao hướng mới:** bản Python hiện tại phải tự quản lý CDP, cookie, profile
Chrome, retry, watchdog, quarantine — nhiều bề mặt lỗi. Bản .NET chính thức
được maintain cùng SDK MCP, đóng gói self-contained, có sẵn khung NuGet/config
input, và chạy được trong Visual Studio / VS Code **mà không cần cài runtime**.

> Trạng thái: mới tạo, còn tool mẫu `get_random_number`. Xem
> [`docs/MCP-SERVER-APP-PLAN.md`](my-geminiweb-orchestrator/docs/MCP-SERVER-APP-PLAN.md)
> để biết kế hoạch chi tiết.

---

## 2. `my-geminiweb-orchestrator/` — Orchestrator Python (đã archive)

Toàn bộ hệ thống Python cũ, **giữ nguyên khả năng chạy**, chỉ đổi vị trí thư mục.

```
my-geminiweb-orchestrator/
├── docs/
│   ├── HANDOFF.md              ← bàn giao cho phiên mới (đọc file này trước)
│   ├── ARCHITECTURE.md
│   ├── FINDINGS.md             ← kết quả nghiên cứu + nguồn
│   ├── COOKIE-BRIDGE.md        ← cookie bridge + auto_login + file attach/download
│   ├── COOKIE-SETUP.md
│   ├── ORCHESTRATOR.md         ← kiến trúc orchestrator
│   └── ORCHESTRATOR-USAGE.md   ← hướng dẫn dùng
├── orchestrator/               ← Orchestrator: chạy task dài KHÔNG chặn agent
│   ├── store.py                ← SQLite job store + event log
│   ├── dispatch.py             ← submit / spawn / wait / cancel
│   ├── orch_mcp.py             ← MCP server (cổng gọi của agent)
│   ├── orch_cli.py             ← CLI (dùng cho shell background)
│   ├── gui.py / ui.py / dashboard.py / board.py
│   ├── paths.py                ← nguồn sự thật duy nhất cho đường dẫn dữ liệu
│   ├── cdp.py / auto_login.py / harvest.py / cookie_sync.py / cookie_bridge.py
│   ├── gemini_files.py         ← đọc + tải file Gemini sinh ra (.xlsx/.pdf/…)
│   ├── chrome_profile.py       ← tạo hồ sơ Chrome cho multi-account
│   ├── selftest.py             ← smoke test
│   ├── orchestrator.spec       ← PyInstaller spec (.exe)
│   └── runners/
│       ├── gemini.py           ← Deep Research / chat / canvas (+ attach file)
│       └── agy.py              ← Antigravity CLI (NDJSON stream)
├── tools/
│   ├── gemini_dr_mcp.py        ← MCP server Deep Research
│   └── gemini_pool.py          ← pool config + auto-select + quota
├── scripts/
│   ├── continue.py             ← entrypoint cho phiên mới (nhắn "continue")
│   ├── scan_browser_cookies.py ← quét trạng thái cookie trên máy
│   ├── setup_pool.py           ← tạo/validate/probe pool
│   └── build_exe.py            ← đóng gói .exe
├── research/
│   └── chatcmd-analysis.md     ← phân tích int04/ChatCmd (đã loại)
├── browser_extension/          ← extension Chrome cấp cookie (cookie bridge)
├── pools/                      ← cookie export (gitignore, KHÔNG commit)
├── reports/                    ← output job + file Gemini sinh ra (gitignore)
├── accounts.json               ← registry tài khoản (gitignore, KHÔNG commit)
└── cookie_files/               ← bản sao cookie gốc (gitignore, KHÔNG commit)
```

### Chạy

```powershell
cd my-geminiweb-orchestrator

# 0. Phiên mới: xem trạng thái hiện tại
python scripts\continue.py

# 1. Orchestrator — submit job (KHÔNG chặn agent)
python -m orchestrator.orch_cli submit deep_research "So sánh X vs Y năm 2026" --pool windows
python -m orchestrator.orch_cli watch j_xxxxx

# 2. GUI dashboard
python -m orchestrator.gui

# 3. Chat kèm file đính kèm, file Gemini sinh ra được tải về reports/downloads/
python -m orchestrator.orch_cli submit chat "Đọc file đính kèm" --pool acc --attach file.py
```

Xem `my-geminiweb-orchestrator/docs/ORCHESTRATOR-USAGE.md`.

---

## Nguyên tắc bất di bất dịch

1. **KHÔNG dùng AI Studio Interactions API** — nó tính phí riêng, không trừ vào gói AI Pro.
2. **KHÔNG đọc cookie Chrome/Edge từ ngoài tiến trình** — Chrome ≥127 dùng app-bound encryption.
3. **KHÔNG copy cookie từ profile đang mở** — rủi ro "Evil Clone" → Google logout toàn diện.
4. **KHÔNG commit cookie / mật khẩu / token lên git.** `.gitignore` chặn `pools/`, `reports/`, `accounts.json`, `cookie_files/`.
5. **KHÔNG claim gì chưa verify** — mọi claim load-bearing phải có primary source hoặc output thật từ tool.

---

## Rủi ro đã biết

| Rủi ro | Mức | Xử lý |
|---|---|---|
| "Evil Clone" logout — Google kill-switch nếu 2 session trùng | ⚠️ Cao | Chrome profile riêng cho automation, không mở song song profile chính |
| Vi phạm ToS Google | ⚠️ | Chấp nhận rủi ro, dùng tài khoản phụ |
| Cookie `v20` app-bound encryption (Chrome ≥127) | 🟡 | Phải lấy cookie từ bên trong Chrome (CDP trên profile riêng, hoặc extension) |
| Google đổi endpoint `batchexecute` | 🟡 | `gemini_webapi` cập nhật thường xuyên — theo dõi upstream |
| `account_status=1016` giả (cookie stale) | 🟡 | `cdp.get_cookies()` ưu tiên domain `.google.com`; `harvest all` tự đăng nhập lại |

## License

MIT
