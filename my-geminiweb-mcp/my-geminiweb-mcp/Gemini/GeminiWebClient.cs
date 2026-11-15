using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// Minimal, dependency-free client for the Gemini web frontend (gemini.google.com).
/// </summary>
/// <remarks>
/// <para>
/// This speaks the same private protocol as the Python reference implementation
/// (<c>gemini_webapi</c>), but on top of the plain <see cref="HttpClient"/>. That matters for a
/// NuGet package: no Python runtime, no TLS-fingerprint impersonation library, and a
/// single-file Native AOT binary.
/// </para>
/// <para>
/// Verified against the live site: <c>SNlM0e</c> bootstrap tokens are extracted from
/// <c>GET /app</c>, and <c>StreamGenerate</c> accepts the 81-slot JSPB payload with the
/// length-prefixed stream frames decoded by <see cref="StreamingFrameParser"/>.
/// </para>
/// </remarks>
internal sealed class GeminiWebClient : IDisposable
{
    private const string InitUrl = "https://gemini.google.com/app";
    private const string GenerateUrl =
        "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate";
    private const string ModelHeaderKey = "x-goog-ext-525001261-jspb";
    private const string RequestIdHeaderKey = "x-goog-ext-525005358-jspb";

    /// <summary>
    /// The 10-slot metadata template Gemini expects. Slots 0/1/2 carry
    /// <c>cid</c>/<c>rid</c>/<c>rcid</c>; the remaining slots stay empty for a plain chat turn.
    /// </summary>
    private static readonly string?[] DefaultMetadata =
        ["", "", "", null, null, null, null, null, null, ""];

    /// <summary>
    /// Builds the 10-slot metadata array Gemini expects. Slots 0/1/2 carry
    /// <c>cid</c>/<c>rid</c>/<c>rcid</c>; the remaining slots stay empty for a plain chat turn.
    /// </summary>
    private static JsonArray BuildMetadata(IReadOnlyList<string>? chatMetadata)
    {
        var array = new JsonArray();
        for (int i = 0; i < 10; i++)
            array.Add((JsonNode?)DefaultMetadata[i]);

        if (chatMetadata is null)
            return array;

        for (int i = 0; i < Math.Min(3, chatMetadata.Count); i++)
        {
            if (!string.IsNullOrEmpty(chatMetadata[i]))
                array[i] = chatMetadata[i];
        }

        return array;
    }

    private readonly HttpClient _http;
    private readonly string _cookieHeader;
    private readonly string _sessionId;
    private int _reqId = Random.Shared.Next(10_000, 99_999);

    public GeminiWebClient(string psid, string psidts, HttpClient? http = null)
    {
        _cookieHeader = $"__Secure-1PSID={psid}; __Secure-1PSIDTS={psidts}";
        _sessionId = Guid.NewGuid().ToString().ToUpperInvariant();
        _http = http ?? new HttpClient(new HttpClientHandler
        {
            UseCookies = false,
            AllowAutoRedirect = true,
            AutomaticDecompression =
                DecompressionMethods.GZip | DecompressionMethods.Deflate | DecompressionMethods.Brotli,
        })
        {
            Timeout = TimeSpan.FromMinutes(5),
        };
    }

    /// <summary>The session bootstrap values scraped from the app page.</summary>
    public GeminiSession? Session { get; private set; }

    /// <summary>Reads the bootstrap page and extracts the session tokens.</summary>
    public async Task<GeminiSession> InitializeAsync(CancellationToken ct = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, InitUrl);
        ApplyCommonHeaders(request);

        using var response = await _http.SendAsync(request, ct);
        response.EnsureSuccessStatusCode();
        string body = await response.Content.ReadAsStringAsync(ct);

        string token = Extract(body, "SNlM0e");
        if (token.Length == 0)
            throw new GeminiAuthException(
                "Gemini returned no access token. The session cookies are probably expired or invalid.");

        Session = new GeminiSession(
            token,
            Extract(body, "cfb2h"),
            Extract(body, "FdrFJe"),
            Extract(body, "TuX5cc") is { Length: > 0 } lang ? lang : "en",
            Extract(body, "qKIAYe") is { Length: > 0 } push ? push : "feeds/mcudyrk2a4khkz");
        return Session;
    }

    /// <summary>
    /// Sends a chat turn and returns the assistant reply plus the conversation handles
    /// needed to continue the same conversation later.
    /// </summary>
    /// <param name="prompt">The user message.</param>
    /// <param name="chatMetadata">
    /// The <c>[cid, rid, rcid]</c> triple returned by a previous turn. Pass it to continue
    /// the same conversation; pass <c>null</c> to start a new one.
    /// </param>
    /// <param name="model">
    /// Optional model selector, expressed as the raw JSPB model header value. When omitted,
    /// Google picks the account's default model.
    /// </param>
    /// <param name="ct">Cancellation token.</param>
    public async Task<GeminiReply> GenerateAsync(
        string prompt,
        IReadOnlyList<string>? chatMetadata = null,
        string? model = null,
        CancellationToken ct = default)
    {
        if (Session is null)
            await InitializeAsync(ct);

        var session = Session!;
        string requestUuid = Guid.NewGuid().ToString().ToUpperInvariant();

        var inner = new JsonArray();
        for (int i = 0; i < 81; i++)
            inner.Add((JsonNode?)null);

        inner[0] = new JsonArray(prompt, 0, null, null, null, null, 0);
        inner[1] = new JsonArray(session.Language);
        inner[2] = BuildMetadata(chatMetadata);
        inner[6] = new JsonArray(1);
        inner[7] = 1;
        inner[10] = 1;
        inner[11] = 0;
        inner[17] = new JsonArray(new JsonArray(0));
        inner[18] = 0;
        inner[27] = 1;
        inner[30] = new JsonArray(4);
        inner[41] = new JsonArray(1);
        inner[53] = 0;
        inner[59] = requestUuid;
        inner[61] = new JsonArray();
        inner[68] = 1;
        inner[79] = 1;
        inner[80] = 1;

        var headers = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["at"] = session.AccessToken,
            // f.req is a JSON array whose second element is the JSPB payload encoded as a
            // JSON string. Built with JsonNode so it works under Native AOT.
            ["f.req"] = new JsonArray(null, inner.ToJsonString()).ToJsonString(),
        };

        var query = new List<string>
        {
            "hl=" + Uri.EscapeDataString(session.Language),
            "_reqid=" + _reqId,
            "rt=c",
        };
        _reqId += 100_000;

        if (session.BuildLabel.Length > 0)
            query.Add("bl=" + Uri.EscapeDataString(session.BuildLabel));
        if (session.SessionId.Length > 0)
            query.Add("f.sid=" + Uri.EscapeDataString(session.SessionId));

        using var request = new HttpRequestMessage(
            HttpMethod.Post, $"{GenerateUrl}?{string.Join('&', query)}");
        ApplyCommonHeaders(request);
        request.Headers.TryAddWithoutValidation(RequestIdHeaderKey, $"[\"{requestUuid}\",1]");
        request.Headers.TryAddWithoutValidation("X-Same-Domain", "1");

        if (model is not null)
        {
            var modelHeader = JsonNode.Parse(model)!.AsArray();
            modelHeader.Add((JsonNode?)JsonValue.Create(1));
            modelHeader.Add((JsonNode?)JsonValue.Create(_sessionId));
            request.Headers.TryAddWithoutValidation(ModelHeaderKey, modelHeader.ToJsonString());
        }

        request.Content = new FormUrlEncodedContent(headers);
        request.Content.Headers.Remove("Content-Type");
        request.Content.Headers.TryAddWithoutValidation(
            "Content-Type", "application/x-www-form-urlencoded;charset=utf-8");

        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
        if (!response.IsSuccessStatusCode)
        {
            string err = await response.Content.ReadAsStringAsync(ct);
            throw new GeminiApiException(
                $"StreamGenerate failed with HTTP {(int)response.StatusCode}: {Truncate(err, 300)}");
        }

        return await ReadReplyAsync(response, ct);
    }

    private static async Task<GeminiReply> ReadReplyAsync(HttpResponseMessage response, CancellationToken ct)
    {
        var parser = new StreamingFrameParser();
        var decoder = new UTF8Encoding(false);
        var buffer = new byte[8192];

        string text = "";
        string thoughts = "";
        string cid = "";
        string rid = "";

        await using (var stream = await response.Content.ReadAsStreamAsync(ct))
        {
            int read;
            while ((read = await stream.ReadAsync(buffer, ct)) > 0)
            {
                foreach (var frame in parser.Feed(decoder.GetString(buffer, 0, read)))
                {
                    if (frame is not JsonArray envelope || envelope.Count < 3)
                        continue;
                    if (envelope[2] is not JsonValue value || !value.TryGetValue<string>(out var raw))
                        continue;

                    JsonNode? part;
                    try
                    {
                        part = JsonNode.Parse(raw);
                    }
                    catch (JsonException)
                    {
                        continue;
                    }

                    if (part is not JsonArray p || p.Count < 5)
                        continue;

                    if (p[1] is JsonArray meta && meta.Count >= 2)
                    {
                        cid = meta[0]?.GetValue<string>() ?? cid;
                        rid = meta[1]?.GetValue<string>() ?? rid;
                    }

                    if (p[4] is not JsonArray candidates)
                        continue;

                    foreach (var candidate in candidates)
                    {
                        if (candidate is not JsonArray c || c.Count < 2)
                            continue;
                        if (c[1] is JsonArray t && t.Count > 0)
                        {
                            string? s = t[0]?.GetValue<string>();
                            if (!string.IsNullOrEmpty(s))
                                text = s;
                        }
                        thoughts = ExtractThoughts(c) ?? thoughts;
                    }
                }
            }
        }

        foreach (var frame in parser.Flush())
        {
            // Frames arriving after the stream closed still carry the final reply.
            if (frame is not JsonArray envelope || envelope.Count < 3)
                continue;
            if (envelope[2] is not JsonValue value || !value.TryGetValue<string>(out var raw))
                continue;
            JsonNode? part;
            try
            {
                part = JsonNode.Parse(raw);
            }
            catch (JsonException)
            {
                continue;
            }
            if (part is not JsonArray p || p.Count < 5 || p[4] is not JsonArray candidates)
                continue;
            foreach (var candidate in candidates)
            {
                if (candidate is JsonArray c && c.Count > 1 && c[1] is JsonArray t && t.Count > 0)
                {
                    string? s = t[0]?.GetValue<string>();
                    if (!string.IsNullOrEmpty(s))
                        text = s;
                }
            }
        }

        return new GeminiReply(text, thoughts, cid, rid);
    }

    private static string? ExtractThoughts(JsonArray candidate)
    {
        // Thoughts live at candidate[37][0][0].
        if (candidate.Count <= 37 || candidate[37] is not JsonArray l0 || l0.Count == 0)
            return null;
        if (l0[0] is not JsonArray l1 || l1.Count == 0)
            return null;
        return l1[0]?.GetValue<string>();
    }

    private void ApplyCommonHeaders(HttpRequestMessage request)
    {
        request.Headers.TryAddWithoutValidation(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36");
        request.Headers.TryAddWithoutValidation("Origin", "https://gemini.google.com");
        request.Headers.TryAddWithoutValidation("Referer", "https://gemini.google.com/");
        request.Headers.TryAddWithoutValidation("Cookie", _cookieHeader);
    }

    private static string Extract(string body, string key)
    {
        Match m = Regex.Match(body, "\"" + Regex.Escape(key) + "\":\\s*\"(.*?)\"");
        return m.Success ? m.Groups[1].Value : string.Empty;
    }

    private static string Truncate(string s, int max) =>
        s.Length <= max ? s : s[..max] + "…";

    public void Dispose() => _http.Dispose();
}

/// <summary>The assistant's reply plus the handles needed to continue the conversation.</summary>
/// <param name="Text">The assistant's visible reply.</param>
/// <param name="Thoughts">The model's extended-thinking summary, when enabled.</param>
/// <param name="ConversationId">The <c>cid</c> conversation handle.</param>
/// <param name="ReplyId">The <c>rid</c> reply handle.</param>
/// <param name="Metadata">The <c>[cid, rid, rcid]</c> triple to pass back to continue the chat.</param>
internal sealed record GeminiReply(
    string Text,
    string Thoughts,
    string ConversationId,
    string ReplyId)
{
    /// <summary>The metadata triple for the next turn.</summary>
    public string[] Metadata => [ConversationId, ReplyId, ""];
}

/// <summary>Raised when the session cookies are rejected by Google.</summary>
internal sealed class GeminiAuthException(string message) : Exception(message);

/// <summary>Raised when a Gemini request fails.</summary>
internal sealed class GeminiApiException(string message) : Exception(message);

