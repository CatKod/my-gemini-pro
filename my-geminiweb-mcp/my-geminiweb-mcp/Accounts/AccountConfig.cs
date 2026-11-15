using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Accounts;

/// <summary>
/// One Gemini account: its identity, its cookies, and its health state.
/// </summary>
internal sealed class AccountConfig
{
    /// <summary>Stable pool key, e.g. <c>account1</c>. Used to pin a request to one account.</summary>
    public string Name { get; set; } = "";

    /// <summary>The Google account email, when known. Purely informational.</summary>
    public string? Email { get; set; }

    /// <summary>The <c>__Secure-1PSID</c> cookie value.</summary>
    public string Psid { get; set; } = "";

    /// <summary>The <c>__Secure-1PSIDTS</c> cookie value.</summary>
    public string Psidts { get; set; } = "";

    /// <summary>
    /// Other cookies from the same session, keyed by name.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The full cookie set matters. A request that carries only <c>__Secure-1PSID</c> gets
    /// HTTP 200 from <c>gemini.google.com/app</c> but no session token at all — the failure
    /// is silent, so the extra cookies are what make a session work, not an optimisation.
    /// </para>
    /// <para>
    /// <c>__Secure-1PSIDTS</c> is the short-lived half of the pair: Google expires it within
    /// hours and only mints a replacement in the <c>Set-Cookie</c> of a regular response.
    /// Keeping the jar here is what allows that replacement to be persisted.
    /// </para>
    /// </remarks>
    public Dictionary<string, string> ExtraCookies { get; set; } = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>When the cookies were last refreshed, for diagnostics.</summary>
    public DateTimeOffset? LastRefreshed { get; set; }

    /// <summary>The full cookie set as a map, including the two authentication cookies.</summary>
    public Dictionary<string, string> AllCookies()
    {
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["__Secure-1PSID"] = Psid,
        };
        if (!string.IsNullOrEmpty(Psidts))
            map["__Secure-1PSIDTS"] = Psidts;

        foreach (var (name, value) in ExtraCookies)
        {
            if (!string.IsNullOrEmpty(value))
                map[name] = value;
        }
        return map;
    }

    /// <summary>Reads an account from a JSON node.</summary>
    public static AccountConfig? FromJson(JsonObject obj)
    {
        string name = (string?)obj["name"] ?? "";
        if (name.Length == 0)
            return null;

        var account = new AccountConfig
        {
            Name = name,
            Email = (string?)obj["email"],
            Psid = (string?)obj["psid"] ?? "",
            Psidts = (string?)obj["psidts"] ?? "",
        };

        if (obj["cookies"] is JsonObject cookies)
        {
            foreach (var (key, value) in cookies)
            {
                if (value is null)
                    continue;
                account.ExtraCookies[key] = value.GetValue<string>();
            }
        }

        if (obj["lastRefreshed"] is JsonValue refreshed &&
            refreshed.TryGetValue<DateTimeOffset>(out var when))
        {
            account.LastRefreshed = when;
        }

        return account;
    }

    /// <summary>Serializes the account back to JSON.</summary>
    public JsonObject ToJson()
    {
        var obj = new JsonObject
        {
            ["name"] = Name,
            ["email"] = Email,
            ["psid"] = Psid,
            ["psidts"] = Psidts,
        };

        if (ExtraCookies.Count > 0)
        {
            var cookies = new JsonObject();
            foreach (var (key, value) in ExtraCookies)
                cookies[key] = value;
            obj["cookies"] = cookies;
        }

        if (LastRefreshed is not null)
            obj["lastRefreshed"] = LastRefreshed.Value.ToString("O");

        return obj;
    }
}
