"""Probe which saved accounts still have a live Gemini session.

A dead session does NOT show up as an HTTP error: `GET https://gemini.google.com/app`
returns 200 with a login page. The only reliable signal is the presence of the
`SNlM0e` bootstrap token in the response HTML.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from curl_cffi.requests import AsyncSession

ACCOUNTS = Path(os.environ["LOCALAPPDATA"]) / "MyGeminiWebMcp" / "accounts.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


async def probe(entry):
    cookies = {
        "__Secure-1PSID": entry["psid"],
        "__Secure-1PSIDTS": entry["psidts"],
    }
    async with AsyncSession(impersonate="chrome145") as s:
        try:
            r = await s.get(
                "https://gemini.google.com/app",
                headers={
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                    "User-Agent": UA,
                },
                cookies=cookies,
                timeout=30,
            )
        except Exception as e:
            return entry["name"], "ERROR", f"{type(e).__name__}: {e}"
        body = r.text
        alive = "SNlM0e" in body
        return (
            entry["name"],
            "ALIVE" if alive else "DEAD",
            f"http={r.status_code} len={len(body)} snl={'yes' if alive else 'no'}",
        )


async def main():
    data = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    for entry in data.get("accounts", []):
        name, status, detail = await probe(entry)
        print(f"{status:6} {name:20} {detail}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
