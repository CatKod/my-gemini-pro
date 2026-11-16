using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using MyGeminiWebMcp.Accounts;

namespace MyGeminiWebMcp.Bridge;

/// <summary>
/// A loopback HTTP endpoint that receives Gemini cookies from a browser extension.
/// </summary>
/// <remarks>
/// <para>
/// Chrome and Edge encrypt their cookie store on disk with app-bound encryption, so no
/// process outside the browser can read the session cookies. A browser extension can: it asks
/// the browser through <c>chrome.cookies</c>, which decrypts on the browser's behalf, and
/// posts the result here.
/// </para>
/// <para>
/// The endpoint binds to <c>127.0.0.1</c> only — it is unreachable from the network — and
/// every request must carry the shared token, so a random web page cannot feed cookies into
/// the pool. The token is printed to stderr, never to stdout, because stdout carries the MCP
/// protocol.
/// </para>
/// </remarks>
internal sealed class CookieBridge(AccountPool pool, string tokenPath, ILogger? logger = null) : IDisposable
{
    /// <summary>The default loopback port.</summary>
    public const int DefaultPort = 9478;

    private readonly AccountPool _pool = pool;
    private HttpListener _listener = new();
    private readonly CancellationTokenSource _stop = new();
    private readonly string _tokenPath = tokenPath;
    private readonly AccountEmailResolver _emailResolver = new();
    private Task? _loop;

    /// <summary>The port the bridge listens on.</summary>
    public int Port { get; private set; }

    /// <summary>The shared secret the extension must present.</summary>
    public string Token { get; } = LoadOrCreateToken(tokenPath);

    /// <summary>Resolves the account email behind a harvested session, for pool matching.</summary>
    public AccountEmailResolver EmailResolver => _emailResolver;

    /// <summary>Whether the bridge is currently accepting requests.</summary>
    public bool Running => _listener.IsListening;

    /// <summary>The URL the extension posts to.</summary>
    public string Endpoint => $"http://127.0.0.1:{Port}/harvest";

    /// <summary>
    /// Reads the persisted token, or creates one when none exists.
    /// </summary>
    /// <remarks>
    /// The token outlives the process on purpose. An MCP server is started fresh by every
    /// client session, so a token that changed on every start would force the user to
    /// paste it into the extension again each time — the exact friction the bridge is
    /// meant to remove.
    /// </remarks>
    private static string LoadOrCreateToken(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                string existing = File.ReadAllText(path).Trim();
                if (existing.Length > 0)
                    return existing;
            }
        }
        catch (IOException)
        {
            // Fall through and mint a new one.
        }
        catch (UnauthorizedAccessException)
        {
            // Fall through and mint a new one.
        }

        string token = GenerateToken();
        try
        {
            string? directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory))
                Directory.CreateDirectory(directory);
            File.WriteAllText(path, token);
        }
        catch (IOException)
        {
            // The token still works for this process even when it cannot be persisted.
        }
        catch (UnauthorizedAccessException)
        {
            // Same.
        }

        return token;
    }

    /// <summary>
    /// Starts the bridge on the given port.
    /// </summary>
    /// <param name="port">
    /// The preferred port. When it is already taken — a previous server instance that did not
    /// shut down cleanly, for example — the bridge walks forward to the next free port rather
    /// than failing: the chat tools work without the bridge, but a dead bridge means the user
    /// can never hand over fresh cookies again.
    /// </param>
    public void Start(int port = DefaultPort)
    {
        if (_listener.IsListening)
            return;

        Exception? lastError = null;
        for (int candidate = port; candidate < port + 20; candidate++)
        {
            try
            {
                Port = candidate;
                _listener.Prefixes.Clear();
                _listener.Prefixes.Add($"http://127.0.0.1:{candidate}/");
                _listener.Start();
                lastError = null;
                break;
            }
            catch (HttpListenerException ex)
            {
                lastError = ex;
                // The listener is unusable after a failed Start; build a fresh one.
                _listener.Close();
                _listener = new HttpListener();
            }
        }

        if (lastError is not null)
            throw lastError;

        _loop = Task.Run(async () =>
        {
            while (!_stop.IsCancellationRequested)
            {
                HttpListenerContext context;
                try
                {
                    context = await _listener.GetContextAsync();
                }
                catch (HttpListenerException)
                {
                    break;
                }
                catch (ObjectDisposedException)
                {
                    break;
                }

                // Handle off the loop so a slow request cannot block the next one.
                _ = Task.Run(() => HandleAsync(context));
            }
        });

        logger?.LogInformation(
            "Cookie bridge listening on {Endpoint} (token {Token})", Endpoint, Token);

        WriteConfigFile();
    }

    /// <summary>
    /// Writes the endpoint and token to a small JSON file next to the pool.
    /// </summary>
    /// <remarks>
    /// The extension needs two values, and both change rarely — the token never, the port
    /// only when the preferred one is taken. Writing them to disk means the user (or an
    /// agent driving the browser) can read them without parsing the server's stderr.
    /// </remarks>
    private void WriteConfigFile()
    {
        try
        {
            string? directory = Path.GetDirectoryName(_tokenPath);
            if (!string.IsNullOrEmpty(directory))
                Directory.CreateDirectory(directory);

            var config = new JsonObject
            {
                ["endpoint"] = Endpoint,
                ["baseUrl"] = $"http://127.0.0.1:{Port}",
                ["token"] = Token,
                ["port"] = Port,
                ["ping"] = $"http://127.0.0.1:{Port}/ping",
            };

            File.WriteAllText(
                Path.Combine(directory ?? ".", "bridge_config.json"),
                config.ToJsonString(new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
        }
        catch (IOException)
        {
            // The config file is a convenience; the stderr output still carries the values.
        }
        catch (UnauthorizedAccessException)
        {
            // Same.
        }
    }

    /// <summary>Stops the bridge.</summary>
    public void Stop()
    {
        _stop.Cancel();
        if (_listener.IsListening)
            _listener.Stop();
    }

    /// <summary>Stops the bridge and releases the listener.</summary>
    public void Dispose()
    {
        Stop();
        _listener.Close();
        _stop.Dispose();
    }

    private async Task HandleAsync(HttpListenerContext context)
    {
        try
        {
            string path = context.Request.Url?.AbsolutePath ?? "";

            if (context.Request.HttpMethod == "OPTIONS")
            {
                await WriteJsonAsync(context, 204, new JsonObject());
                return;
            }

            if (context.Request.HttpMethod == "GET" && path == "/ping")
            {
                await WriteJsonAsync(context, 200, new JsonObject
                {
                    ["ok"] = true,
                    ["service"] = "my-geminiweb-mcp-bridge",
                });
                return;
            }

            if (context.Request.HttpMethod == "GET" && path == "/config")
            {
                // The endpoint and token, for the extension to pick up automatically.
                //
                // Why this is safe: a web page cannot read the answer. Chrome only lets a
                // cross-origin response be read when the server sends
                // `Access-Control-Allow-Origin`, and this server sends that header only for
                // an extension origin. A page on the internet can therefore issue the
                // request but cannot see the reply. The extension is allowed to read it
                // because its host permission makes Chrome skip the CORS check for it.
                //
                // A local process can read it too — but such a process can already read
                // accounts.json, which holds the very same cookies.
                await WriteJsonAsync(context, 200, new JsonObject
                {
                    ["ok"] = true,
                    ["endpoint"] = Endpoint,
                    ["baseUrl"] = $"http://127.0.0.1:{Port}",
                    ["token"] = Token,
                    ["port"] = Port,
                });
                return;
            }

            if (context.Request.HttpMethod != "POST" || path != "/harvest")
            {
                await WriteJsonAsync(context, 404, new JsonObject
                {
                    ["ok"] = false,
                    ["error"] = "not found",
                });
                return;
            }

            string body;
            using (var reader = new StreamReader(context.Request.InputStream, Encoding.UTF8))
                body = await reader.ReadToEndAsync();

            var payload = JsonNode.Parse(body) as JsonObject;
            if (payload is null)
            {
                await WriteJsonAsync(context, 400, new JsonObject
                {
                    ["ok"] = false,
                    ["error"] = "malformed JSON",
                });
                return;
            }

            // Authorisation: the shared token. The extension learns it once from /config
            // and stores it, so it is presented on every harvest.
            string token = (string?)payload["token"] ?? "";
            if (token.Length == 0 || !FixedTimeEquals(token, Token))
            {
                await WriteJsonAsync(context, 403, new JsonObject
                {
                    ["ok"] = false,
                    ["error"] = "bad token",
                });
                return;
            }

            var result = await Ingest(payload);
            await WriteJsonAsync(context, result["ok"]?.GetValue<bool>() == true ? 200 : 400, result);
        }
        catch (Exception ex)
        {
            logger?.LogWarning(ex, "Cookie bridge request failed.");
            try
            {
                await WriteJsonAsync(context, 500, new JsonObject
                {
                    ["ok"] = false,
                    ["error"] = ex.Message,
                });
            }
            catch (Exception)
            {
                // The client is gone; nothing left to report to.
            }
        }
    }

    /// <summary>
    /// Stores a harvested cookie set in the pool.
    /// </summary>
    /// <remarks>
    /// The account is identified by the name the extension sent, or — when the extension
    /// could not tell — by matching the <c>__Secure-1PSID</c> value against the accounts
    /// already in the pool. An unknown account creates a new entry rather than being dropped:
    /// losing a working session because the label was wrong would be worse than an extra entry.
    /// </remarks>
    private async Task<JsonObject> Ingest(JsonObject payload)
    {
        string psid = (string?)payload["psid"] ?? "";
        string psidts = (string?)payload["psidts"] ?? "";
        string? email = (string?)payload["email"];
        string? name = (string?)payload["account"];

        if (psid.Length == 0)
        {
            return new JsonObject
            {
                ["ok"] = false,
                ["error"] = "missing __Secure-1PSID",
            };
        }

        // The cookies that are not session ids: they are shared by every account in the
        // browser profile, so they ride along with whichever session is picked.
        var shared = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (payload["cookies"] is JsonObject extras)
        {
            foreach (var (key, value) in extras)
            {
                if (value is null)
                    continue;
                string v = value.GetValue<string>();
                if (!string.IsNullOrEmpty(v) && !IsSessionId(key))
                    shared[key] = v;
            }
        }

        // Several Google accounts in one Chrome profile means several session ids in
        // the cookie store, distinguished by their path. Only one of them belongs to the
        // account that is currently active. The extension therefore sends every variant
        // it found, and the server picks the one that actually resolves to an account.
        var candidates = BuildCandidates(payload, psid, psidts, shared);
        string? resolvedEmail = email;

        // Every candidate that resolves to a real Google account is a real account in
        // this browser profile. Storing all of them is the whole point of the bridge:
        // one harvest from Chrome heals the whole pool at once, instead of the user
        // having to switch accounts one by one.
        var found = new List<(string Email, Dictionary<string, string> Cookies)>();

        // The extension pairs each email with the cookies of the very tab it read the
        // email from. That pairing is authoritative: it tells us which session belongs
        // to which account without having to guess.
        if (payload["sessions"] is JsonArray sessions)
        {
            foreach (var session in sessions.OfType<JsonObject>())
            {
                string? sessionEmail = (string?)session["email"];
                string sessionUrl = (string?)session["url"] ?? "";

                var sessionCookies = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                if (session["cookies"] is JsonObject sessionJar)
                {
                    foreach (var (key, value) in sessionJar)
                    {
                        if (value is null)
                            continue;
                        string v = value.GetValue<string>();
                        if (!string.IsNullOrEmpty(v))
                            sessionCookies[key] = v;
                    }
                }

                if (!sessionCookies.ContainsKey("__Secure-1PSID"))
                    continue;

                // The content script could not read the account switcher. The server can
                // still answer, as long as it asks the slot URL the tab was on.
                if (string.IsNullOrWhiteSpace(sessionEmail))
                    sessionEmail = await _emailResolver.ResolveAsync(sessionCookies, sessionUrl);

                if (string.IsNullOrWhiteSpace(sessionEmail))
                    continue;

                string sessionPsidts = sessionCookies.GetValueOrDefault("__Secure-1PSIDTS", "");
                logger?.LogInformation(
                    "Cookie bridge: session email={Email} psidts={Psidts} url={Url} cookies={Count} paths={Paths}",
                    sessionEmail,
                    Head(sessionPsidts, 28),
                    sessionUrl,
                    sessionCookies.Count,
                    string.Join(", ", (session["paths"] as JsonArray)?.OfType<JsonValue>().Select(v => v.GetValue<string>()) ?? Enumerable.Empty<string>()));

                found.Add((sessionEmail, sessionCookies));
            }
        }

        foreach (var (candidate, candidateUrl) in candidates)
        {
            // The extension reports the account that is active right now. Its session
            // is the one the browser is using, so it needs no probing.
            string? candidateEmail;
            if (!string.IsNullOrWhiteSpace(resolvedEmail)
                && candidate["__Secure-1PSID"] == psid
                && found.Count == 0)
            {
                candidateEmail = resolvedEmail;
            }
            else
            {
                // The URL of the tab the cookies came from decides which account slot
                // Google answers for. Without it, every session resolves to the primary
                // account and the secondary accounts get the wrong session.
                candidateEmail = await _emailResolver.ResolveAsync(candidate, candidateUrl);
                logger?.LogInformation(
                    "Cookie bridge: candidate session {Psid} -> {Email}",
                    Head(candidate["__Secure-1PSID"] ?? "", 16),
                    string.IsNullOrWhiteSpace(candidateEmail) ? "(unresolved)" : candidateEmail);
            }

            if (string.IsNullOrWhiteSpace(candidateEmail))
                continue;

            // The same account can show up twice (the "1" and the "3" family of
            // the same session). Keep the first one.
            if (found.Any(f => string.Equals(f.Email, candidateEmail, StringComparison.OrdinalIgnoreCase)))
                continue;

            found.Add((candidateEmail, candidate));
        }

        var stored = new List<string>();

        foreach (var (accountEmail, set) in found)
        {
            // How the account is identified, in order of authority:
            //
            //   1. The name the extension sent. The user can pin it explicitly.
            //   2. The Google account email. This is the durable identity: Google issues a
            //      new __Secure-1PSID whenever a session rotates, so a harvest that arrives
            //      with a fresh session id cannot be matched by session id at all.
            //   3. The session id itself, as a last resort before creating a new entry.
            //
            // The email must be resolved before the session-id lookup, otherwise a stale
            // entry that happens to hold the same session id would swallow the harvest and
            // the real account would never be healed.
            string? accountName = name;
            if (string.IsNullOrWhiteSpace(accountName))
                accountName = _pool.FindByEmail(accountEmail);

            if (string.IsNullOrWhiteSpace(accountName))
                accountName = _pool.FindByPsid(set["__Secure-1PSID"]);

            if (string.IsNullOrWhiteSpace(accountName))
            {
                // Not a known account: create one named after the email, or after a short
                // fingerprint of the session id so the entry is at least recognisable.
                accountName = accountEmail.Split('@')[0];
            }

            var account = _pool.Upsert(accountName!, accountEmail, set);
            stored.Add(account.Name);

            logger?.LogInformation(
                "Cookie bridge: stored account {Account} ({CookieCount} cookies).",
                account.Name, set.Count);
        }

        if (payload["debug"] is JsonArray debug)
        {
            logger?.LogInformation(
                "Cookie bridge: browser cookies -> {Debug}",
                string.Join(", ", debug.OfType<JsonObject>().Select(d =>
                    $"{d["name"]?.GetValue<string>()}@{d["path"]?.GetValue<string>()}[{d["storeId"]?.GetValue<string>()}]={d["prefix"]?.GetValue<string>()}")));
        }

        if (payload["stores"] is JsonArray rawStores)
        {
            logger?.LogInformation(
                "Cookie bridge: {Count} store(s) -> {Stores}",
                rawStores.Count,
                string.Join(" | ", rawStores.OfType<JsonObject>().Select(s =>
                    $"{s["storeId"]?.GetValue<string>()}:n={s["cookieCount"]}:tabs={string.Join(",", (s["tabIds"] as JsonArray)?.Select(t => t?.GetValue<int>()) ?? Enumerable.Empty<int?>())}:{Tail((string?)s["psidts"] ?? "", 12)}")));
        }

        string primary = stored.Count > 0 ? stored[0] : (name ?? "");
        string? primaryEmail = found.Count > 0 ? found[0].Email : resolvedEmail;

        return new JsonObject
        {
            ["ok"] = true,
            ["account"] = primary,
            ["email"] = primaryEmail,
            ["accounts"] = new JsonArray(stored.Select(s => (JsonNode)s).ToArray()),
            ["cookieCount"] = found.Count > 0 ? found[0].Cookies.Count : 0,
            ["hasPsidts"] = !string.IsNullOrEmpty(psidts),
        };
    }

    /// <summary>Whether a cookie name belongs to the Google session-id family.</summary>
    private static bool IsSessionId(string name) =>
        name.StartsWith("__Secure-1PSID", StringComparison.Ordinal)
        || name.StartsWith("__Secure-3PSID", StringComparison.Ordinal);

    /// <summary>Builds a complete cookie set from a session id and the shared cookies.</summary>
    private static Dictionary<string, string> BuildCookieSet(
        string psid,
        string psidts,
        IReadOnlyDictionary<string, string> shared)
    {
        var set = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["__Secure-1PSID"] = psid,
        };
        if (!string.IsNullOrEmpty(psidts))
            set["__Secure-1PSIDTS"] = psidts;

        foreach (var (key, value) in shared)
            set[key] = value;

        return set;
    }

    /// <summary>
    /// Builds the candidate session sets the extension sent, one per session id found.
    /// </summary>
    /// <remarks>
    /// A Chrome profile can hold several Google accounts at once. Google then keeps one
    /// session id per account and only the account that is currently active gets a
    /// working session on <c>gemini.google.com</c>. The extension cannot tell them
    /// apart, so the server tries them and keeps the one that resolves to a real
    /// account.
    ///
    /// The grouping is by cookie family, not by path: Google stores every session on
    /// <c>.google.com</c> at path <c>/</c>. The "1" family (<c>__Secure-1PSID</c>)
    /// belongs to the account that owns the browser session, the "3" family
    /// (<c>__Secure-3PSID</c>) to the accounts reached through a third-party context
    /// such as the Gemini account switcher. Each family carries its own timestamp
    /// cookie, so the two must be kept apart.
    /// </remarks>
    private static List<(Dictionary<string, string> Cookies, string Url)> BuildCandidates(
        JsonObject payload,
        string psid,
        string psidts,
        IReadOnlyDictionary<string, string> shared)
    {
        var candidates = new List<(Dictionary<string, string> Cookies, string Url)>();

        // The session id the extension used, plus its timestamp cookie.
        candidates.Add((BuildCookieSet(psid, psidts, shared), ""));

        if (payload["variants"] is not JsonArray variants)
            return candidates;

        // One entry per cookie family: "1" and "3".
        var ids = new Dictionary<string, (string Psid, string Psidts)>(StringComparer.Ordinal);

        foreach (var item in variants.OfType<JsonObject>())
        {
            string name = (string?)item["name"] ?? "";
            string value = (string?)item["value"] ?? "";
            if (name.Length == 0 || value.Length == 0)
                continue;

            string family = FamilyOf(name);
            if (family.Length == 0)
                continue;

            if (name.EndsWith("TS", StringComparison.Ordinal))
            {
                if (ids.TryGetValue(family, out var existing))
                    ids[family] = (existing.Psid, value);
                else
                    ids[family] = ("", value);
            }
            else if (name.EndsWith("PSID", StringComparison.Ordinal))
            {
                if (ids.TryGetValue(family, out var existing))
                    ids[family] = (value, existing.Psidts);
                else
                    ids[family] = (value, "");
            }
        }

        foreach (var (psidValue, psidtsValue) in ids.Values)
        {
            if (psidValue.Length == 0 || string.Equals(psidValue, psid, StringComparison.Ordinal))
                continue;

            var set = BuildCookieSet(psidValue, psidtsValue, shared);

            // The timestamp of the other family does not belong to this session.
            foreach (string key in set.Keys.ToList())
            {
                if (key.StartsWith("__Secure-3PSID", StringComparison.Ordinal)
                    && !psidValue.StartsWith("__Secure-3PSID", StringComparison.Ordinal))
                    set.Remove(key);
            }

            candidates.Add((set, ""));
        }

        // The extension also probes the `/u/<n>` paths directly: each Google account
        // that is not the browser's main one keeps its session on `.google.com` under
        // its own path, so reading the cookies for that URL yields that account's
        // session id.
        if (payload["slots"] is JsonArray slots)
        {
            foreach (var slot in slots.OfType<JsonObject>())
            {
                string slotPsid = (string?)slot["psid"] ?? "";
                if (slotPsid.Length == 0 || string.Equals(slotPsid, psid, StringComparison.Ordinal))
                    continue;

                int slotSlot = (int?)slot["slot"] ?? 0;
                string slotPsidts = (string?)slot["psidts"] ?? "";
                string slotPsid3 = (string?)slot["psid3"] ?? "";
                string slotPsid3ts = (string?)slot["psid3ts"] ?? "";

                var set = BuildCookieSet(slotPsid, slotPsidts, shared);
                if (slotPsid3.Length > 0)
                {
                    set["__Secure-3PSID"] = slotPsid3;
                    if (slotPsid3ts.Length > 0)
                        set["__Secure-3PSIDTS"] = slotPsid3ts;
                }

                candidates.Add((set, $"https://gemini.google.com/u/{slotSlot}/app"));
            }
        }

        // Chrome partitions the cookies of every signed-in Google account into its own
        // store. The extension reads each store separately and sends the session ids
        // it found, so every account in the profile can be healed from one harvest.
        if (payload["stores"] is JsonArray stores)
        {
            foreach (var store in stores.OfType<JsonObject>())
            {
                string storePsid = (string?)store["psid"] ?? "";
                if (storePsid.Length == 0 || string.Equals(storePsid, psid, StringComparison.Ordinal))
                    continue;

                string storePsidts = (string?)store["psidts"] ?? "";
                string storePsid3 = (string?)store["psid3"] ?? "";
                string storePsid3ts = (string?)store["psid3ts"] ?? "";

                var set = BuildCookieSet(storePsid, storePsidts, shared);
                if (storePsid3.Length > 0)
                {
                    set["__Secure-3PSID"] = storePsid3;
                    if (storePsid3ts.Length > 0)
                        set["__Secure-3PSIDTS"] = storePsid3ts;
                }

                candidates.Add((set, ""));
            }
        }

        return candidates;
    }

    /// <summary>
    /// Returns the cookie family of a Google session cookie: "1", "3" or "".
    /// </summary>
    private static string FamilyOf(string name)
    {
        if (name.StartsWith("__Secure-1PSID", StringComparison.Ordinal))
            return "1";
        if (name.StartsWith("__Secure-3PSID", StringComparison.Ordinal))
            return "3";
        return "";
    }

    private static async Task WriteJsonAsync(HttpListenerContext context, int status, JsonObject payload)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(payload.ToJsonString());
        context.Response.StatusCode = status;
        context.Response.ContentType = "application/json; charset=utf-8";

        // CORS: only the extension may read the response. A wildcard would let any web page
        // on the internet read the bridge's answers (including the token in /config).
        //
        // An extension that holds a host permission is exempt from the CORS check, so it
        // reads the answer regardless of this header. A plain web page is not: it can send
        // the request, but without this header it cannot read the reply.
        string origin = context.Request.Headers["Origin"] ?? "";
        if (origin.StartsWith("chrome-extension://", StringComparison.Ordinal)
            || origin.StartsWith("moz-extension://", StringComparison.Ordinal))
        {
            context.Response.Headers.Add("Access-Control-Allow-Origin", origin);
            context.Response.Headers.Add("Vary", "Origin");
        }
        context.Response.Headers.Add("Access-Control-Allow-Headers", "Content-Type");
        context.Response.ContentLength64 = bytes.Length;
        await context.Response.OutputStream.WriteAsync(bytes);
        context.Response.Close();
    }

    /// <summary>Compares two secrets without leaking their contents through timing.</summary>
    private static bool FixedTimeEquals(string a, string b)
    {
        if (a.Length != b.Length)
            return false;

        int diff = 0;
        for (int i = 0; i < a.Length; i++)
            diff |= a[i] ^ b[i];
        return diff == 0;
    }

    /// <summary>Generates a URL-safe random token.</summary>
    private static string GenerateToken()
    {
        byte[] bytes = RandomNumberGenerator.GetBytes(24);
        return Convert.ToBase64String(bytes)
            .Replace('+', '-')
            .Replace('/', '_')
            .TrimEnd('=');
    }

    /// <summary>Produces a short, stable fingerprint of a value.</summary>
    private static string ShortHash(string value)
    {
        byte[] hash = System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(value));
        return Convert.ToHexString(hash)[..6].ToLowerInvariant();
    }

    /// <summary>First <paramref name="max"/> characters, safe for short strings.</summary>
    private static string Head(string value, int max) =>
        value.Length <= max ? value : value[..max];

    /// <summary>Last <paramref name="max"/> characters, safe for short strings.</summary>
    private static string Tail(string value, int max) =>
        value.Length <= max ? value : value[^max..];
}
