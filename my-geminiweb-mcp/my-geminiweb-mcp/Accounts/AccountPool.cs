using System.Text.Json;
using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Accounts;

/// <summary>
/// Round-robin account pool with automatic failover.
/// </summary>
/// <remarks>
/// Health is tracked per account so a dead session is skipped rather than retried forever.
/// The pool never writes credentials back to disk: it only reads the configured file.
/// </remarks>
internal sealed class AccountPool
{
    private readonly List<AccountConfig> _accounts;
    private readonly Dictionary<string, DateTimeOffset> _lastFailure = new(StringComparer.OrdinalIgnoreCase);
    private readonly Lock _gate = new();
    private int _cursor;

    private AccountPool(List<AccountConfig> accounts) => _accounts = accounts;

    /// <summary>The number of accounts in the pool.</summary>
    public int Count => _accounts.Count;

    /// <summary>Loads the pool from a JSON file. Returns an empty pool when the file is missing.</summary>
    public static AccountPool Load(string path)
    {
        if (!File.Exists(path))
            return new AccountPool([]);

        string json = File.ReadAllText(path);
        // AOT-safe: read through JsonNode instead of reflection-based deserialization.
        var node = JsonNode.Parse(json) as JsonObject;
        var accounts = new List<AccountConfig>();
        if (node?["accounts"] is JsonArray list)
        {
            foreach (var item in list.OfType<JsonObject>())
            {
                var account = new AccountConfig
                {
                    Name = (string?)item["name"] ?? "",
                    Email = (string?)item["email"],
                    Psid = (string?)item["psid"] ?? "",
                    Psidts = (string?)item["psidts"] ?? "",
                };
                if (account.Name.Length > 0)
                    accounts.Add(account);
            }
        }

        return new AccountPool(accounts);
    }

    /// <summary>
    /// Picks the next account to use, skipping ones that failed recently.
    /// </summary>
    /// <param name="preferred">
    /// A specific account name to use. When it is unhealthy the pool still falls back to
    /// another account, because failing over beats failing.
    /// </param>
    public AccountConfig? Next(string? preferred = null)
    {
        lock (_gate)
        {
            if (_accounts.Count == 0)
                return null;

            if (!string.IsNullOrWhiteSpace(preferred))
            {
                var match = _accounts.FirstOrDefault(
                    a => string.Equals(a.Name, preferred, StringComparison.OrdinalIgnoreCase));
                if (match is not null && IsHealthy(match))
                    return match;

                // Preferred account exists but is unhealthy -> fall through to rotation.
            }

            for (int i = 0; i < _accounts.Count; i++)
            {
                int index = (_cursor + i) % _accounts.Count;
                var account = _accounts[index];
                if (!IsHealthy(account))
                    continue;
                _cursor = (index + 1) % _accounts.Count;
                return account;
            }

            // Everything is unhealthy: retry the least-recently-failed account.
            var fallback = _accounts
                .OrderBy(a => _lastFailure.TryGetValue(a.Name, out var t) ? t : DateTimeOffset.MinValue)
                .First();
            return fallback;
        }
    }

    /// <summary>Returns a specific account by name, regardless of its health.</summary>
    public AccountConfig? Get(string name)
    {
        lock (_gate)
        {
            return _accounts.FirstOrDefault(
                a => string.Equals(a.Name, name, StringComparison.OrdinalIgnoreCase));
        }
    }

    /// <summary>Marks an account as failed, so the pool stops routing to it for a cool-down.</summary>
    public void MarkFailed(AccountConfig account)
    {
        lock (_gate)
            _lastFailure[account.Name] = DateTimeOffset.UtcNow;
    }

    /// <summary>Clears the failure marker for an account.</summary>
    public void MarkHealthy(AccountConfig account)
    {
        lock (_gate)
            _lastFailure.Remove(account.Name);
    }

    /// <summary>Returns the health snapshot of every account, for diagnostics.</summary>
    public IReadOnlyList<AccountStatus> Status()
    {
        lock (_gate)
        {
            return _accounts.Select(a => new AccountStatus(
                a.Name,
                a.Email,
                IsHealthy(a),
                _lastFailure.TryGetValue(a.Name, out var t) ? t : null)).ToList();
        }
    }

    private bool IsHealthy(AccountConfig account)
    {
        if (string.IsNullOrEmpty(account.Psid))
            return false;
        return !_lastFailure.TryGetValue(account.Name, out var failed) ||
               DateTimeOffset.UtcNow - failed > TimeSpan.FromMinutes(10);
    }
}

/// <summary>Health snapshot of a single account.</summary>
/// <param name="Name">The pool key.</param>
/// <param name="Email">The Google account email, when known.</param>
/// <param name="Healthy">Whether the pool will route requests to this account.</param>
/// <param name="LastFailure">When the account last failed, if ever.</param>
internal sealed record AccountStatus(
    string Name,
    string? Email,
    bool Healthy,
    DateTimeOffset? LastFailure);

/// <summary>Shared JSON options for the pool files.</summary>
internal static class PoolJson
{
    /// <summary>Lenient, case-insensitive JSON options.</summary>
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
        WriteIndented = true,
    };
}
