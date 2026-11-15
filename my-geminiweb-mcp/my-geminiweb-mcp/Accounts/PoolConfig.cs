using System.Text.Json;
using System.Text.Json.Serialization;

namespace MyGeminiWebMcp.Accounts;

/// <summary>
/// One Gemini account: its identity, its cookies, and its health state.
/// </summary>
internal sealed class AccountConfig
{
    /// <summary>Stable pool key, e.g. <c>account1</c>. Used to pin a request to one account.</summary>
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    /// <summary>The Google account email, when known. Purely informational.</summary>
    [JsonPropertyName("email")]
    public string? Email { get; set; }

    /// <summary>The <c>__Secure-1PSID</c> cookie value.</summary>
    [JsonPropertyName("psid")]
    public string Psid { get; set; } = "";

    /// <summary>The <c>__Secure-1PSIDTS</c> cookie value.</summary>
    [JsonPropertyName("psidts")]
    public string Psidts { get; set; } = "";
}

/// <summary>The on-disk account pool configuration.</summary>
internal sealed class PoolConfig
{
    /// <summary>The configured accounts.</summary>
    [JsonPropertyName("accounts")]
    public List<AccountConfig> Accounts { get; set; } = [];
}
