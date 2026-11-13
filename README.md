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
- [x] Export cookie cho pool `windows` (`mmeo44244@gmail.com`) → `pools/windows.json`
- [x] Viết `tools/gemini_pool.py` + `tools/gemini_dr_mcp.py` (pool kép + auto-routing)
- [x] Tích hợp vào `gemini_mcp.py` (thêm `gemini_dr_*` tools)
- [x] Test end-to-end: Deep Research chạy thật, trả report + sources
- [x] **Orchestrator** — chạy task dài không chặn agent (Deep Research / chat / canvas / Antigravity)
- [x] Tự động tạo hồ sơ Chrome cho acc Google mới (song song hoá pool)
- [x] GUI dashboard (Tkinter) + đóng gói `.exe`
- [ ] Export cookie cho pool `wsl` (`catkingmv@gmail.com`)
- [ ] Bật `gemini-dr` + `orchestrator` MCP server trong Cursor (đã ghi vào `~/.cursor/mcp.json`, cần reload)

## Orchestrator — task dài không chặn agent

Vấn đề: gọi Gemini Deep Research / Antigravity trực tiếp = agent phải chờ
30-60 phút, và bạn không biết Gemini đang làm gì.

Giải pháp: tách "chạy" khỏi "chờ". Job chạy ở process con độc lập, ghi
tiến độ vào SQLite; agent làm việc khác rồi quay lại lấy kết quả.

```powershell
# Submit -> nhận job_id ngay, KHÔNG chặn
python -m orchestrator.orch_cli submit deep_research "So sánh X vs Y năm 2026" --pool windows

# Chạy NỀN -> Cursor tự đánh thức bạn khi job xong
python -m orchestrator.orch_cli watch j_xxxxx

# GUI (tuỳ chọn)
python -m orchestrator.gui
```

Xem `docs/ORCHESTRATOR-USAGE.md`.

## Cấu trúc

```
my-gemini-pro/
├── README.md
├── docs/
│   ├── HANDOFF.md              ← bàn giao cho phiên mới (đọc file này trước)
│   ├── ARCHITECTURE.md
│   ├── FINDINGS.md             ← kết quả nghiên cứu + nguồn
│   ├── COOKIE-SETUP.md
│   ├── ORCHESTRATOR.md         ← kiến trúc orchestrator
│   └── ORCHESTRATOR-USAGE.md   ← hướng dẫn dùng
├── orchestrator/               ← Orchestrator: chạy task dài KHÔNG chặn agent
│   ├── store.py                ← SQLite job store + event log
│   ├── dispatch.py             ← submit / spawn / wait / cancel
│   ├── orch_mcp.py             ← MCP server (cổng gọi của agent)
│   ├── orch_cli.py             ← CLI (dùng cho shell background)
│   ├── gui.py                  ← Tkinter dashboard
│   ├── chrome_profile.py       ← tạo hồ sơ Chrome cho multi-account
│   ├── selftest.py             ← smoke test
│   ├── orchestrator.spec       ← PyInstaller spec (.exe)
│   ├── skills/orchestrator/SKILL.md
│   └── runners/
│       ├── gemini.py           ← Deep Research / chat / canvas
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

# 3. Test một deep research thật (đóng Chrome trước để tránh evil-clone)
python -c "import asyncio,sys; sys.path.insert(0,'tools'); from gemini_dr_mcp import gemini_dr_research; print(asyncio.run(gemini_dr_research('Rust vs Go: quan ly bo nho')))"
```

## MCP tools

Server `gemini-dr` (`tools/gemini_dr_mcp.py`) + các tool `gemini_dr_*` đã gắn
thêm vào server `gemini` (`~/.cursor/bin/gemini_mcp.py`):

| Tool | Việc |
|---|---|
| `gemini_dr_research(query, pool, timeout, model)` | Chạy full research → report markdown + sources |
| `gemini_dr_plan(query, pool, model)` | Chỉ xem plan, **chưa** chạy (để duyệt scope) |
| `gemini_dr_collect(cid, pool, timeout)` | Thu report của task đang chạy (dùng khi research bị timeout) |
| `gemini_dr_quota(pool)` | Headroom 5h + weekly của mỗi pool |
| `gemini_dr_account()` | Email ↔ pool mapping |

### ⚠️ Model bắt buộc

Deep Research **không** chạy trên mọi model. Model mà account để mặc định thường
*từ chối* với câu "tôi chỉ là mô hình ngôn ngữ" thay vì trả plan. Bridge tự dò
qua các model ứng viên và cache model đã biết vào `pools/<name>.json` (`dr_model`).

Xem upstream issue: <https://github.com/HanaokaYuzu/Gemini-API/issues/359>

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
