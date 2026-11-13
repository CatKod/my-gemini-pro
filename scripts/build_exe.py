"""Build Orchestrator thành .exe (PyInstaller onedir).

Kết quả:
    dist/orchestrator-cli/orchestrator-cli.exe   (console, headless)
    dist/orchestrator-gui/orchestrator-gui.exe   (Tkinter dashboard)

Benchmark đã đo trên máy này (Python 3.14.8, MSVC 18 Community):
    onedir ~51 MB, startup median ~1,25 s

Chạy:
    python scripts\\build_exe.py
    python scripts\\build_exe.py --onefile     # bản 1 file (~27 MB, chậm hơn)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "orchestrator" / "orchestrator.spec"
DIST = REPO_ROOT / "dist"
BUILD = REPO_ROOT / "build"


def _run(cmd: list[str], **kw) -> int:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(REPO_ROOT), **kw).returncode


def build(onefile: bool = False) -> int:
    if shutil.which("pyinstaller") is None:
        print(
            "Chưa cài pyinstaller. Chạy:\n"
            "  python -m pip install pyinstaller",
            file=sys.stderr,
        )
        return 1

    cmd = ["pyinstaller", str(SPEC), "--noconfirm", "--distpath", str(DIST), "--workpath", str(BUILD)]
    if onefile:
        cmd.append("--onefile")
    code = _run(cmd)
    if code != 0:
        return code

    for name in ("orchestrator-cli", "orchestrator-gui"):
        exe = DIST / name / f"{name}.exe"
        if exe.is_file():
            size = exe.stat().st_size
            total = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file())
            print(f"{name}.exe: {size/1e6:.1f} MB (cả thư mục: {total/1e6:.1f} MB)")
    return 0


def benchmark() -> int:
    """Đo startup time của bản CLI."""
    exe = DIST / "orchestrator-cli" / "orchestrator-cli.exe"
    if not exe.is_file():
        print("Chưa build. Chạy build trước.", file=sys.stderr)
        return 1
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        subprocess.run([str(exe), "list", "--limit", "1"], capture_output=True)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    print(f"startup: median {times[len(times)//2]:.0f} ms  (min {times[0]:.0f}, max {times[-1]:.0f})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true")
    ap.add_argument("--bench-only", action="store_true")
    args = ap.parse_args()

    if args.bench_only:
        return benchmark()
    rc = build(args.onefile)
    if rc == 0:
        benchmark()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
