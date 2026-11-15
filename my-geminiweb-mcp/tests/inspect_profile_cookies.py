"""Report which Google accounts a Chrome profile still has cookies for.

Chrome encrypts cookie *values* with app-bound encryption, but the *names* and *domains*
stay in plaintext in the Cookies SQLite database. That is enough to tell whether a profile
still holds a signed-in Google session — without decrypting anything.

Usage: python inspect_profile_cookies.py [profile-name]
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROFILES = Path(
    r"C:\Users\kimvi\AppData\Local\Google\Chrome\User Data"
)

INTERESTING = {
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "NID",
    "COMPASS",
}


def report(profile_dir: Path) -> list[str]:
    db = profile_dir / "Default" / "Network" / "Cookies"
    if not db.is_file():
        db = profile_dir / "Network" / "Cookies"
    if not db.is_file():
        return []

    tmp = Path(tempfile.gettempdir()) / f"inspect_{profile_dir.name}.db"
    shutil.copy2(db, tmp)
    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select name, host_key, expires_utc, length(encrypted_value) "
                "from cookies where host_key like '%google.com' "
                "and name in (" + ",".join("?" * len(INTERESTING)) + ")",
                tuple(INTERESTING),
            ).fetchall()
        finally:
            con.close()
    finally:
        tmp.unlink(missing_ok=True)

    return rows


def main():
    wanted = sys.argv[1] if len(sys.argv) > 1 else None

    if not PROFILES.is_dir():
        print("no Chrome user data dir")
        return

    for prof in sorted(PROFILES.iterdir()):
        if not prof.is_dir():
            continue
        if not (prof.name == "Default" or prof.name.startswith("Profile ")):
            continue
        if wanted and prof.name != wanted:
            continue

        rows = report(prof)
        if not rows:
            continue
        print(f"=== {prof.name} ===")
        for name, host, expires, enc_len in rows:
            # 0 = session cookie; otherwise seconds since 1601-01-01.
            if expires == 0:
                exp = "session"
            else:
                secs = expires / 1_000_000 - 11_644_473_600
                import datetime

                exp = datetime.datetime.fromtimestamp(secs).strftime("%Y-%m-%d %H:%M")
            print(f"  {name:22} {host:16} expires={exp:18} enc_bytes={enc_len}")
        print()


if __name__ == "__main__":
    main()
