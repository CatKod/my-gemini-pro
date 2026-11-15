using System.ComponentModel;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Server;
using MyGeminiWebMcp.Accounts;
using MyGeminiWebMcp.Bridge;
using MyGeminiWebMcp.Gemini;

namespace MyGeminiWebMcp.Tools;

/// <summary>
/// MCP tools that expose the Gemini web chat to any MCP client.
/// </summary>
internal sealed class GeminiTools(AccountPool pool, AppPaths paths, CookieBridge bridge)
{
    /// <summary>
    /// Sends a message to Gemini and returns the reply.
    /// </summary>
    /// <param name="prompt">The message to send.</param>
    /// <param name="account">
    /// Optional pool key of the account to use, e.g. <c>account1</c>. When omitted the pool
    /// rotates automatically and fails over to a healthy account.
    /// </param>
    /// <param name="conversation">
    /// The conversation handle returned by a previous call, used to continue the same chat.
    /// Pass it back verbatim to keep the context.
    /// </param>
    [McpServerTool(Name = "gemini_chat")]
    [Description("Sends a message to Gemini and returns the assistant's reply. Pass back the conversation handle from a previous call to continue the same chat.")]
    public async Task<string> Chat(
        [Description("The message to send to Gemini")] string prompt,
        [Description("Optional account pool key to pin this request to one account")] string? account = null,
        [Description("Conversation handle from a previous call, to continue the same chat")] string? conversation = null)
    {
        if (string.IsNullOrWhiteSpace(prompt))
            throw new ArgumentException("prompt must not be empty.", nameof(prompt));

        // A conversation handle always names the account it belongs to: a conversation id
        // created on one account is meaningless on another, so continuing a chat pins the
        // account instead of letting the pool rotate.
        var handle = string.IsNullOrWhiteSpace(conversation) ? null : ParseConversation(conversation);

        AccountConfig? accountConfig;
        if (handle is not null && !string.IsNullOrEmpty(handle.Account))
        {
            accountConfig = pool.Get(handle.Account)
                ?? throw new InvalidOperationException(
                    $"Conversation belongs to account '{handle.Account}', which is no longer in the pool.");
        }
        else
        {
            accountConfig = pool.Next(account)
                ?? throw new InvalidOperationException(
                    "No usable Gemini account. Configure one with the 'gemini_accounts' tool or the " +
                    "pool file at " + paths.PoolFile + ".");
        }

        using var client = new GeminiWebClient(
            accountConfig.Psid,
            accountConfig.Psidts,
            extraCookies: accountConfig.ExtraCookies);
        try
        {
            await client.InitializeAsync();

            // The session cookie is short-lived. Refresh it before the request so a session
            // that is about to expire does not fail the call.
            await TryRotateAsync(client, accountConfig);

            var reply = await client.GenerateAsync(prompt, handle?.Metadata);

            pool.MarkHealthy(accountConfig);
            PersistCookies(client, accountConfig);

            // JsonNode instead of anonymous-type serialization: reflection-based
            // serialization is unavailable under Native AOT.
            var payload = new JsonObject
            {
                ["account"] = accountConfig.Name,
                ["reply"] = reply.Text,
                ["conversation"] = EncodeConversation(reply, accountConfig.Name),
                ["thoughts"] = string.IsNullOrEmpty(reply.Thoughts) ? null : reply.Thoughts,
            };
            return payload.ToJsonString();
        }
        catch (GeminiAuthException ex)
        {
            // The cookies are dead. Only a fresh browser harvest can revive this account.
            pool.MarkAuthFailed(accountConfig);
            throw new InvalidOperationException(
                $"The session for account '{accountConfig.Name}' is no longer valid ({ex.Message}). " +
                "Open gemini.google.com in a signed-in browser and run the 'geminiweb-mcp' browser " +
                "extension to send fresh cookies, then retry.");
        }
        catch (GeminiApiException ex)
        {
            pool.MarkFailed(accountConfig);
            throw new InvalidOperationException(
                $"Gemini request failed on account '{accountConfig.Name}': {ex.Message}");
        }
    }

    /// <summary>
    /// Refreshes the short-lived session cookie for every account in the pool.
    /// </summary>
    /// <returns>Per-account results, including whether the cookie actually changed.</returns>
    [McpServerTool(Name = "gemini_refresh")]
    [Description("Refreshes the expiring session cookie (__Secure-1PSIDTS) for the configured accounts so the sessions stay alive.")]
    public async Task<string> Refresh()
    {
        var results = new JsonArray();

        foreach (var status in pool.Status())
        {
            var account = pool.Get(status.Name);
            if (account is null)
                continue;

            string before = account.Psidts;
            try
            {
                using var client = new GeminiWebClient(
                    account.Psid,
                    account.Psidts,
                    extraCookies: account.ExtraCookies);

                // The bootstrap request is where a fresh __Secure-1PSIDTS comes from.
                await client.InitializeAsync(fullJar: true);

                // Then push the server-side session expiry back.
                string? refreshed = await client.RotateCookiesAsync(account.Name);

                string? current = client.Cookies.Get("__Secure-1PSIDTS");
                bool changed = !string.IsNullOrEmpty(current) && current != before;

                if (changed)
                    PersistCookies(client, account);

                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = account.Name,
                    ["refreshed"] = changed,
                    ["rotated"] = refreshed is not null,
                    ["reason"] = changed
                        ? "a new __Secure-1PSIDTS was issued and saved"
                        : "the cookie was already current",
                });
            }
            catch (GeminiAuthException ex)
            {
                pool.MarkAuthFailed(account);
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = account.Name,
                    ["refreshed"] = false,
                    ["error"] = ex.Message,
                    ["needsReharvest"] = true,
                });
            }
            catch (Exception ex)
            {
                pool.MarkFailed(account);
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = account.Name,
                    ["refreshed"] = false,
                    ["error"] = ex.Message,
                });
            }
        }

        return new JsonObject { ["results"] = results }.ToJsonString();
    }

    /// <summary>
    /// Reports how the browser extension should connect to this server.
    /// </summary>
    /// <remarks>
    /// The endpoint and the token are returned so a user (or an agent driving the browser)
    /// can configure the extension without digging through the server's stderr output.
    /// </remarks>
    [McpServerTool(Name = "gemini_bridge")]
    [Description("Returns the loopback endpoint and token the browser extension needs in order to send session cookies to this server.")]
    public string BridgeInfo()
    {
        return new JsonObject
        {
            ["running"] = bridge.Running,
            ["endpoint"] = bridge.Endpoint,
            ["token"] = bridge.Token,
            ["tokenFile"] = paths.BridgeTokenFile,
            ["poolFile"] = paths.PoolFile,
            ["hint"] =
                "Install the 'geminiweb-mcp' extension (chrome://extensions -> Developer mode -> " +
                "Load unpacked), open its popup, paste this endpoint and token, then press " +
                "'Send cookies' while signed in to gemini.google.com. The token is persisted, so " +
                "you only paste it once.",
        }.ToJsonString();
    }

    /// <summary>
    /// Lists the accounts in the pool and their health.
    /// </summary>
    [McpServerTool(Name = "gemini_accounts")]
    [Description("Lists the configured Gemini accounts and their current health status.")]
    public string Accounts()
    {
        var statuses = pool.Status();
        var accounts = new JsonArray();
        foreach (var s in statuses)
        {
            accounts.Add((JsonNode?)new JsonObject
            {
                ["name"] = s.Name,
                ["email"] = s.Email,
                ["healthy"] = s.Healthy,
                ["lastFailure"] = s.LastFailure?.ToString("O"),
                ["needsReharvest"] = s.NeedsReharvest,
                ["lastRefreshed"] = s.LastRefreshed?.ToString("O"),
            });
        }

        return new JsonObject
        {
            ["poolFile"] = paths.PoolFile,
            ["count"] = statuses.Count,
            ["accounts"] = accounts,
        }.ToJsonString();
    }

    /// <summary>
    /// Verifies that the configured accounts can actually reach Gemini.
    /// </summary>
    [McpServerTool(Name = "gemini_check")]
    [Description("Checks that the Gemini accounts are reachable and reports the account status returned by Google.")]
    public async Task<string> Check()
    {
        var results = new JsonArray();
        foreach (var status in pool.Status())
        {
            var account = pool.Get(status.Name);
            if (account is null)
            {
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = status.Name,
                    ["ok"] = false,
                    ["error"] = "not configured",
                });
                continue;
            }

            try
            {
                using var client = new GeminiWebClient(
                    account.Psid,
                    account.Psidts,
                    extraCookies: account.ExtraCookies);
                var session = await client.InitializeAsync();
                pool.MarkHealthy(account);
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = status.Name,
                    ["ok"] = true,
                    ["language"] = session.Language,
                    ["buildLabel"] = session.BuildLabel,
                });
            }
            catch (GeminiAuthException ex)
            {
                pool.MarkAuthFailed(account);
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = status.Name,
                    ["ok"] = false,
                    ["error"] = ex.Message,
                    ["needsReharvest"] = true,
                });
            }
            catch (Exception ex)
            {
                pool.MarkFailed(account);
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = status.Name,
                    ["ok"] = false,
                    ["error"] = ex.Message,
                });
            }
        }

        return new JsonObject { ["results"] = results }.ToJsonString();
    }

    /// <summary>
    /// Refreshes the session cookie, ignoring failures: a rotation problem must not break a
    /// chat that would otherwise work.
    /// </summary>
    private async Task TryRotateAsync(GeminiWebClient client, AccountConfig account)
    {
        try
        {
            await client.RotateCookiesAsync(account.Name);
        }
        catch (GeminiAuthException)
        {
            // The session is genuinely dead. Let the request itself report the failure.
        }
        catch (Exception)
        {
            // Rotation is best-effort.
        }
    }

    /// <summary>Writes the cookies the server handed back into the pool file.</summary>
    private void PersistCookies(GeminiWebClient client, AccountConfig account)
    {
        string? psidts = client.Cookies.Get("__Secure-1PSIDTS");
        if (psidts is null || psidts == account.Psidts)
            return;

        var extras = client.Cookies.All
            .Where(c => c.Name is not ("__Secure-1PSID" or "__Secure-1PSIDTS"))
            .Select(c => new KeyValuePair<string, string>(c.Name, c.Value))
            .ToList();

        pool.UpdateCookies(account, psidts, extras);
    }

    /// <summary>
    /// Encodes the conversation handle: the Gemini ids plus the account that owns them.
    /// </summary>
    private static string EncodeConversation(GeminiReply reply, string account) =>
        new JsonObject
        {
            ["account"] = account,
            ["cid"] = reply.ConversationId,
            ["rid"] = reply.ReplyId,
        }.ToJsonString();

    /// <summary>
    /// Parses a conversation handle produced by <see cref="EncodeConversation"/>.
    /// Legacy handles that were plain id arrays are still accepted.
    /// </summary>
    private static ConversationHandle? ParseConversation(string conversation)
    {
        try
        {
            if (JsonNode.Parse(conversation) is JsonObject obj)
            {
                return new ConversationHandle(
                    (string?)obj["account"] ?? "",
                    [(string?)obj["cid"] ?? "", (string?)obj["rid"] ?? "", ""]);
            }

            if (JsonNode.Parse(conversation) is JsonArray array)
            {
                var ids = array.Select(n => n?.GetValue<string>() ?? string.Empty).ToList();
                return new ConversationHandle("", ids);
            }
        }
        catch (JsonException)
        {
            // Fall through: accept a plain "cid,rid" pair as well.
        }

        var parts = conversation.Split(
            ',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        return new ConversationHandle("", parts);
    }
}

/// <summary>A parsed conversation handle: the owning account plus the Gemini ids.</summary>
/// <param name="Account">The pool key of the account that owns the conversation.</param>
/// <param name="Metadata">The <c>[cid, rid, rcid]</c> triple.</param>
internal sealed record ConversationHandle(string Account, IReadOnlyList<string> Metadata);
