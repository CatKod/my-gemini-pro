using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Accounts;

/// <summary>
/// Round-robin account pool with automatic failover and cookie refresh.
/// </summary>
/// <remarks>
/// Health is tracked per account so a dead session is skipped rather than retried forever.
/// The pool is the only component that writes credentials back to disk, and it does so
/// atomically so an interrupted write cannot corrupt the file.
/// </remarks>
internal sealed class AccountPool
{
    /// <summary>How long an account stays skipped after a failure.</summary>
    public static readonly TimeSpan FailureCooldown = TimeSpan.FromMinutes(10);

    /// <summary>How long an account stays skipped after an authentication failure.</summary>
    /// <remarks>
    /// An authentication failure is different from a transient network error: the session
    /// cookies are dead and only a fresh browser harvest can revive them. It therefore gets
    /// a longer cooldown than a generic failure.
    /// </remarks>
    public static readonly TimeSpan AuthFailureCooldown = TimeSpan.FromMinutes(60);

    private readonly string _filePath;
    private readonly List<AccountConfig> _accounts;
    private readonly Dictionary<string, DateTimeOffset> _lastFailure = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, DateTimeOffset> _lastAuthFailure = new(StringComparer.OrdinalIgnoreCase);
    private readonly Lock _gate = new();
    private int _cursor;

    private AccountPool(string filePath, List<AccountConfig> accounts)
    {
        _filePath = filePath;
        _accounts = accounts;
    }

    /// <summary>The number of accounts in the pool.</summary>
    public int Count => _accounts.Count;

    /// <summary>The path of the pool file.</summary>
    public string FilePath => _filePath;

    /// <summary>Loads the pool from a JSON file. Returns an empty pool when the file is missing.</summary>
    public static AccountPool Load(string path)
    {
        if (!File.Exists(path))
            return new AccountPool(path, []);

        // AOT-safe: read through JsonNode instead of reflection-based deserialization.
        var node = JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
        var accounts = new List<AccountConfig>();
        if (node?["accounts"] is JsonArray list)
        {
            foreach (var item in list.OfType<JsonObject>())
            {
                var account = AccountConfig.FromJson(item);
                if (account is not null)
                    accounts.Add(account);
            }
        }

        return new AccountPool(path, accounts);
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
            return _accounts
                .OrderBy(a => _lastFailure.TryGetValue(a.Name, out var t) ? t : DateTimeOffset.MinValue)
                .First();
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
        {
            _lastFailure[account.Name] = DateTimeOffset.UtcNow;
            Save();
        }
    }

    /// <summary>
    /// Marks an account as authentication-dead: its cookies no longer work and only a fresh
    /// browser harvest can revive it.
    /// </summary>
    public void MarkAuthFailed(AccountConfig account)
    {
        lock (_gate)
        {
            _lastFailure[account.Name] = DateTimeOffset.UtcNow;
            _lastAuthFailure[account.Name] = DateTimeOffset.UtcNow;
            Save();
        }
    }

    /// <summary>Clears the failure marker for an account.</summary>
    public void MarkHealthy(AccountConfig account)
    {
        lock (_gate)
        {
            _lastFailure.Remove(account.Name);
            _lastAuthFailure.Remove(account.Name);
        }
    }

    /// <summary>
    /// Stores refreshed cookie values for an account and writes the pool file back to disk.
    /// </summary>
    /// <remarks>
    /// Writing is atomic: the new content goes to a temporary file that is then moved over
    /// the original, so a crash mid-write cannot leave a truncated pool file behind.
    /// </remarks>
    public void UpdateCookies(AccountConfig account, string psidts, IEnumerable<KeyValuePair<string, string>> extras)
    {
        lock (_gate)
        {
            if (!string.IsNullOrEmpty(psidts))
                account.Psidts = psidts;

            foreach (var (name, value) in extras)
            {
                if (!string.IsNullOrEmpty(value))
                    account.ExtraCookies[name] = value;
            }

            account.LastRefreshed = DateTimeOffset.UtcNow;
            Save();
        }
    }

    /// <summary>
    /// Adds a new account or replaces the cookies of an existing one.
    /// </summary>
    /// <remarks>
    /// This is the entry point for the browser-extension bridge: a harvest identifies the
    /// account by name (or by its <c>__Secure-1PSID</c> value) and lands here.
    /// </remarks>
    /// <param name="name">The pool key for the account.</param>
    /// <param name="email">The Google account email, when known.</param>
    /// <param name="cookies">The complete cookie set harvested from the browser.</param>
    /// <param name="matchedBy">How the account was identified, for diagnostics.</param>
    /// <returns>The stored account.</returns>
    public AccountConfig Upsert(
        string name,
        string? email,
        IReadOnlyDictionary<string, string> cookies,
        string matchedBy = "extension")
    {
        lock (_gate)
        {
            var account = _accounts.FirstOrDefault(
                a => string.Equals(a.Name, name, StringComparison.OrdinalIgnoreCase));

            if (account is null)
            {
                account = new AccountConfig { Name = name };
                _accounts.Add(account);
            }

            if (!string.IsNullOrWhiteSpace(email))
                account.Email = email;

            if (cookies.TryGetValue("__Secure-1PSID", out var psid) && !string.IsNullOrEmpty(psid))
                account.Psid = psid;
            if (cookies.TryGetValue("__Secure-1PSIDTS", out var psidts) && !string.IsNullOrEmpty(psidts))
                account.Psidts = psidts;

            foreach (var (key, value) in cookies)
            {
                if (string.IsNullOrEmpty(value))
                    continue;
                if (key is "__Secure-1PSID" or "__Secure-1PSIDTS")
                    continue;
                account.ExtraCookies[key] = value;
            }

            account.LastRefreshed = DateTimeOffset.UtcNow;

            // A fresh harvest revives an account that had died on authentication.
            MarkHealthyCore(account);

            Save();
            _ = matchedBy;
            return account;
        }
    }

    /// <summary>
    /// Finds the pool account that owns a given <c>__Secure-1PSID</c> value.
    /// </summary>
    /// <returns>The matching account name, or <c>null</c>.</returns>
    public string? FindByPsid(string psid)
    {
        if (string.IsNullOrEmpty(psid))
            return null;

        lock (_gate)
        {
            var match = _accounts.FirstOrDefault(
                a => string.Equals(a.Psid, psid, StringComparison.Ordinal));
            return match?.Name;
        }
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
                _lastFailure.TryGetValue(a.Name, out var t) ? t : null,
                _lastAuthFailure.ContainsKey(a.Name),
                a.LastRefreshed)).ToList();
        }
    }

    private void Save()
    {
        var accounts = new JsonArray();
        foreach (var account in _accounts)
            accounts.Add((JsonNode?)account.ToJson());

        var root = new JsonObject { ["accounts"] = accounts };
        string json = root.ToJsonString(new System.Text.Json.JsonSerializerOptions { WriteIndented = true });

        string directory = Path.GetDirectoryName(_filePath) ?? string.Empty;
        if (directory.Length > 0)
            Directory.CreateDirectory(directory);

        string temp = _filePath + ".tmp";
        File.WriteAllText(temp, json);
        File.Move(temp, _filePath, overwrite: true);
    }

    private void MarkHealthyCore(AccountConfig account)
    {
        _lastFailure.Remove(account.Name);
        _lastAuthFailure.Remove(account.Name);
    }

    private bool IsHealthy(AccountConfig account)
    {
        if (string.IsNullOrEmpty(account.Psid))
            return false;

        if (_lastAuthFailure.TryGetValue(account.Name, out var authFailed))
        {
            if (DateTimeOffset.UtcNow - authFailed < AuthFailureCooldown)
                return false;
        }

        if (_lastFailure.TryGetValue(account.Name, out var failed))
        {
            if (DateTimeOffset.UtcNow - failed < FailureCooldown)
                return false;
        }

        return true;
    }
}

/// <summary>Health snapshot of a single account.</summary>
/// <param name="Name">The pool key.</param>
/// <param name="Email">The Google account email, when known.</param>
/// <param name="Healthy">Whether the pool will route requests to this account.</param>
/// <param name="LastFailure">When the account last failed, if ever.</param>
/// <param name="NeedsReharvest">Whether the last failure was an authentication failure.</param>
/// <param name="LastRefreshed">When the account's cookies were last refreshed, if ever.</param>
internal sealed record AccountStatus(
    string Name,
    string? Email,
    bool Healthy,
    DateTimeOffset? LastFailure,
    bool NeedsReharvest,
    DateTimeOffset? LastRefreshed);
