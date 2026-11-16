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
    private readonly CookieJar _jar;
    private readonly string _sessionId;
    private int _reqId = Random.Shared.Next(10_000, 99_999);

    /// <summary>
    /// Creates a client for one account.
    /// </summary>
    /// <param name="psid">The <c>__Secure-1PSID</c> cookie value.</param>
    /// <param name="psidts">The <c>__Secure-1PSIDTS</c> cookie value.</param>
    /// <param name="http">An optional shared <see cref="HttpClient"/>.</param>
    /// <param name="extraCookies">
    /// Additional cookies from the same session (for example <c>NID</c>). They are required
    /// for <see cref="RotateCookies"/> to mint a fresh <c>__Secure-1PSIDTS</c>.
    /// </param>
    public GeminiWebClient(
        string psid,
        string psidts,
        HttpClient? http = null,
        IEnumerable<KeyValuePair<string, string>>? extraCookies = null)
    {
        _jar = new CookieJar();
        _jar.Set("__Secure-1PSID", psid);
        _jar.Set("__Secure-1PSIDTS", psidts);
        if (extraCookies is not null)
        {
            foreach (var (name, value) in extraCookies)
            {
                if (!string.IsNullOrEmpty(value))
                    _jar.Set(name, value);
            }
        }

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

    /// <summary>The cookie jar, so callers can read refreshed cookie values.</summary>
    public CookieJar Cookies => _jar;

    /// <summary>Refreshes the <c>__Secure-1PSIDTS</c> cookie.</summary>
    public async Task<string?> RotateCookiesAsync(string accountKey, CancellationToken ct = default)
    {
        var rotator = new CookieRotator(_http, _jar);
        return await rotator.RotateAsync(accountKey, ct);
    }

    /// <summary>Reads the bootstrap page and extracts the session tokens.</summary>
    /// <param name="fullJar">
    /// When true, the whole cookie set is sent instead of only the two authentication
    /// cookies. The extra cookies make the request look like the browser's, which is what
    /// earns back the <c>__Secure-3PSID</c> family in the response.
    /// </param>
    /// <param name="ct">Cancellation token.</param>
    public async Task<GeminiSession> InitializeAsync(bool fullJar = false, CancellationToken ct = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, InitUrl);
        ApplyCommonHeaders(request, fullJar);

        using var response = await _http.SendAsync(request, ct);
        response.EnsureSuccessStatusCode();
        string body = await response.Content.ReadAsStringAsync(ct);

        _jar.Absorb(response);

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
    /// Optional model name (<c>flash</c>, <c>pro</c>, <c>flash-lite</c>, with optional
    /// <c>-plus</c>/<c>-advanced</c> suffix), <c>default</c>, or a raw JSPB array.
    /// When omitted, Google picks the account's default model.
    /// </param>
    /// <param name="attachments">Optional local file paths to upload with the turn.</param>
    /// <param name="extendedThinking">When true, requests extended thinking (slot 80 = 2).</param>
    /// <param name="downloadsFolder">Where files Gemini generates are saved, or null to skip.</param>
    /// <param name="ct">Cancellation token.</param>
    public async Task<GeminiReply> GenerateAsync(
        string prompt,
        IReadOnlyList<string>? chatMetadata = null,
        string? model = null,
        IReadOnlyList<string>? attachments = null,
        bool extendedThinking = false,
        string? downloadsFolder = null,
        CancellationToken ct = default)
    {
        if (Session is null)
            await InitializeAsync(ct: ct);

        // Uploads happen once: the server ids are reused across transient retries.
        JsonArray? fileData = null;
        if (attachments is { Count: > 0 })
        {
            var transfer = new FileTransfer(_http, _jar, Session!.PushId, downloadsFolder);
            var uploads = await transfer.UploadAsync(attachments, ct);
            fileData = FileTransfer.BuildFileData(uploads);
        }

        string? modelHeader = GeminiModels.ResolveHeader(model);
        int? modelNumber = ModelNumberOf(modelHeader);
        bool fellBackToDefault = false;

        GeminiApiException? lastError = null;
        for (int attempt = 1; attempt <= 3; attempt++)
        {
            try
            {
                var reply = await GenerateOnceAsync(
                    prompt, chatMetadata, modelHeader, modelNumber,
                    fileData, extendedThinking, downloadsFolder, ct);

                // Google answers a generic "sorry" instead of an error code when the
                // hardcoded model id does not match what this account is offered
                // (models are discovered per account at runtime — the ids go stale).
                // Fall back to the account default once rather than returning sorry.
                if (modelHeader is not null && !fellBackToDefault && IsGenericRefusal(reply.Text))
                {
                    fellBackToDefault = true;
                    modelHeader = null;
                    modelNumber = null;
                    continue;
                }

                return reply;
            }
            catch (GeminiTransientException ex)
            {
                lastError = new GeminiApiException(ex.Message);
                if (attempt >= 3)
                    break;
                await Task.Delay(TimeSpan.FromSeconds(5 * attempt), ct);
            }
            catch (GeminiApiException ex) when (IsModelMismatch(ex.Message) && modelHeader is not null && !fellBackToDefault)
            {
                // Same stale-id situation, surfaced as 1050/1052: retry once without
                // a model header and let Google pick the account default.
                fellBackToDefault = true;
                lastError = ex;
                modelHeader = null;
                modelNumber = null;
            }
            catch (GeminiApiException ex) when (IsTransientMessage(ex.Message) && attempt < 3)
            {
                lastError = ex;
                await Task.Delay(TimeSpan.FromSeconds(5 * attempt), ct);
            }
        }

        throw lastError ?? new GeminiApiException("StreamGenerate failed after 3 attempts.");
    }

    private async Task<GeminiReply> GenerateOnceAsync(
        string prompt,
        IReadOnlyList<string>? chatMetadata,
        string? modelHeader,
        int? modelNumber,
        JsonArray? fileData,
        bool extendedThinking,
        string? downloadsFolder,
        CancellationToken ct)
    {
        var session = Session!;
        string requestUuid = Guid.NewGuid().ToString().ToUpperInvariant();

        var inner = new JsonArray();
        for (int i = 0; i < 81; i++)
            inner.Add((JsonNode?)null);

        inner[0] = new JsonArray(prompt, 0, null, fileData, null, null, 0);
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
        inner[79] = modelNumber ?? 1;
        inner[80] = extendedThinking ? 2 : 1;

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

        if (modelHeader is not null)
        {
            try
            {
                var headerArray = JsonNode.Parse(modelHeader)!.AsArray();
                headerArray.Add((JsonNode?)JsonValue.Create(extendedThinking ? 2 : 1));
                headerArray.Add((JsonNode?)JsonValue.Create(_sessionId));
                request.Headers.TryAddWithoutValidation(
                    ModelHeaderKey, headerArray.ToJsonString());
                // The Python reference (gemini_webapi.constants.build_model_header)
                // always sends these two companion headers alongside a model
                // selection. Without them Google answers a generic
                // "something went wrong" instead of a model reply.
                request.Headers.TryAddWithoutValidation("x-goog-ext-73010989-jspb", "[0]");
                request.Headers.TryAddWithoutValidation("x-goog-ext-73010990-jspb", "[0,0,0]");
            }
            catch (JsonException ex)
            {
                throw new GeminiApiException($"Invalid model header: {ex.Message}");
            }
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

        // Google refreshes the session cookies on regular responses too.
        _jar.Absorb(response);

        return await ReadReplyAsync(response, downloadsFolder, ct);
    }

    /// <summary>The last segment of a JSPB model header: the model number Google also wants in slot 79.</summary>
    private static int? ModelNumberOf(string? header)
    {
        if (string.IsNullOrEmpty(header))
            return null;
        try
        {
            var array = JsonNode.Parse(header)!.AsArray();
            if (array.Count > 0 && array[^1] is JsonValue v && v.TryGetValue<int>(out int n))
                return n;
        }
        catch (JsonException)
        {
        }
        return null;
    }

    /// <summary>Whether a failure message looks transient and worth one more attempt.</summary>
    private static bool IsTransientMessage(string message) =>
        message.Contains("1013")
        || message.Contains("1097")
        || message.Contains("temporary", StringComparison.OrdinalIgnoreCase)
        || message.Contains("temporar", StringComparison.OrdinalIgnoreCase);

    /// <summary>Whether the failure is a stale hardcoded model id (1050/1052).</summary>
    private static bool IsModelMismatch(string message) =>
        message.Contains("1050") || message.Contains("1052");

    /// <summary>
    /// Whether the reply is Google's generic refusal for a model id the account
    /// is not offered, rather than a real answer.
    /// </summary>
    private static bool IsGenericRefusal(string text)
    {
        string t = text.Trim();
        return t.StartsWith("Sorry, something went wrong", StringComparison.OrdinalIgnoreCase)
            || t.StartsWith("Rất tiếc, đã xảy ra lỗi", StringComparison.OrdinalIgnoreCase);
    }

    private async Task<GeminiReply> ReadReplyAsync(
        HttpResponseMessage response, string? downloadsFolder, CancellationToken ct)
    {
        var parser = new StreamingFrameParser();
        var decoder = new UTF8Encoding(false);
        var buffer = new byte[8192];

        string text = "";
        string thoughts = "";
        string cid = "";
        string rid = "";
        int? apiError = null;
        var rawCandidates = new List<JsonArray>();

        await using (var stream = await response.Content.ReadAsStreamAsync(ct))
        {
            int read;
            while ((read = await stream.ReadAsync(buffer, ct)) > 0)
            {
                foreach (var frame in parser.Feed(decoder.GetString(buffer, 0, read)))
                {
                    ProcessFrame(frame, ref text, ref thoughts, ref cid, ref rid, ref apiError, rawCandidates);
                }
            }
        }

        foreach (var frame in parser.Flush())
        {
            // Frames arriving after the stream closed still carry the final reply.
            ProcessFrame(frame, ref text, ref thoughts, ref cid, ref rid, ref apiError, rawCandidates);
        }

        if (apiError is not null)
            ThrowForApiError(apiError.Value);

        List<GeneratedFile> files = [];
        if (downloadsFolder is not null && rawCandidates.Count > 0 && Session is not null)
        {
            try
            {
                var transfer = new FileTransfer(_http, _jar, Session.PushId, downloadsFolder);
                files = await transfer.DownloadGeneratedAsync(rawCandidates, ct);
            }
            catch (Exception)
            {
                // File download is best-effort: the chat reply is still valid.
            }
        }

        return new GeminiReply(text, thoughts, cid, rid, files);
    }

    private static void ProcessFrame(
        JsonNode? frame,
        ref string text, ref string thoughts, ref string cid, ref string rid,
        ref int? apiError, List<JsonArray> rawCandidates)
    {
        if (frame is not JsonArray envelope || envelope.Count < 3)
            return;
        if (envelope[2] is not JsonValue value || !value.TryGetValue<string>(out var raw))
            return;

        JsonNode? part;
        try
        {
            part = JsonNode.Parse(raw);
        }
        catch (JsonException)
        {
            return;
        }

        if (part is not JsonArray p || p.Count < 5)
            return;

        int? code = NestedInt(p, [5, 2, 0, 1, 0]);
        if (code is not null)
            apiError = code;

        if (p[1] is JsonArray meta && meta.Count >= 2)
        {
            cid = meta[0]?.GetValue<string>() ?? cid;
            rid = meta[1]?.GetValue<string>() ?? rid;
        }

        if (p[4] is not JsonArray candidates)
            return;

        foreach (var candidate in candidates)
        {
            if (candidate is not JsonArray c || c.Count < 2)
                continue;
            rawCandidates.Add(c);
            if (c[1] is JsonArray t && t.Count > 0)
            {
                string? s = t[0]?.GetValue<string>();
                if (!string.IsNullOrEmpty(s))
                    text = s;
            }
            thoughts = ExtractThoughts(c) ?? thoughts;
        }
    }

    /// <summary>Reads a nested int at a path like [5,2,0,1,0]; null when absent.</summary>
    private static int? NestedInt(JsonNode? node, int[] path)
    {
        JsonNode? current = node;
        foreach (int index in path)
        {
            if (current is JsonArray array)
            {
                if (index < 0 || index >= array.Count)
                    return null;
                current = array[index];
            }
            else
            {
                return null;
            }
        }

        if (current is JsonValue v && v.TryGetValue<int>(out int n))
            return n;
        return null;
    }

    private static void ThrowForApiError(int code)
    {
        throw code switch
        {
            1013 => new GeminiTransientException("Gemini encountered a temporary error (1013). Retrying..."),
            1037 => new GeminiApiException(
                "Usage limit exceeded. Switch to a different model (e.g. Gemini Flash), or check limits on gemini.google.com."),
            1050 or 1052 => new GeminiApiException(
                $"The model is inconsistent with the conversation history (code {code}). Use the same model for the whole chat."),
            1060 => new GeminiApiException(
                "The IP address was temporarily blocked by Google (1060). Wait a while or switch network before retrying."),
            _ => new GeminiTransientException($"Gemini returned API error code {code}. Retrying..."),
        };
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

    private void ApplyCommonHeaders(HttpRequestMessage request, bool fullJar = false)
    {
        request.Headers.TryAddWithoutValidation(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36");
        request.Headers.TryAddWithoutValidation("Origin", "https://gemini.google.com");
        request.Headers.TryAddWithoutValidation("Referer", "https://gemini.google.com/");
        request.Headers.TryAddWithoutValidation("Cookie", _jar.ToHeader(onlyAuth: !fullJar));
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
/// <param name="Files">Files Gemini generated during this turn, already saved to disk.</param>
/// <param name="Metadata">The <c>[cid, rid, rcid]</c> triple to pass back to continue the chat.</param>
internal sealed record GeminiReply(
    string Text,
    string Thoughts,
    string ConversationId,
    string ReplyId,
    IReadOnlyList<GeneratedFile>? Files = null)
{
    /// <summary>The metadata triple for the next turn.</summary>
    public string[] Metadata => [ConversationId, ReplyId, ""];
}

/// <summary>Raised when the session cookies are rejected by Google.</summary>
internal sealed class GeminiAuthException(string message) : Exception(message);

/// <summary>Raised when a Gemini request fails.</summary>
internal class GeminiApiException(string message) : Exception(message);

/// <summary>Raised for transient Google errors (1013, ...) that deserve one more attempt.</summary>
internal sealed class GeminiTransientException(string message) : GeminiApiException(message);

