using System.ComponentModel;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Server;
using MyGeminiWebMcp.Accounts;
using MyGeminiWebMcp.Gemini;

namespace MyGeminiWebMcp.Tools;

/// <summary>
/// MCP tools that expose the Gemini web chat to any MCP client.
/// </summary>
internal sealed class GeminiTools(AccountPool pool, AppPaths paths)
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

        using var client = new GeminiWebClient(accountConfig.Psid, accountConfig.Psidts);
        try
        {
            await client.InitializeAsync();
            var reply = await client.GenerateAsync(prompt, handle?.Metadata);

            pool.MarkHealthy(accountConfig);

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
        catch (Exception ex) when (ex is GeminiAuthException or GeminiApiException)
        {
            pool.MarkFailed(accountConfig);
            throw new InvalidOperationException(
                $"Gemini request failed on account '{accountConfig.Name}': {ex.Message}");
        }
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
            var account = pool.Next(status.Name);
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
                using var client = new GeminiWebClient(account.Psid, account.Psidts);
                var session = await client.InitializeAsync();
                pool.MarkHealthy(account);
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = account.Name,
                    ["ok"] = true,
                    ["language"] = session.Language,
                    ["buildLabel"] = session.BuildLabel,
                });
            }
            catch (Exception ex)
            {
                pool.MarkFailed(account);
                results.Add((JsonNode?)new JsonObject
                {
                    ["account"] = account.Name,
                    ["ok"] = false,
                    ["error"] = ex.Message,
                });
            }
        }

        return new JsonObject { ["results"] = results }.ToJsonString();
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




