using System.Net;
using System.Text.RegularExpressions;

namespace MyGeminiWebMcp.Bridge;

/// <summary>
/// Resolves the Google account email behind a harvested cookie set.
/// </summary>
/// <remarks>
/// <para>
/// The browser extension cannot always report the email: the Gemini page title does not
/// contain it, and the account switcher lives on <c>accounts.google.com</c>, which the
/// extension deliberately does not read. Without the email a harvest of a rotated session
/// cannot be matched against the pool, and the server would create a duplicate entry instead
/// of healing the existing one.
/// </para>
/// <para>
/// The server can answer the question itself: the Gemini bootstrap page embeds the signed-in
/// account email in its payload. One extra HTTPS request per harvest is a fair price for
/// keeping the pool keyed by account rather than by session id.
/// </para>
/// </remarks>
internal sealed partial class AccountEmailResolver(HttpClient? http = null)
{
    private const string AppUrl = "https://gemini.google.com/app";

    private readonly HttpClient _http = http ?? new HttpClient(new HttpClientHandler
    {
        UseCookies = false,
        AllowAutoRedirect = true,
        AutomaticDecompression =
            DecompressionMethods.GZip | DecompressionMethods.Deflate | DecompressionMethods.Brotli,
    })
    {
        Timeout = TimeSpan.FromSeconds(30),
    };

    /// <summary>
    /// Returns the email of the Google account behind the given cookies, or <c>null</c>.
    /// </summary>
    /// <param name="cookies">The harvested cookie set.</param>
    public async Task<string?> ResolveAsync(IReadOnlyDictionary<string, string> cookies)
    {
        string header = string.Join("; ", cookies.Select(c => $"{c.Key}={c.Value}"));
        if (header.Length == 0)
            return null;

        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, AppUrl);
            request.Headers.TryAddWithoutValidation(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36");
            request.Headers.TryAddWithoutValidation("Cookie", header);

            using var response = await _http.SendAsync(request);
            if (!response.IsSuccessStatusCode)
                return null;

            string body = await response.Content.ReadAsStringAsync();
            return ExtractEmail(body);
        }
        catch (HttpRequestException)
        {
            return null;
        }
        catch (TaskCanceledException)
        {
            return null;
        }
    }

    /// <summary>
    /// Pulls the signed-in account email out of the Gemini bootstrap page.
    /// </summary>
    /// <remarks>
    /// The page is a JavaScript payload, not markup: the email appears as a JSON string
    /// value. Only <c>gmail.com</c> addresses are accepted, so that a stray string in the
    /// page (a support address, a documentation link) cannot become an account key.
    /// </remarks>
    internal static string? ExtractEmail(string body)
    {
        foreach (Match match in EmailPattern().Matches(body))
        {
            string candidate = match.Value;
            if (candidate.EndsWith("@gmail.com", StringComparison.OrdinalIgnoreCase))
                return candidate.ToLowerInvariant();
        }

        return null;
    }

    [GeneratedRegex(@"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")]
    private static partial Regex EmailPattern();
}
