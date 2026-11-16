"""Remove test leftovers from the real account pool.

The end-to-end tests use a scratch home, but a bug once let a test bridge attach to the
real pool. This script drops any account whose cookies carry a known test marker.

Usage:  python clean_real_pool.py [--dry-run]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

POOL = (
    Path(os.environ["LOCALAPPDATA"])
    / "MyGeminiWebMcp"
    / "accounts.json"
)

#: Values the tests use. An account holding one of them is a test leftover.
MARKERS = {
    "g.a000EXTENSIONTESTSID",
    "sidts-EXTENSIONTEST",
    "ext-3psid",
    "ext-nid-123",
}


def main():
    dry_run = "--dry-run" in sys.argv

    if not POOL.is_file():
        print(f"no pool at {POOL}")
        return

    data = json.loads(POOL.read_text(encoding="utf-8"))
    accounts = data.get("accounts", [])

    keep, drop = [], []
    for account in accounts:
        values = {account.get("psid", ""), account.get("psidts", "")}
        values |= set((account.get("cookies") or {}).values())
        if values & MARKERS:
            drop.append(account)
        else:
            keep.append(account)

    print(f"pool: {POOL}")
    print(f"  accounts: {len(accounts)}, keep: {len(keep)}, drop: {len(drop)}")
    for account in drop:
        print(f"  DROP {account.get('name')}")

    if dry_run or not drop:
        return

    data["accounts"] = keep
    POOL.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  written: {len(keep)} accounts left")


if __name__ == "__main__":
    main()
