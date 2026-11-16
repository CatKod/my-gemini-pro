"""Read a Chrome LevelDB directory (extension settings) and print the key/value pairs.

The browser holds the .log file open, so it is read with an explicit share mode that
permits reading while Chrome has it locked.

Usage:  python read_leveldb.py <leveldb-dir>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def read_shared(path: Path) -> bytes:
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
    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"missing {folder}")
        return

    for file in sorted(folder.iterdir()):
        if file.suffix not in (".log", ".ldb"):
            continue
        try:
            data = read_shared(file)
        except OSError as e:
            print(f"{file.name}: {e}")
            continue

        print(f"=== {file.name} ({len(data)} bytes) ===")
        # LevelDB stores values with a length prefix; printable runs are enough for
        # a quick look at what is inside. Chrome writes some of it as UTF-16.
        for match in re.finditer(rb"[\x20-\x7E]{6,}", data):
            print("   ", match.group().decode("ascii"))
        for match in re.finditer(rb"(?:[\x20-\x7E]\x00){6,}", data):
            print("   u", match.group().decode("utf-16-le"))


if __name__ == "__main__":
    main()
