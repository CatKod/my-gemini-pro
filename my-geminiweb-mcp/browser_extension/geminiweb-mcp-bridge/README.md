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

**There is nothing to paste.** The extension finds the server on its own:

- it scans `127.0.0.1:9478` upwards until a bridge answers `/ping`;
- it then reads the endpoint and the shared token from the bridge's `/config` endpoint
  and stores them.

Open the popup once and press **Send cookies** while signed in to
`https://gemini.google.com/app`. That is the whole setup.

Turn on **"Auto-send on every Gemini tab"** to have the pool refresh by itself every time
you open Gemini — no clicking needed.

### When the port moves

The bridge prefers port 9478 but walks to the next free port when that one is taken (a
previous server instance that did not shut down cleanly, for example). The extension
scans a range of ports, so it follows the server on its own.

### Manual override

The **Endpoint** and **Token** fields accept manual values if you want to pin the
extension to a specific server. Leave them alone in normal use.

## What it sends

Only cookies for `.google.com`, and only to `127.0.0.1`:

- `__Secure-1PSID`, `__Secure-1PSIDTS` — the session
- `__Secure-3PSID`, `__Secure-3PSIDTS` — the device-bound pair
- `NID`, `COMPASS` — routing and preference cookies

Nothing leaves your machine. The extension refuses to post to any address that is not
loopback, and the server rejects any request without the shared token.

## Why a web page cannot steal the token

The token is served from `/config` on the loopback interface. A page on the internet can
send a request to that address, but it cannot **read** the answer: the bridge sends
`Access-Control-Allow-Origin` only for an extension origin, so the browser blocks the
response for everyone else. A local process could read it — but such a process can
already read `accounts.json`, which holds the very same cookies.

## Troubleshooting

| Message | Meaning |
|---|---|
| `Không thấy cookie __Secure-1PSID` | Not signed in to Google, or the tab is not `gemini.google.com`. |
| `token sai` / `bad token` | The server was restarted with a different token. Reload the extension; it re-reads the token. |
| `Không kết nối được` | The MCP server is not running. Start it and retry. |
| The extension finds no server | Check that the server is running: `curl http://127.0.0.1:9478/ping`. |
