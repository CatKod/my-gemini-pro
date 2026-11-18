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
| `gemini_models` | List the models this account is actually offered (discovered live from Gemini). |
| `gemini_accounts` | List the configured accounts and their health. |
| `gemini_check` | Verify that each account can actually reach Gemini. |
| `gemini_refresh` | Keep the sessions alive by refreshing the expiring cookie. |
| `gemini_bridge` | Show the endpoint and token the browser extension needs. |

### `gemini_chat`

```jsonc
{
  "prompt": "Explain this code",
  "account": "account1",       // optional: pin to one account (strict — fails instead of switching accounts)
  "conversation": "{...}",     // optional: handle from a previous reply
  "model": "4 Hydrogen",       // optional: any name from gemini_models, or "default"
  "attachments": ["./a.py"],  // optional: local file paths to upload with this turn
  "extendedThinking": false    // optional: request extended thinking
}
```

Returns:

```jsonc
{
  "account": "account1",
  "reply": "…",
  "conversation": "{\"account\":\"account1\",\"cid\":\"c_…\",\"rid\":\"r_…\"}",
  "thoughts": null,
  "files": [                   // only when Gemini generates downloadable files
    {"fileName": "report.xlsx", "savedPath": "…/downloads/report.xlsx", "size": 1234, "mimeType": "…", "source": "blob"}
  ]
}
```

**Keep the `conversation` handle.** It pins the follow-up turn to the account that owns
the conversation — a Gemini conversation id is meaningless on a different account.

**Pinning is strict.** Passing `account` targets exactly that account: when it is dead
the call fails with a clear error instead of silently answering from another account.
Omit `account` to let the pool rotate and fail over automatically.

**Models are discovered, not hardcoded.** Google renames, renumbers and re-tiers models
without notice — on 2026-11-16 `flash` had already been replaced by `hydrogen`, and on
2026-11-18 `4 Carbon` was renamed to `4 Argon` while the model list kept the old name.
A hardcoded table therefore goes stale, and a stale id does not produce an error: Google
opens the stream and then never answers it.

The server therefore asks Gemini which models the account is offered (the `GET_USER_STATUS`
RPC) and sends the id, capacity tier and model number that came back. Call
`gemini_models` to see the list; `gemini_chat` accepts any of these spellings of a model:

| Spelling | Example |
|---|---|
| canonical name | `gemini-4-hydrogen` |
| UI label | `4 Hydrogen` |
| description | `3.1 Pro` |
| localised display name | `Trợ giúp hằng ngày` |
| internal id | `56fdd199312815e2` |

**A model the account may not select is refused up front.** Gemini lists such a model
too, but with an empty tier list — the account sees it in the picker and cannot use it.
Asking for it makes Google open a stream that never answers, so the server refuses the
request immediately instead of waiting. `gemini_models` marks those models with
`"selectable": false` and lists them under `unselectable`:

```jsonc
{
  "models": [
    { "name": "gemini-carbon", "description": "4 Carbon", "currentName": "4 Argon",
      "selectable": false }
  ],
  "unselectable": ["4 Carbon"]
}
```

`currentName` is the name the current launch lineup uses; both spellings resolve, so
`4 Carbon` and `4 Argon` reach the same model. The same model must be used for the whole
conversation. A name that resolves to nothing comes back as data, not as a crash:

```jsonc
{
  "account": "account1",
  "error": "Unknown model 'xyz'. Available for this account: …",
  "availableModels": ["…"]
}
```

Values that look like a JSPB array (`[...]`) are passed through unchanged.

**A stalled stream is retried without a model header.** Two watchdogs guard the turn:

- *idle timeout* (120 s) — the stream opened but sent nothing. That is what a stale
  model id looks like: Google accepts the request and never answers it.
- *first-token budget* (300 s) — the stream is alive but produces no answer. A model the
  account may not use (a locked one) behaves like this: Google keeps the stream open and
  sends keep-alive frames for minutes. Verified 2026-11-18: a request for the locked
  `4 Carbon` model held the stream open for **901 s** before the idle timeout ever fired.
  The budget cuts that to 300 s, after which the server falls back to the account default
  and reports `fallbackReason`.

Both timeouts only guard the *first* token. Once the answer starts, a long Deep Research
turn is not cut short.

**The reply says which model actually answered.** `gemini_chat` returns `servedModel`
(e.g. `"3.5 Flash-Lite"`) so a caller never assumes it got the model it asked for. When
the requested model's quota is exhausted, Google silently answers with a cheaper model
and says so in a note — that note comes back as `quotaNotice`, e.g.
`"Limit reached. Continuing with Flash-Lite."`. Verified 2026-11-18: probing `hydrogen`
and `carbon` on a drained quota returned a Flash-Lite answer while reporting `3.5 Flash-Lite`.
When the server itself falls back, `servedModel` is `"account default"` and
`fallbackReason` explains why.

`servedModelConfirmed` says whether that name was **read from the reply payload** or is
the model that was *requested*. Google only echoes a model id on some replies — a
short answer on `4 Hydrogen` carried no model id at all — so without this flag the
caller cannot tell a confirmed name from a guess. Verified 2026-11-19: `gemini-flash-lite`
and `gemini-pro` come back with `servedModelConfirmed: true` (the id was in the payload);
a reply without an echoed id comes back with `servedModelConfirmed: false` and the
requested model as the best available answer.

**Attachments.** Files are uploaded to `content-push.googleapis.com/upload`
(`X-Tenant-Id: bard-storage`) and referenced from the chat payload, so Gemini reads
their real content — verified with a `.py` file (`Circle` / `987654`).

**Generated files.** When Gemini produces `.xlsx`/`.pdf`/`.csv`, the raw candidate
carries a `[file-tag: …]` marker with the filename, an optional base64 blob, and a
`contribution.usercontent.google.com` download URL. Both shapes are handled and saved
under `%LOCALAPPDATA%\\MyGeminiWebMcp\\downloads\\` (override with `GEMINIWEB_MCP_HOME`).

**Retries.** Transient Google errors (1013, 1097, temporary-service messages) are
retried up to 3 times with 5s/10s backoff. Usage-limit (1037), inconsistent-model
(1050/1052) and IP-block (1060) errors fail fast with a readable message.

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
