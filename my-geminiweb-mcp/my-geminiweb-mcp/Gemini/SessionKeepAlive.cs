using System.Net;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using MyGeminiWebMcp.Accounts;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// Keeps every account's session alive and persists whatever Google hands back.
/// </summary>
/// <remarks>
/// <para>
/// Google expires an idle session within hours, and the expiry is silent: a request with an
/// expired <c>__Secure-1PSIDTS</c> still gets HTTP 200 from <c>gemini.google.com/app</c>, but
/// the page carries no session token, so the failure shows up as an empty reply rather than
/// an error. This service therefore does the work of noticing that state and recovering from
/// it before a user request is ever affected.
/// </para>
/// <para>
/// Two mechanisms are combined, both verified against the live site:
/// </para>
/// <list type="bullet">
/// <item>
/// <c>GET /app</c> — the response carries a fresh <c>__Secure-1PSIDTS</c> when Google
/// decides to issue one, plus the <c>__Secure-3PSID</c> family. This is where the new
/// short-lived cookie actually comes from; it is persisted to the pool file.
/// </item>
/// <item>
/// <c>POST accounts.google.com/RotateCookies</c> — pushes the server-side session expiry
/// back. It does not return a new <c>__Secure-1PSIDTS</c>, so it is a complement to the
/// request above rather than a replacement for it.
/// </item>
/// </list>
/// <para>
/// The loop runs on a timer with jitter, so several accounts on one machine do not all hit
/// Google at the same instant.
/// </para>
/// </remarks>
internal sealed class SessionKeepAlive(
    AccountPool pool,
    HttpClient? http = null,
    ILogger? logger = null) : IDisposable
{
    /// <summary>How often the keep-alive runs.</summary>
    public static readonly TimeSpan Interval = TimeSpan.FromMinutes(20);

    private readonly HttpClient _http = http ?? new HttpClient(new HttpClientHandler
    {
        UseCookies = false,
        AllowAutoRedirect = true,
        AutomaticDecompression =
            DecompressionMethods.GZip | DecompressionMethods.Deflate | DecompressionMethods.Brotli,
    })
    {
        Timeout = TimeSpan.FromMinutes(2),
    };

    private readonly CancellationTokenSource _stop = new();
    private Task? _loop;

    /// <summary>Starts the background loop.</summary>
    public void Start()
    {
        if (_loop is not null)
            return;

        _loop = Task.Run(async () =>
        {
            // A small random delay keeps the accounts from synchronising with each other.
            await Task.Delay(Random.Shared.Next(2, 12) * 1000, _stop.Token);

            while (!_stop.IsCancellationRequested)
            {
                try
                {
                    await RunOnceAsync(_stop.Token);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    logger?.LogWarning(ex, "Session keep-alive failed.");
                }

                // Jitter of +/- 15 minutes around the nominal interval.
                int minutes = (int)Interval.TotalMinutes + Random.Shared.Next(-15, 16);
                minutes = Math.Clamp(minutes, 5, 240);
                try
                {
                    await Task.Delay(TimeSpan.FromMinutes(minutes), _stop.Token);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        });
    }

    /// <summary>
    /// Runs one keep-alive pass over every account.
    /// </summary>
    /// <returns>A per-account report.</returns>
    public async Task<JsonArray> RunOnceAsync(CancellationToken ct = default)
    {
        var results = new JsonArray();

        foreach (var status in pool.Status())
        {
            var account = pool.Get(status.Name);
            if (account is null)
                continue;

            var entry = new JsonObject { ["account"] = account.Name };
            try
            {
                using var client = new GeminiWebClient(
                    account.Psid,
                    account.Psidts,
                    _http,
                    account.ExtraCookies);

                // The bootstrap request is what returns a fresh __Secure-1PSIDTS.
                // The full jar is sent so the response carries the whole __Secure-3PSID
                // family back, which is what keeps the session worth persisting.
                await client.InitializeAsync(fullJar: true, ct);

                // Then push the server-side expiry back.
                await client.RotateCookiesAsync(account.Name, ct);

                string? psidts = client.Cookies.Get("__Secure-1PSIDTS");
                bool changed = !string.IsNullOrEmpty(psidts) && psidts != account.Psidts;

                if (changed)
                {
                    var extras = client.Cookies.All
                        .Where(c => c.Name is not ("__Secure-1PSID" or "__Secure-1PSIDTS"))
                        .Select(c => new KeyValuePair<string, string>(c.Name, c.Value))
                        .ToList();

                    pool.UpdateCookies(account, psidts!, extras);
                    pool.MarkHealthy(account);
                }

                entry["ok"] = true;
                entry["psidtsRefreshed"] = changed;
            }
            catch (GeminiAuthException ex)
            {
                pool.MarkAuthFailed(account);
                entry["ok"] = false;
                entry["error"] = ex.Message;
                entry["needsReharvest"] = true;
                logger?.LogWarning(
                    "Account {Account} needs a fresh cookie harvest: {Message}", account.Name, ex.Message);
            }
            catch (Exception ex)
            {
                pool.MarkFailed(account);
                entry["ok"] = false;
                entry["error"] = ex.Message;
            }

            // Cast to JsonNode so the AOT-safe overload is used; the generic one would
            // need runtime code generation for the JsonObject.
            results.Add((JsonNode?)entry);
        }

        return results;
    }

    /// <summary>Stops the background loop.</summary>
    public void Dispose()
    {
        _stop.Cancel();
        try
        {
            _loop?.Wait(TimeSpan.FromSeconds(5));
        }
        catch (AggregateException)
        {
            // The loop was cancelled — expected.
        }
        _stop.Dispose();
        _http.Dispose();
    }
}
