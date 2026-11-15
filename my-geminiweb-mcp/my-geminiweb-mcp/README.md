# MyGeminiWebMcp

MCP server for **Gemini Web** (`gemini.google.com`) — multi-account pool, automatic failover,
conversation continuity. Ships as a **single-file Native AOT binary** (~16 MB): no .NET
runtime, no Python, no extra dependencies on the user's machine.

> Uses the Google AI Pro subscription the user already pays for. It does **not** touch the
> AI Studio Interactions API, which bills separately.

---

## Tools

| Tool | Purpose |
|---|---|
| `gemini_chat` | Send a message to Gemini, get the reply. Pass the `conversation` handle back to continue the same chat. |
| `gemini_accounts` | List the configured accounts and their health. |
| `gemini_check` | Verify that each account can actually reach Gemini. |
| `gemini_refresh` | Keep the sessions alive by refreshing the expiring cookie. |
| `gemini_bridge` | Show the endpoint and token the browser extension needs. |

### `gemini_chat`

```jsonc
{
  "prompt": "Explain this code",
  "account": "account1",       // optional: pin to one account
  "conversation": "{...}"      // optional: handle from a previous reply
}
```

Returns:

```jsonc
{
  "account": "account1",
  "reply": "…",
  "conversation": "{\"account\":\"account1\",\"cid\":\"c_…\",\"rid\":\"r_…\"}",
  "thoughts": null
}
```

**Keep the `conversation` handle.** It pins the follow-up turn to the account that owns
the conversation — a Gemini conversation id is meaningless on a different account.

---

## Setup

### 1. Install

```bash
dotnet tool install -g MyGeminiWebMcp
```

Or, from VS Code / Visual Studio, use `dnx`:

```jsonc
{
  "servers": {
    "geminiweb": {
      "type": "stdio",
      "command": "dnx",
      "args": ["MyGeminiWebMcp", "--version", "0.1.0-beta", "--yes"]
    }
  }
}
```

### 2. Provide session cookies

The server authenticates with the `__Secure-1PSID` / `__Secure-1PSIDTS` cookies from a
signed-in Google session.

Chrome and Edge encrypt their cookie store on disk with **app-bound encryption**
(Chrome ≥ 127), so no tool outside the browser process can read them. There are two ways
to get them in.

#### Option A — the browser extension (recommended)

The repository ships a Chrome/Edge extension at
[`browser_extension/geminiweb-mcp-bridge/`](browser_extension/geminiweb-mcp-bridge/).

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select the `browser_extension/geminiweb-mcp-bridge` folder.
4. Start the MCP server and call the `gemini_bridge` tool — it returns the endpoint and
   the token.
5. Open the extension's popup, paste the endpoint and token, and press **Send cookies**
   while signed in to `gemini.google.com`.

The extension reads the cookies through `chrome.cookies`, so the browser itself decrypts
them. It only reads `.google.com` cookies and only posts to `127.0.0.1`.

Turn on **"Auto-send on every Gemini tab"** in the popup to keep the pool fresh without
clicking anything.

#### Option B — paste the cookies by hand

1. Open `gemini.google.com` and sign in.
2. Open DevTools → **Application** → **Storage** → **Cookies** → `https://gemini.google.com`.
3. Copy the values of `__Secure-1PSID` and `__Secure-1PSIDTS`.

### 3. Write the account pool

Create `%LOCALAPPDATA%\MyGeminiWebMcp\accounts.json`:

```json
{
  "accounts": [
    {
      "name": "account1",
      "email": "you@gmail.com",
      "psid": "<__Secure-1PSID value>",
      "psidts": "<__Secure-1PSIDTS value>"
    },
    {
      "name": "account2",
      "email": "other@gmail.com",
      "psid": "<…>",
      "psidts": "<…>"
    }
  ]
}
```

Set `GEMINIWEB_MCP_HOME` to use a different folder. The server rewrites this file
atomically whenever it learns a fresher cookie, so you never have to edit it by hand
after the extension has run once.

### 4. Verify

Ask your agent to call the `gemini_check` tool, or run it directly.

---

## Behaviour

- **Rotation** — each new conversation picks the next healthy account.
- **Failover** — an account that fails is skipped for 10 minutes; the request retries on
  another one.
- **Pinning** — pass `account` to target one, or keep the `conversation` handle to stay on
  the account that owns the chat.
- **Keep-alive** — a background loop refreshes the session every ~20 minutes (with jitter)
  and persists whatever Google hands back.
- **Dead sessions** — Google answers HTTP 200 even for an expired session, so the server
  checks for the session token rather than the status code. A dead account is flagged
  `needsReharvest` and taken out of rotation for an hour; run the extension on a signed-in
  tab to revive it.

### The cookie bridge

While the server runs it also listens on `http://127.0.0.1:9478` for cookie hand-offs
from the extension. The endpoint and a random token are printed to **stderr** on start-up
(the MCP protocol owns stdout) and are also available from the `gemini_bridge` tool. The
token is required on every request, and the listener is bound to the loopback interface
only, so nothing outside your machine can reach it.

---

## Privacy

The server talks only to `gemini.google.com` and `accounts.google.com`. Cookies are read
from your own `accounts.json` and never written back, logged, or sent anywhere else. The
file lives outside the repository and outside your working directory. The browser
extension only reads `.google.com` cookies and only posts to `127.0.0.1`.

---

## Building from source

```bash
dotnet publish -c Release -r win-x64
```

Targets: `win-x64`, `win-arm64`, `osx-arm64`, `linux-x64`, `linux-arm64`, `linux-musl-x64`.

```bash
dotnet pack -c Release
```

## More information

- [Official MCP documentation](https://modelcontextprotocol.io/)
- [MCP C# SDK](https://csharp.sdk.modelcontextprotocol.io/)
- [NuGet MCP publishing guide](https://aka.ms/nuget/mcp/guide)
