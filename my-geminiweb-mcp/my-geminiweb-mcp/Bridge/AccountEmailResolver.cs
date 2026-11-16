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
    /// Resolves the Google account email behind the given cookies, or <c>null</c>.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The URL matters. Google serves a different signed-in account per account slot:
    /// <c>https://gemini.google.com/app</c> is the primary account, while
    /// <c>https://gemini.google.com/u/1/app</c> is the second one. A request without the
    /// slot segment therefore always answers for the primary account, no matter which
    /// cookies are sent — so the caller has to pass the URL of the tab the cookies came
    /// from.
    /// </para>
    /// </remarks>
    /// <param name="cookies">The harvested cookie set.</param>
    /// <param name="url">The URL to query. Defaults to the primary app URL.</param>
    public async Task<string?> ResolveAsync(
        IReadOnlyDictionary<string, string> cookies,
        string? url = null)
    {
        string header = string.Join("; ", cookies.Select(c => $"{c.Key}={c.Value}"));
        if (header.Length == 0)
            return null;

        string target = NormalizeUrl(url);

        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, target);
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
    /// Reduces a tab URL to the URL that identifies its account slot.
    /// </summary>
    /// <remarks>
    /// Only the slot matters. <c>/u/1/app</c> and <c>/u/1/</c> identify the same account,
    /// so the path is rebuilt from the slot segment alone.
    /// </remarks>
    internal static string NormalizeUrl(string? url)
    {
        if (string.IsNullOrWhiteSpace(url))
            return AppUrl;

        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
            return AppUrl;

        if (!uri.Host.EndsWith("gemini.google.com", StringComparison.OrdinalIgnoreCase))
            return AppUrl;

        var segments = uri.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries);
        string? slot = segments.FirstOrDefault(s => s.Length > 1 && s[0] == 'u' && s[1..].All(char.IsDigit));
        if (slot is null)
            return AppUrl;

        return $"https://gemini.google.com/{slot}/app";
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
