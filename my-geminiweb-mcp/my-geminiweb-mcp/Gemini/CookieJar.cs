using System.Net;
using System.Net.Http.Headers;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// A minimal browser-like cookie jar.
/// </summary>
/// <remarks>
/// <para>
/// The jar keeps the whole Google cookie set, not just the two authentication cookies.
/// This is not gold-plating: a request to <c>gemini.google.com/app</c> that carries only
/// <c>__Secure-1PSID</c> answers HTTP 200 with a page that contains no session token, so the
/// failure never surfaces as an error — it just looks like an empty model reply. The extra
/// cookies (<c>NID</c>, the <c>__Secure-3PSID</c> family) are what make the session work.
/// </para>
/// <para>
/// <c>__Secure-1PSIDTS</c> is the short-lived half of the authentication pair. Google mints a
/// replacement in the <c>Set-Cookie</c> header of ordinary responses, so the jar must absorb
/// it and the caller must persist it, or the session dies within hours.
/// </para>
/// </remarks>
internal sealed class CookieJar
{
    private readonly Dictionary<string, Cookie> _cookies = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>The number of cookies currently held.</summary>
    public int Count => _cookies.Count;

    /// <summary>Adds or replaces a cookie.</summary>
    public void Set(string name, string value, string domain = ".google.com", string path = "/")
    {
        _cookies[name] = new Cookie(name, value, domain, path);
    }

    /// <summary>Adds or replaces a cookie only when it is not already present.</summary>
    public void SetIfAbsent(string name, string value, string domain = ".google.com", string path = "/")
    {
        if (!_cookies.ContainsKey(name))
            Set(name, value, domain, path);
    }

    /// <summary>Returns a cookie value, or <c>null</c> when it is not present.</summary>
    public string? Get(string name) =>
        _cookies.TryGetValue(name, out var cookie) ? cookie.Value : null;

    /// <summary>All cookies, for inspection.</summary>
    public IReadOnlyList<Cookie> All => _cookies.Values.ToList();

    /// <summary>
    /// Builds the <c>Cookie</c> request header value.
    /// </summary>
    /// <param name="onlyAuth">
    /// When true, only the two authentication cookies are included. This is what the Gemini
    /// endpoints want; sending the whole jar would only add noise.
    /// </param>
    public string ToHeader(bool onlyAuth = false)
    {
        var selected = onlyAuth
            ? _cookies.Values.Where(c =>
                c.Name is "__Secure-1PSID" or "__Secure-1PSIDTS").ToList()
            : _cookies.Values.ToList();

        return string.Join("; ", selected.Select(c => $"{c.Name}={c.Value}"));
    }

    /// <summary>
    /// Absorbs the <c>Set-Cookie</c> headers of a response.
    /// </summary>
    /// <remarks>
    /// A response can carry several cookies with the same name but different domains. The
    /// Google-authenticated domain wins, because the copy served for another domain is often
    /// stale — using it produces an unauthenticated session even though the account is alive.
    /// </remarks>
    public void Absorb(HttpResponseMessage response)
    {
        if (!response.Headers.TryGetValues("Set-Cookie", out var values))
            return;

        foreach (var value in values)
        {
            if (!TryParse(value, out var cookie))
                continue;

            if (_cookies.TryGetValue(cookie.Name, out var existing) &&
                DomainRank(existing.Domain) > DomainRank(cookie.Domain))
            {
                continue;
            }

            _cookies[cookie.Name] = cookie;
        }
    }

    /// <summary>Parses one <c>Set-Cookie</c> header value.</summary>
    private static bool TryParse(string header, out Cookie cookie)
    {
        cookie = new Cookie("", "", "", "/");
        var parts = header.Split(';');
        var first = parts[0];
        int eq = first.IndexOf('=');
        if (eq <= 0)
            return false;

        string name = first[..eq].Trim();
        string value = first[(eq + 1)..].Trim();
        if (name.Length == 0)
            return false;

        string domain = ".google.com";
        string path = "/";
        foreach (var part in parts.Skip(1))
        {
            var trimmed = part.Trim();
            if (trimmed.StartsWith("Domain=", StringComparison.OrdinalIgnoreCase))
                domain = trimmed[7..].Trim();
            else if (trimmed.StartsWith("Path=", StringComparison.OrdinalIgnoreCase))
                path = trimmed[5..].Trim();
        }

        cookie = new Cookie(name, value, domain, path);
        return true;
    }

    /// <summary>
    /// Ranks a cookie domain: the Google-authenticated domain outranks the others.
    /// </summary>
    private static int DomainRank(string domain)
    {
        string normalized = domain.TrimStart('.').ToLowerInvariant();
        return normalized switch
        {
            "google.com" => 3,
            _ when normalized.EndsWith(".google.com") => 2,
            _ => 1,
        };
    }
}

/// <summary>A single cookie.</summary>
/// <param name="Name">The cookie name.</param>
/// <param name="Value">The cookie value.</param>
/// <param name="Domain">The cookie domain.</param>
/// <param name="Path">The cookie path.</param>
internal sealed record Cookie(string Name, string Value, string Domain, string Path);
