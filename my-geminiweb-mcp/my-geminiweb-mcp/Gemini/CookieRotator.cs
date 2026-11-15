using System.Net;
using System.Net.Http.Headers;
using System.Text;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// Keeps a Gemini session alive by telling Google the session is still in use.
/// </summary>
/// <remarks>
/// <para>
/// Google expires an idle Google session within hours. The <c>RotateCookies</c> endpoint on
/// <c>accounts.google.com</c> is the mechanism that pushes that expiry back. It answers
/// HTTP 200 and hands out refreshed <c>__Secure-1PSIDCC</c> / <c>__Secure-3PSIDCC</c>
/// cookies.
/// </para>
/// <para>
/// Two things this endpoint does <em>not</em> do, both verified against the live site:
/// it does not require more than the two authentication cookies (a request with only
/// <c>__Secure-1PSID</c> and <c>__Secure-1PSIDTS</c> is answered with HTTP 200), and it
/// does not mint a fresh <c>__Secure-1PSIDTS</c>. The replacement for
/// <c>__Secure-1PSIDTS</c> arrives in the <c>Set-Cookie</c> header of ordinary responses to
/// <c>gemini.google.com/app</c>, which is why the keep-alive also performs that request and
/// persists whatever comes back.
/// </para>
/// <para>
/// The endpoint rate-limits hard (HTTP 429), so rotation is throttled to one attempt per
/// minute per account and never runs concurrently for the same account.
/// </para>
/// </remarks>
internal sealed class CookieRotator(HttpClient http, CookieJar jar)
{
    private const string RotateUrl = "https://accounts.google.com/RotateCookies";

    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Dictionary<string, DateTimeOffset> _lastAttempt = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>
    /// Attempts to extend the session lifetime.
    /// </summary>
    /// <param name="accountKey">The account key, used to throttle per account.</param>
    /// <param name="ct">Cancellation token.</param>
    /// <returns>
    /// The current <c>__Secure-1PSIDTS</c> value after the call — possibly unchanged — or
    /// <c>null</c> when the attempt was throttled or the endpoint failed.
    /// </returns>
    public async Task<string?> RotateAsync(string accountKey, CancellationToken ct = default)
    {
        // Throttle: RotateCookies answers 429 when called too often.
        lock (_lastAttempt)
        {
            if (_lastAttempt.TryGetValue(accountKey, out var last) &&
                DateTimeOffset.UtcNow - last < TimeSpan.FromMinutes(1))
            {
                return null;
            }
            _lastAttempt[accountKey] = DateTimeOffset.UtcNow;
        }

        await _gate.WaitAsync(ct);
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, RotateUrl);
            request.Content = new StringContent(
                "[000,\"-0000000000000000000\"]",
                Encoding.UTF8,
                new MediaTypeHeaderValue("application/json"));
            request.Headers.TryAddWithoutValidation("Origin", "https://accounts.google.com");
            request.Headers.TryAddWithoutValidation("Content-Type", "application/json");
            request.Headers.TryAddWithoutValidation("Cookie", jar.ToHeader());

            using var response = await http.SendAsync(request, ct);

            if (response.StatusCode == HttpStatusCode.Unauthorized)
                throw new GeminiAuthException(
                    "RotateCookies returned 401 — the session cookies are no longer valid.");

            if ((int)response.StatusCode == 429)
            {
                // Rate limited: back off and try again later.
                lock (_lastAttempt)
                    _lastAttempt[accountKey] = DateTimeOffset.UtcNow.AddMinutes(-1).AddSeconds(-30);
                return null;
            }

            if (!response.IsSuccessStatusCode)
                return null;

            jar.Absorb(response);
            return jar.Get("__Secure-1PSIDTS");
        }
        finally
        {
            _gate.Release();
        }
    }
}
