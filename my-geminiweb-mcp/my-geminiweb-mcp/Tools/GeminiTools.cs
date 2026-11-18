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
    /// <param name="model">
    /// Optional model name. The names are the ones this account is actually offered —
    /// call <c>gemini_models</c> to list them. Examples: <c>hydrogen</c>, <c>pro</c>,
    /// <c>3.5 flash-lite</c>, <c>carbon</c>. Pass <c>default</c> to let Google choose.
    /// The same model must be used for the whole conversation.
    /// </param>
    /// <param name="attachments">Optional local file paths to upload with this turn.</param>
    /// <param name="extendedThinking">When true, requests extended thinking.</param>
    [McpServerTool(Name = "gemini_chat")]
    [Description("Sends a message to Gemini and returns the assistant's reply. Pass back the conversation handle from a previous call to continue the same chat.")]
    public async Task<string> Chat(
        [Description("The message to send to Gemini")] string prompt,
        [Description("Optional account pool key to pin this request to one account")] string? account = null,
        [Description("Conversation handle from a previous call, to continue the same chat")] string? conversation = null,
        [Description("Optional model name, as listed by the gemini_models tool")] string? model = null,
        [Description("Optional local file paths to upload with this turn")] string[]? attachments = null,
        [Description("Request extended thinking")] bool extendedThinking = false)
    {
        if (string.IsNullOrWhiteSpace(prompt))
            throw new ArgumentException("prompt must not be empty.", nameof(prompt));

        // A conversation handle always names the account it belongs to: a conversation id
        // created on one account is meaningless on another, so continuing a chat pins the
        // account instead of letting the pool rotate. An explicitly pinned account is
        // also strict: falling back to a different account would report success for the
        // wrong account and hide the fact that the pinned one is dead.
        var handle = string.IsNullOrWhiteSpace(conversation) ? null : ParseConversation(conversation);

        AccountConfig? accountConfig;
        if (handle is not null && !string.IsNullOrEmpty(handle.Account))
        {
            accountConfig = pool.Get(handle.Account)
                ?? throw new InvalidOperationException(
                    $"Conversation belongs to account '{handle.Account}', which is no longer in the pool.");
        }
        else if (!string.IsNullOrWhiteSpace(account))
        {
            accountConfig = pool.Get(account)
                ?? throw new InvalidOperationException(
                    $"Account '{account}' is not in the pool. Available: " +
                    string.Join(", ", pool.Status().Select(s => s.Name)) + ".");
        }
        else
        {
            accountConfig = pool.Next()
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

            GeminiReply reply;
            try
            {
                reply = await client.GenerateAsync(
                    prompt,
                    handle?.Metadata,
                    model,
                    attachments,
                    extendedThinking,
                    paths.DownloadsFolder);
            }
            catch (ArgumentException ex)
            {
                // An unknown model name is a caller mistake, not a Gemini failure. It is
                // reported as data — with the models this account does offer — because the
                // MCP SDK replaces a thrown exception's message with a generic sentence.
                return new JsonObject
                {
                    ["account"] = accountConfig.Name,
                    ["error"] = ex.Message,
                    ["availableModels"] = new JsonArray(
                        client.Models.Models
                            .Select(m => (JsonNode?)JsonValue.Create(m.DisplayName))
                            .ToArray()),
                }.ToJsonString();
            }

            pool.MarkHealthy(accountConfig);
            PersistCookies(client, accountConfig);

            // JsonNode instead of anonymous-type serialization: reflection-based
            // serialization is unavailable under Native AOT.
            JsonArray? filesJson = null;
            if (reply.Files is { Count: > 0 })
            {
                filesJson = new JsonArray();
                foreach (var f in reply.Files)
                {
                    filesJson.Add((JsonNode?)new JsonObject
                    {
                        ["fileName"] = f.FileName,
                        ["savedPath"] = f.SavedPath,
                        ["size"] = f.Size,
                        ["mimeType"] = f.MimeType,
                        ["source"] = f.Source,
                    });
                }
            }

            var payload = new JsonObject
            {
                ["account"] = accountConfig.Name,
                ["reply"] = reply.Text,
                ["conversation"] = EncodeConversation(reply, accountConfig.Name),
                ["thoughts"] = string.IsNullOrEmpty(reply.Thoughts) ? null : reply.Thoughts,
            };

            // Which model actually answered. When the requested model's quota is
            // exhausted, Google silently serves a cheaper one — the caller has to know,
            // otherwise it reads a Flash-Lite answer as a Hydrogen one.
            if (!string.IsNullOrEmpty(reply.ServedModel))
                payload["servedModel"] = reply.ServedModel;

            // Whether that name was read from the reply payload or is the model that was
            // asked for. Google only echoes a model id on some replies, so without this
            // flag the caller cannot tell a confirmed name from a guess.
            payload["servedModelConfirmed"] = reply.ServedModelConfirmed;

            if (!string.IsNullOrEmpty(reply.QuotaNotice))
                payload["quotaNotice"] = reply.QuotaNotice;
            if (!string.IsNullOrEmpty(reply.FallbackReason))
                payload["fallbackReason"] = reply.FallbackReason;

            if (filesJson is not null)
                payload["files"] = filesJson;
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
    /// Lists the models an account is actually offered, discovered from Gemini itself.
    /// </summary>
    /// <remarks>
    /// The list is per account and per frontend build — Google renames, renumbers and
    /// re-tiers models without notice, so it is queried live instead of being hardcoded.
    /// The names listed here are exactly what <c>gemini_chat</c> accepts.
    /// </remarks>
    [McpServerTool(Name = "gemini_models")]
    [Description("Lists the Gemini models the configured account is offered, with the names you can pass to gemini_chat.")]
    public async Task<string> Models(string? account = null)
    {
        AccountConfig? accountConfig;
        if (!string.IsNullOrWhiteSpace(account))
        {
            accountConfig = pool.Get(account)
                ?? throw new InvalidOperationException(
                    $"Account '{account}' is not in the pool. Available: " +
                    string.Join(", ", pool.Status().Select(s => s.Name)) + ".");
        }
        else
        {
            accountConfig = pool.Next()
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

            var models = new JsonArray();
            foreach (var m in client.Models.Models)
            {
                models.Add((JsonNode?)new JsonObject
                {
                    ["name"] = m.ModelName,
                    ["displayName"] = m.DisplayName,
                    ["description"] = m.Description,
                    ["modelId"] = m.ModelId,
                    ["modelNumber"] = m.ModelNumber,
                    ["capacity"] = m.Capacity,
                    // Whether this account may pick the model at all. A model the account
                    // is shown but cannot select makes Gemini open a stream that never
                    // answers, so asking for it is refused up front.
                    ["selectable"] = m.IsSelectable,
                    // The name the current launch lineup uses, when it differs from the
                    // name in the model list (Google renamed Carbon to Argon on
                    // 2026-11-18 while the list still carried the old name).
                    ["currentName"] = m.CurrentName,
                    ["aliases"] = new JsonArray(
                        m.Aliases.Select(a => (JsonNode?)JsonValue.Create(a)).ToArray()),
                });
            }

            var unselectable = client.Models.UnselectableModels
                .Select(m => (JsonNode?)JsonValue.Create(m.Description))
                .ToArray();

            return new JsonObject
            {
                ["account"] = accountConfig.Name,
                ["count"] = models.Count,
                ["models"] = models,
                ["unselectable"] = new JsonArray(unselectable),
            }.ToJsonString();
        }
        catch (GeminiAuthException ex)
        {
            pool.MarkAuthFailed(accountConfig);
            throw new InvalidOperationException(
                $"The session for account '{accountConfig.Name}' is no longer valid ({ex.Message}).");
        }
        catch (GeminiApiException ex)
        {
            pool.MarkFailed(accountConfig);
            throw new InvalidOperationException(
                $"Could not list the models of account '{accountConfig.Name}': {ex.Message}");
        }
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
