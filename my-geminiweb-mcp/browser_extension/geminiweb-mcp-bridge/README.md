# GeminiWeb MCP Cookie Bridge

A Chrome/Edge extension that hands your signed-in Gemini session to
[MyGeminiWebMcp](https://www.nuget.org/packages/MyGeminiWebMcp), the MCP server that
serves Gemini Web.

## Why an extension

Chrome and Edge encrypt their cookie store on disk with **app-bound encryption**
(Chrome ≥ 127). The key is bound to the browser process, so nothing outside it — not a
script, not `Cookie-Editor` running as a desktop app — can read `__Secure-1PSID` from
disk.

An extension runs *inside* the browser and uses the `chrome.cookies` API, so the browser
decrypts the cookies on your behalf. It is the only supported way to get them.

## Install

1. Open `chrome://extensions` (Chrome) or `edge://extensions` (Edge).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select this folder.
4. Pin the extension to your toolbar.

## Configure

Start the MCP server. It prints the bridge endpoint and a token to **stderr** on
start-up, and the `gemini_bridge` tool returns the same values.

In the extension popup:

| Field | Value |
|---|---|
| Endpoint | `http://127.0.0.1:9478/harvest` |
| Token | the token from the server |
| Account | optional — the pool key to file this session under |

Press **Save**, then open `https://gemini.google.com/app` while signed in and press
**Send cookies**.

Turn on **"Auto-send on every Gemini tab"** to have the pool refresh by itself every time
you open Gemini — no clicking needed.

## What it sends

Only cookies for `.google.com`, and only to `127.0.0.1`:

- `__Secure-1PSID`, `__Secure-1PSIDTS` — the session
- `__Secure-3PSID`, `__Secure-3PSIDTS` — the device-bound pair
- `NID`, `COMPASS` — routing and preference cookies

Nothing leaves your machine. The extension refuses to post to any address that is not
loopback, and the server rejects any request without the shared token.

## Troubleshooting

| Message | Meaning |
|---|---|
| `Không thấy cookie __Secure-1PSID` | Not signed in to Google, or the tab is not `gemini.google.com`. |
| `token sai` / `bad token` | The server restarted and issued a new token. Copy the new one. |
| `Không kết nối được` | The MCP server is not running. Start it and retry. |
