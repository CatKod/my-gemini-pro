"""PyInstaller spec — đóng gói Orchestrator thành .exe trên Windows.

Hai bản build:
    onedir  (~50 MB, khởi động ~1.2s)  → dùng khi có GUI Tkinter
    onefile (~27 MB, khởi động ~3.3s)  → bản headless tối giản

Đã benchmark trên máy này (Python 3.14.8, MSVC 18 Community):
    PyInstaller onedir : 51.1 MB, median startup 1,251 ms
    PyInstaller onefile: 26.9 MB, median startup 3,309 ms
    Nuitka onedir     : 121.5 MB, median startup 1,040 ms (build ~6 phút)

Vì sao không Nuitka cho bản GUI: Python 3.14 nhúng Tcl qua `zipfs://` trong
`python314.dll`, khiến plugin `tk-inter` của Nuitka không tìm thấy Tcl.
Xem docs/ORCHESTRATOR.md.

Build:
    pyinstaller orchestrator.spec --noconfirm
"""

from __future__ import annotations

# Modules nặng không cần cho orchestrator.
EXCLUDES = [
    "numpy",
    "scipy",
    "PIL",
    "matplotlib",
    "pandas",
    "tkinter.test",
    "test",
    "unittest",
    "pydoc_data",
    "setuptools",
    "pip",
    "IPython",
    # Playwright kéo theo driver Node (~109 MB) → bản .exe phình từ 68 lên
    # 183 MB. Bản .exe ưu tiên nhẹ + startup nhanh; job `canvas` (browser
    # thật) cần Playwright nên chạy bằng Python source thay vì .exe.
    # Runner canvas sẽ báo lỗi rõ ràng nếu thiếu Playwright.
    "playwright",
]

HIDDEN = [
    "gemini_webapi",
    "mcp",
    "mcp.server.mcpserver",
    "curl_cffi",
    "certifi",
    # `cookie_sync` dùng websocket-client thuần Python (tránh kéo Playwright
    # driver ~109 MB vào bản .exe).
    "websocket",
    # Runner `gemini` import `gemini_pool` từ `tools/` — phải bundle để
    # bản .exe chạy được (source `tools/` không tồn tại cạnh file .exe).
    "gemini_pool",
    # Runner được spawn bằng lệnh nội bộ `__runner__` → `importlib` import
    # theo tên lúc chạy, PyInstaller không phát hiện tĩnh → khai báo tường minh.
    "orchestrator.runners.gemini",
    "orchestrator.runners.agy",
    "orchestrator.runners.canvas",
]

# Thư mục chứa `orchestrator/` và `tools/` — cần cho PyInstaller resolve
# import khi đóng gói.
#
# SPECPATH = thư mục chứa file .spec (= `<repo>/orchestrator`).
# `entry_cli.py` nằm cùng thư mục đó; `tools/gemini_pool.py` nằm ở `<repo>/tools`.
# Dùng đường dẫn tuyệt đối cho entry để không phụ thuộc cwd lúc build.
import os as _os

_SPEC_DIR = _os.path.abspath(SPECPATH)              # <repo>/orchestrator
_REPO_ROOT = _os.path.dirname(_SPEC_DIR)            # <repo>
PATHS = [_SPEC_DIR, _REPO_ROOT, _os.path.join(_REPO_ROOT, "tools")]
_ENTRY_CLI = _os.path.join(_SPEC_DIR, "entry_cli.py")
_ENTRY_GUI = _os.path.join(_SPEC_DIR, "entry_gui.py")

# `gemini_webapi` cần data file (models.json / images) đi kèm package.
datas = []
try:
    from importlib.util import find_spec

    spec = find_spec("gemini_webapi")
    if spec and spec.submodule_search_locations:
        import os

        pkg = spec.submodule_search_locations[0]
        for root, _dirs, files in os.walk(pkg):
            for f in files:
                if f.endswith((".json", ".js", ".css", ".html")):
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, os.path.dirname(pkg))
                    datas.append((full, os.path.join("gemini_webapi", rel)))
except Exception:
    pass

# ---- Console (headless) ----------------------------------------------------
a = Analysis(
    [_ENTRY_CLI],
    pathex=PATHS,
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="orchestrator-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="orchestrator-cli",
)

# ---- GUI (Tkinter) ---------------------------------------------------------
b = Analysis(
    [_ENTRY_GUI],
    pathex=PATHS,
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN + ["tkinter"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz2 = PYZ(b.pure)

exe2 = EXE(
    pyz2,
    b.scripts,
    [],
    exclude_binaries=True,
    name="orchestrator-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll2 = COLLECT(
    exe2,
    b.binaries,
    b.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="orchestrator-gui",
)
