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
internal sealed class CookieBridge(AccountPool pool, ILogger? logger = null) : IDisposable
{
    /// <summary>The default loopback port.</summary>
    public const int DefaultPort = 9478;

    private readonly HttpListener _listener = new();
    private readonly CancellationTokenSource _stop = new();
    private Task? _loop;

    /// <summary>The port the bridge listens on.</summary>
    public int Port { get; private set; }

    /// <summary>The shared secret the extension must present.</summary>
    public string Token { get; } = GenerateToken();

    /// <summary>Whether the bridge is currently accepting requests.</summary>
    public bool Running => _listener.IsListening;

    /// <summary>The URL the extension posts to.</summary>
    public string Endpoint => $"http://127.0.0.1:{Port}/harvest";

    /// <summary>
    /// Starts the bridge on the given port.
    /// </summary>
    /// <param name="port">The port to bind. Zero picks a free one.</param>
    public void Start(int port = DefaultPort)
    {
        if (_listener.IsListening)
            return;

        Port = port;
        _listener.Prefixes.Clear();
        _listener.Prefixes.Add($"http://127.0.0.1:{port}/");
        _listener.Start();

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

            var result = Ingest(payload);
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
    private JsonObject Ingest(JsonObject payload)
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

        if (string.IsNullOrWhiteSpace(name))
            name = pool.FindByPsid(psid);

        if (string.IsNullOrWhiteSpace(name))
        {
            // Not a known account: create one named after the email, or after a short
            // fingerprint of the session id so the entry is at least recognisable.
            string label = !string.IsNullOrWhiteSpace(email)
                ? email!.Split('@')[0]
                : "acc-" + ShortHash(psid);
            name = label;
        }

        var cookies = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["__Secure-1PSID"] = psid,
        };
        if (!string.IsNullOrEmpty(psidts))
            cookies["__Secure-1PSIDTS"] = psidts;

        if (payload["cookies"] is JsonObject extras)
        {
            foreach (var (key, value) in extras)
            {
                if (value is null)
                    continue;
                string v = value.GetValue<string>();
                if (!string.IsNullOrEmpty(v))
                    cookies[key] = v;
            }
        }

        var account = pool.Upsert(name!, email, cookies);

        logger?.LogInformation(
            "Cookie bridge: stored account {Account} ({CookieCount} cookies).",
            account.Name, cookies.Count);

        return new JsonObject
        {
            ["ok"] = true,
            ["account"] = account.Name,
            ["email"] = account.Email,
            ["cookieCount"] = cookies.Count,
            ["hasPsidts"] = !string.IsNullOrEmpty(psidts),
        };
    }

    private static async Task WriteJsonAsync(HttpListenerContext context, int status, JsonObject payload)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(payload.ToJsonString());
        context.Response.StatusCode = status;
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.Headers.Add("Access-Control-Allow-Origin", "*");
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
}
