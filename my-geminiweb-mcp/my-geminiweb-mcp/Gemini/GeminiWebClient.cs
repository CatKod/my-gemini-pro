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
    private const string BatchExecuteUrl = "https://gemini.google.com/_/BardChatUi/data/batchexecute";
    private const string ModelHeaderKey = "x-goog-ext-525001261-jspb";
    private const string RequestIdHeaderKey = "x-goog-ext-525005358-jspb";

    /// <summary>The RPC that returns the account's model list.</summary>
    private const string GetUserStatusRpcId = "otAQ7b";

    /// <summary>
    /// How long a stream may stay silent before it is declared dead.
    /// </summary>
    /// <remarks>
    /// A stale model id does not produce an error: Google opens the stream and then never
    /// sends anything. The idle timeout turns that silence into a retryable failure
    /// instead of an indefinite hang. It matches the watchdog of the Python reference
    /// implementation, and is generous enough for a thinking model, which can stay silent
    /// for a minute before its first token.
    /// </remarks>
    private static readonly TimeSpan StreamIdleTimeout = TimeSpan.FromSeconds(120);

    /// <summary>
    /// How long one whole turn may take before it is declared dead.
    /// </summary>
    /// <remarks>
    /// The idle timeout cannot catch a locked model: Google keeps the stream open and
    /// sends keep-alive frames, so the stream is never idle — it just never produces an
    /// answer. Verified 2026-11-18: a request for the locked `4 Carbon` model kept the
    /// stream open for 901 s. A budget on the whole turn catches that case.
    ///
    /// It is generous enough for a Deep Research turn, which can think for minutes.
    /// </remarks>
    private static readonly TimeSpan TurnTimeout = TimeSpan.FromSeconds(300);

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

    /// <summary>
    /// The models this account is offered, discovered from Gemini itself.
    /// </summary>
    public ModelRegistry Models { get; } = new();

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

        await DiscoverModelsAsync(ct);

        return Session;
    }

    /// <summary>
    /// Asks Gemini which models this account is offered.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The model list is per account and per frontend build: Google renames, renumbers and
    /// re-tiers models without notice, so a hardcoded table goes stale. A stale id does not
    /// produce an error — Google opens a stream that never answers — which is why the
    /// selection must come from the account's own RPC response.
    /// </para>
    /// <para>
    /// A failure here is not fatal: the chat still works without a model header, letting
    /// Google pick the account's default model.
    /// </para>
    /// </remarks>
    /// <param name="ct">Cancellation token.</param>
    public async Task DiscoverModelsAsync(CancellationToken ct = default)
    {
        try
        {
            var body = await BatchExecuteAsync(GetUserStatusRpcId, "[]", ct);
            if (body is not null)
                Models.ApplyUserStatus(body);
        }
        catch (Exception)
        {
            // Discovery is best-effort: without it, requests simply omit the model header.
        }
    }

    /// <summary>
    /// Runs one RPC through Google's batchexecute endpoint and returns its payload.
    /// </summary>
    /// <param name="rpcId">The RPC id, e.g. <c>otAQ7b</c>.</param>
    /// <param name="payload">The RPC payload, as JSON.</param>
    /// <param name="ct">Cancellation token.</param>
    /// <returns>The parsed payload, or <c>null</c> when the RPC did not answer.</returns>
    private async Task<JsonArray?> BatchExecuteAsync(
        string rpcId, string payload, CancellationToken ct)
    {
        var session = Session!;

        var query = new List<string>
        {
            "rpcids=" + rpcId,
            "hl=" + Uri.EscapeDataString(session.Language),
            "_reqid=" + _reqId,
            "rt=c",
            "source-path=/app",
        };
        _reqId += 100_000;

        if (session.BuildLabel.Length > 0)
            query.Add("bl=" + Uri.EscapeDataString(session.BuildLabel));
        if (session.SessionId.Length > 0)
            query.Add("f.sid=" + Uri.EscapeDataString(session.SessionId));

        using var request = new HttpRequestMessage(
            HttpMethod.Post, $"{BatchExecuteUrl}?{string.Join('&', query)}");
        ApplyCommonHeaders(request);
        request.Headers.TryAddWithoutValidation("X-Same-Domain", "1");

        // The batchexecute model header carries the session id as its trailing value.
        request.Headers.TryAddWithoutValidation(
            ModelHeaderKey,
            $"[1,null,null,null,null,null,null,null,[4,5,6,8],null,null,null,null,null,null,null,\"{_sessionId}\"]");
        request.Headers.TryAddWithoutValidation("x-goog-ext-73010989-jspb", "[0]");

        request.Content = new FormUrlEncodedContent(new Dictionary<string, string>
        {
            ["at"] = session.AccessToken,
            // f.req is the batch: a list of RPC calls, each one [id, payload, null, tag].
            ["f.req"] = new JsonArray(
                new JsonArray(new JsonArray(rpcId, payload, null, "generic"))).ToJsonString(),
        });
        request.Content.Headers.Remove("Content-Type");
        request.Content.Headers.TryAddWithoutValidation(
            "Content-Type", "application/x-www-form-urlencoded;charset=utf-8");

        using var response = await _http.SendAsync(request, ct);
        if (!response.IsSuccessStatusCode)
            return null;

        _jar.Absorb(response);
        string body = await response.Content.ReadAsStringAsync(ct);
        return ParseRpcPayload(body, rpcId);
    }

    /// <summary>
    /// Extracts one RPC's payload from a batchexecute response.
    /// </summary>
    /// <remarks>
    /// The response is a length-prefixed stream of envelopes. Each envelope carries the
    /// RPC id in slot 1 and the payload as a JSON string in slot 2.
    /// </remarks>
    /// <param name="body">The raw response body.</param>
    /// <param name="rpcId">The RPC id to look for.</param>
    /// <returns>The parsed payload, or <c>null</c> when the RPC did not answer.</returns>
    private static JsonArray? ParseRpcPayload(string body, string rpcId)
    {
        var parser = new StreamingFrameParser();
        var decoder = new UTF8Encoding(false);

        List<JsonNode?> frames = [.. parser.Feed(decoder.GetString(Encoding.UTF8.GetBytes(body)))];
        frames.AddRange(parser.Flush());

        foreach (var frame in frames)
        {
            if (frame is not JsonArray envelope || envelope.Count < 3)
                continue;
            if (envelope[1]?.GetValue<string>() != rpcId)
                continue;
            if (envelope[2] is not JsonValue value || !value.TryGetValue<string>(out var raw))
                continue;

            try
            {
                return JsonNode.Parse(raw) as JsonArray;
            }
            catch (JsonException)
            {
                return null;
            }
        }

        return null;
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
    /// Optional model name. The names come from the account itself (e.g. <c>hydrogen</c>,
    /// <c>pro</c>, <c>3.5 flash-lite</c>, <c>carbon</c>), or <c>default</c>, or a raw JSPB
    /// array. When omitted, Google picks the account's default model.
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

        (string? modelHeader, GeminiModel? requestedModel) =
            await ResolveModelHeaderAsync(model);
        int? modelNumber = GeminiModels.ModelNumberOf(modelHeader);
        bool fellBackToDefault = false;
        string? fallbackReason = null;

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
                    fallbackReason = "Google refused the requested model with a generic error.";
                    modelHeader = null;
                    modelNumber = null;
                    continue;
                }

                // When the answer came from the fallback, Google does not echo a model
                // id, so the served model is the account default.
                //
                // When Google does not echo a model id either — which happens on short
                // replies and on the account default — the model that was asked for is
                // the best guess available. It is reported as unconfirmed so the caller
                // can tell a read-from-payload value from a guess.
                string? served = reply.ServedModel;
                bool confirmed = reply.ServedModelConfirmed || reply.ServedModel is not null;

                if (served is null && fallbackReason is not null)
                    served = "account default";
                else if (served is null && requestedModel is not null)
                    served = requestedModel.Description;

                return reply with
                {
                    FallbackReason = fallbackReason,
                    ServedModel = served,
                    ServedModelConfirmed = confirmed,
                };
            }
            catch (GeminiTransientException ex)
            {
                lastError = new GeminiApiException(ex.Message);
                if (attempt >= 3)
                    break;
                await Task.Delay(TimeSpan.FromSeconds(5 * attempt), ct);
            }
            catch (GeminiStreamStalledException ex) when (modelHeader is not null && !fellBackToDefault)
            {
                // The stream opened and then went silent. That is what a model id this
                // account is not offered looks like: Google accepts the request and never
                // answers it. Retry once without a model header and let Google pick the
                // account's default model.
                fellBackToDefault = true;
                fallbackReason = ex.Message;
                lastError = ex;
                modelHeader = null;
                modelNumber = null;
            }
            catch (GeminiApiException ex) when (IsModelMismatch(ex.Message) && modelHeader is not null && !fellBackToDefault)
            {
                // Same stale-id situation, surfaced as 1050/1052: retry once without
                // a model header and let Google pick the account default.
                fellBackToDefault = true;
                fallbackReason = ex.Message;
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

    /// <summary>
    /// Turns a model name into the model-selection header.
    /// </summary>
    /// <remarks>
    /// The names come from the account's own model list, so a renamed model is still
    /// reachable and a removed one simply no longer resolves.
    /// </remarks>
    /// <param name="model">The requested model name, or null for the account default.</param>
    /// <returns>
    /// The raw header and the model it was built from. The header is <c>null</c> to let
    /// Google choose the default model; the model is then <c>null</c> too.
    /// </returns>
    private async Task<(string? Header, GeminiModel? Model)> ResolveModelHeaderAsync(string? model)
    {
        if (string.IsNullOrWhiteSpace(model))
            return (null, null);

        // A raw JSPB array is passed through untouched.
        if (model.TrimStart().StartsWith('['))
            return (model, null);

        if (!Models.HasModels)
            await InitializeAsync(ct: default).ConfigureAwait(false);

        var resolved = Models.Resolve(model);
        if (resolved is null)
        {
            throw new ArgumentException(
                $"Unknown model '{model}'. Available for this account: {Models.AvailableNames}.");
        }

        // The account is shown the model but may not select it. Asking for it anyway
        // makes Google open a stream that never answers, so the request is refused
        // here instead of hanging until the turn budget runs out.
        if (!resolved.IsSelectable)
        {
            string nowCalled = resolved.CurrentName is not null
                && !string.IsNullOrEmpty(resolved.CurrentName)
                && resolved.CurrentName != resolved.Description
                    ? $" (now called '{resolved.CurrentName}')"
                    : "";

            throw new InvalidOperationException(
                $"Model '{resolved.Description}'{nowCalled} is not available for this account. " +
                "Google would accept the request and never answer it. " +
                $"Selectable models: {Models.AvailableNames}.");
        }

        return (GeminiModels.BuildHeader(resolved), resolved);
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
        string? servedModel = null;
        string? quotaNotice = null;
        int? apiError = null;
        var rawCandidates = new List<JsonArray>();

        await using (var stream = await response.Content.ReadAsStreamAsync(ct))
        {
            bool gotAnswer = false;
            using var overall = CancellationTokenSource.CreateLinkedTokenSource(ct);

            // A model the account may not use (a locked one) is answered differently from
            // a stale id: Google keeps the stream open and sends keep-alive frames for
            // minutes without ever producing an answer. An idle timeout never fires
            // there, because the stream is not idle — it is just silent about the
            // answer. A budget on the whole turn catches that case.
            overall.CancelAfter(TurnTimeout);

            try
            {
                while (true)
                {
                    // A stale model id does not fail: Google opens the stream and then goes
                    // silent forever. The idle timeout turns that silence into a retryable
                    // failure instead of an indefinite hang.
                    //
                    // The timeout is applied to the read itself, not as a wrapper around it:
                    // a wrapper leaves the underlying read pending, and disposing the response
                    // then blocks on that pending read — which would hang the whole call
                    // instead of failing it.
                    int read;
                    using (var idle = CancellationTokenSource.CreateLinkedTokenSource(overall.Token))
                    {
                        idle.CancelAfter(StreamIdleTimeout);
                        try
                        {
                            read = await stream.ReadAsync(buffer, idle.Token);
                        }
                        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                        {
                            if (overall.IsCancellationRequested)
                            {
                                throw new GeminiStreamStalledException(
                                    $"Gemini kept the stream open for {TurnTimeout.TotalSeconds:F0}s without "
                                    + "answering. The requested model is probably not offered to this account.");
                            }

                            throw new GeminiStreamStalledException(
                                $"Gemini opened the stream but sent nothing for {StreamIdleTimeout.TotalSeconds:F0}s. " +
                                "The requested model is probably not offered to this account.");
                        }
                    }

                    if (read <= 0)
                        break;

                    foreach (var frame in parser.Feed(decoder.GetString(buffer, 0, read)))
                    {
                        ProcessFrame(frame, ref text, ref thoughts, ref cid, ref rid,
                            ref apiError, rawCandidates, ref servedModel, ref quotaNotice);
                    }

                    if (!gotAnswer && (text.Length > 0 || thoughts.Length > 0))
                    {
                        // The answer has started. The first-token budget has done its job;
                        // from here on the idle timeout is enough, so a long Deep Research
                        // turn is not cut short.
                        gotAnswer = true;
                        overall.CancelAfter(Timeout.InfiniteTimeSpan);
                    }
                }
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                throw new GeminiStreamStalledException(
                    $"Gemini kept the stream open for {TurnTimeout.TotalSeconds:F0}s without "
                    + "answering. The requested model is probably not offered to this account.");
            }
        }

        foreach (var frame in parser.Flush())
        {
            // Frames arriving after the stream closed still carry the final reply.
            ProcessFrame(frame, ref text, ref thoughts, ref cid, ref rid,
                ref apiError, rawCandidates, ref servedModel, ref quotaNotice);
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

        return new GeminiReply(
            text, thoughts, cid, rid, files, servedModel, quotaNotice,
            ServedModelConfirmed: servedModel is not null);
    }

    private static void ProcessFrame(
        JsonNode? frame,
        ref string text, ref string thoughts, ref string cid, ref string rid,
        ref int? apiError, List<JsonArray> rawCandidates,
        ref string? servedModel, ref string? quotaNotice)
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

        // The reply carries the model that actually served it. When the quota of the
        // requested model is exhausted, Google silently serves Flash-Lite instead and
        // says so in a note next to the answer. Without reading that, a caller would
        // believe it got the model it asked for.
        servedModel = ExtractServedModel(p) ?? servedModel;
        quotaNotice = ExtractQuotaNotice(p) ?? quotaNotice;

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

    /// <summary>Reads the display name of the model that served the reply.</summary>
    /// <remarks>
    /// <para>
    /// The payload carries the model id and its display name next to each other:
    /// <c>"cf41b0e0dd7d53e5", null, null, "3.5 Flash-Lite"</c>. The id is a 16-char hex
    /// string, which is what makes the pair recognisable without hardcoding an index.
    /// </para>
    /// <para>
    /// The pair does not always sit at the top level of the candidate, so the search
    /// descends — through arrays, through objects, and through strings that themselves
    /// hold JSON. The last one matters: the frame envelope carries the payload as a JSON
    /// string, and a turn that is echoed deeper still keeps wrapping it.
    /// </para>
    /// </remarks>
    private static string? ExtractServedModel(JsonNode? node, int depth = 0)
    {
        // The payload nests deeply; a bound keeps a malformed reply from recursing forever.
        if (depth > 12 || node is null)
            return null;

        if (node is JsonArray array)
        {
            // Shallow pass first: the id and its label are siblings.
            for (int i = 0; i < array.Count; i++)
            {
                if (array[i] is not JsonValue value
                    || !value.TryGetValue<string>(out string? s)
                    || !IsModelId(s))
                {
                    continue;
                }

                for (int j = i + 1; j < Math.Min(i + 6, array.Count); j++)
                {
                    if (array[j] is JsonValue name
                        && name.TryGetValue<string>(out string? label)
                        && label.Length > 0)
                    {
                        return label;
                    }
                }
            }

            // Then descend: the pair can be nested several levels down.
            foreach (var child in array)
            {
                var nested = ExtractServedModel(child, depth + 1);
                if (nested is not null)
                    return nested;
            }

            return null;
        }

        if (node is JsonObject obj)
        {
            foreach (var (_, child) in obj)
            {
                var nested = ExtractServedModel(child, depth + 1);
                if (nested is not null)
                    return nested;
            }

            return null;
        }

        // A JSON string may itself hold a JSON payload.
        if (node is JsonValue str
            && str.TryGetValue<string>(out string? raw)
            && raw.Length > 1
            && raw[0] == '[')
        {
            try
            {
                return ExtractServedModel(JsonNode.Parse(raw), depth + 1);
            }
            catch (JsonException)
            {
                // Not JSON after all — fall through.
            }
        }

        return null;
    }

    /// <summary>Whether a string looks like Gemini's internal model id.</summary>
    private static bool IsModelId(string? value) =>
        value is { Length: 16 } && value.All(Uri.IsHexDigit);

    /// <summary>Reads Google's note about a quota-driven downgrade, when there is one.</summary>
    /// <remarks>
    /// The note looks like <c>"Limit reached. Continuing with Flash-Lite."</c>. When it is
    /// there, the reply did NOT come from the model that was asked for: the quota of that
    /// model was exhausted and Google served a cheaper one instead.
    /// </remarks>
    private static string? ExtractQuotaNotice(JsonNode? node)
    {
        if (node is JsonValue value
            && value.TryGetValue<string>(out string? s)
            && s.Contains("Limit reached", StringComparison.OrdinalIgnoreCase))
        {
            return s;
        }

        if (node is JsonArray array)
        {
            foreach (var child in array)
            {
                var nested = ExtractQuotaNotice(child);
                if (nested is not null)
                    return nested;
            }
        }

        return null;
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
/// <param name="ServedModel">
/// The model that answered. Echoed by Google when the reply carries a model id;
/// otherwise it is the model that was asked for, which is only a best guess.
/// </param>
/// <param name="QuotaNotice">Google's note about a quota-driven downgrade, when present.</param>
/// <param name="FallbackReason">Why the server fell back to the account default, when it did.</param>
/// <param name="ServedModelConfirmed">
/// Whether <see cref="ServedModel"/> was read from the reply payload. False means the
/// model was not echoed by Google and the value is the model that was requested.
/// </param>
internal sealed record GeminiReply(
    string Text,
    string Thoughts,
    string ConversationId,
    string ReplyId,
    IReadOnlyList<GeneratedFile>? Files = null,
    string? ServedModel = null,
    string? QuotaNotice = null,
    string? FallbackReason = null,
    bool ServedModelConfirmed = false)
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

/// <summary>
/// Raised when Google opens a stream and then never answers.
/// </summary>
/// <remarks>
/// This is what a model the account is not offered looks like: the request is accepted,
/// the HTTP status is 200, and no frame ever arrives. Treating it as a distinct failure
/// lets the caller retry without the model header, so Google picks the default model
/// instead of hanging.
/// </remarks>
internal sealed class GeminiStreamStalledException(string message) : GeminiApiException(message);

