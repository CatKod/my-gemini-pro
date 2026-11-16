"""Read a Chrome LevelDB value file without disturbing the running browser.

Chrome keeps extension settings in a LevelDB directory under "Local Extension
Settings". The .log file is held open by the browser, so it has to be opened with
shared access and read from there.

Usage:  python read_extension_storage.py <profile-name> <extension-id>
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

CHROME = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"


def read_shared(path: Path) -> bytes:
    with open(path, "rb") as fh:
        # On Windows this uses the default share mode, which fails on a locked file.
        # Retry with an explicit share that allows reading while Chrome holds it.
        pass
    return path.read_bytes()


def read_shared_win(path: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if handle == -1:
        raise OSError(f"cannot open {path}")

    size = kernel32.GetFileSize(handle, None)
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD()
    kernel32.ReadFile(handle, buf, size, ctypes.byref(read), None)
    kernel32.CloseHandle(handle)
    return buf.raw[: read.value]


def main():
    profile = sys.argv[1] if len(sys.argv) > 1 else "Default"
    ext_id = sys.argv[2] if len(sys.argv) > 2 else ""

    folder = CHROME / profile / "Local Extension Settings" / ext_id
    if not folder.is_dir():
        print(f"no storage for {ext_id} in {profile}")
        return

    for file in sorted(folder.iterdir()):
        if file.suffix not in (".log", ".ldb"):
            continue
        try:
            data = read_shared_win(file)
        except OSError as e:
            print(f"  {file.name}: {e}")
            continue

        ascii_text = data.decode("latin-1", "replace")
        strings = re.findall(r"[\x20-\x7E]{5,}", ascii_text)
        interesting = [
            s for s in strings
            if any(k in s for k in ("harvest", "127.0.0.1", "token", "endpoint", "account"))
        ]
        print(f"=== {file.name} ({len(data)} bytes) ===")
        for s in dict.fromkeys(interesting):
            print("   ", s)


if __name__ == "__main__":
    main()
