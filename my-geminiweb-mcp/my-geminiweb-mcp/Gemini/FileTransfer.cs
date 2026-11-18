using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// Uploads attachments to Google and downloads files Gemini generates.
/// </summary>
/// <remarks>
/// <para>
/// Upload goes to <c>https://content-push.googleapis.com/upload</c> as multipart form data
/// (part name <c>file</c>), with <c>X-Tenant-Id: bard-storage</c> and the session's
/// <c>Push-ID</c>. The server answers with a file identifier such as
/// <c>/contrib_service/ttl_1d/...</c>, which is then referenced from the chat payload.
/// </para>
/// <para>
/// Files Gemini generates are not exposed in the parsed reply. They hide in the raw
/// candidate: a <c>[file-tag: ...]</c> marker whose sibling list carries the filename, the
/// mime type, an optional base64 blob, and a download URL on
/// <c>contribution.usercontent.google.com</c>. Both shapes are handled: the blob is decoded
/// directly, otherwise the URL is fetched with the signed-in session.
/// </para>
/// </remarks>
internal sealed class FileTransfer(HttpClient http, CookieJar jar, string pushId, string? downloadsFolder)
{
    private const string UploadUrl = "https://content-push.googleapis.com/upload";
    private const string FileTagPrefix = "[file-tag:";

    /// <summary>Uploads local files, returning their server ids and names.</summary>
    public async Task<List<(string Url, string FileName)>> UploadAsync(
        IEnumerable<string> paths, CancellationToken ct = default)
    {
        var result = new List<(string Url, string FileName)>();
        foreach (string path in paths)
        {
            if (!File.Exists(path))
                throw new FileNotFoundException($"Attachment not found: {path}");

            string fileName = Path.GetFileName(path);
            byte[] bytes = await File.ReadAllBytesAsync(path, ct);
            string mime = GuessMime(fileName);

            // Build the multipart body by hand. MultipartFormDataContent quotes the
            // boundary (boundary="...") and emits RFC-5987 filename* parameters that
            // Google's content-push endpoint rejects with "Malformed multipart body".
            // curl_mime (what gemini_webapi uses) sends a bare boundary and a plain
            // disposition line, so we match that byte-for-byte.
            string boundary = "----GeminiBoundary" + Guid.NewGuid().ToString("N");
            byte[] body = BuildMultipartBody(boundary, fileName, mime, bytes);
            var bodyContent = new ByteArrayContent(body);
            bodyContent.Headers.TryAddWithoutValidation(
                "Content-Type", $"multipart/form-data; boundary={boundary}");

            using var request = new HttpRequestMessage(HttpMethod.Post, UploadUrl);
            request.Headers.TryAddWithoutValidation(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36");
            request.Headers.TryAddWithoutValidation("Origin", "https://gemini.google.com");
            request.Headers.TryAddWithoutValidation("Referer", "https://gemini.google.com/");
            request.Headers.TryAddWithoutValidation("X-Tenant-Id", "bard-storage");
            request.Headers.TryAddWithoutValidation("Push-ID", pushId);
            request.Headers.TryAddWithoutValidation("Cookie", jar.ToHeader());
            request.Content = bodyContent;

            using var response = await http.SendAsync(request, ct);
            if (!response.IsSuccessStatusCode)
            {
                string err = await response.Content.ReadAsStringAsync(ct);
                throw new GeminiApiException(
                    $"File upload failed with HTTP {(int)response.StatusCode}: {Truncate(err, 200)}");
            }

            jar.Absorb(response);
            string id = (await response.Content.ReadAsStringAsync(ct)).Trim().Trim('"');
            if (id.Length == 0)
                throw new GeminiApiException("File upload returned an empty file id.");
            result.Add((id, fileName));
        }

        return result;
    }

    /// <summary>
    /// Builds the <c>file_data</c> payload: one <c>[[url], name]</c> pair per upload.
    /// </summary>
    public static JsonArray BuildFileData(IEnumerable<(string Url, string FileName)> uploads)
    {
        var array = new JsonArray();
        foreach (var (url, name) in uploads)
            array.Add((JsonNode?)new JsonArray((JsonNode?)new JsonArray((JsonNode?)url), (JsonNode?)name));
        return array;
    }

    /// <summary>
    /// Extracts generated files from raw candidates and saves them to the downloads folder.
    /// </summary>
    public async Task<List<GeneratedFile>> DownloadGeneratedAsync(
        IEnumerable<JsonArray> candidates, CancellationToken ct = default)
    {
        var files = new List<GeneratedFile>();
        var seen = new HashSet<string>(StringComparer.Ordinal);

        foreach (var candidate in candidates)
        {
            foreach (var info in ExtractFrom(candidate))
            {
                if (!seen.Add(info.Tag))
                    continue;

                byte[]? bytes = TryDecodeBlob(info.BlobB64);
                string source = "blob";
                if (bytes is null && !string.IsNullOrEmpty(info.DownloadUrl))
                {
                    bytes = await DownloadUrlAsync(info.DownloadUrl, ct);
                    source = "url";
                }

                if (bytes is null)
                    continue;

                string fileName = Sanitize(info.FileName ?? "gemini_output");
                string? savedPath = null;
                if (!string.IsNullOrEmpty(downloadsFolder))
                {
                    Directory.CreateDirectory(downloadsFolder);
                    savedPath = Path.Combine(downloadsFolder, UniqueName(downloadsFolder, fileName));
                    await File.WriteAllBytesAsync(savedPath, bytes, ct);
                }

                files.Add(new GeneratedFile(
                    fileName,
                    savedPath,
                    bytes.Length,
                    string.IsNullOrEmpty(info.MimeType) ? GuessMime(fileName) : info.MimeType,
                    info.DownloadUrl,
                    source));
            }
        }

        return files;
    }

    private async Task<byte[]?> DownloadUrlAsync(string url, CancellationToken ct)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            request.Headers.TryAddWithoutValidation("Referer", "https://gemini.google.com/");
            request.Headers.TryAddWithoutValidation("Cookie", jar.ToHeader());

            using var response = await http.SendAsync(request, ct);
            if (!response.IsSuccessStatusCode)
                return null;

            jar.Absorb(response);
            return await response.Content.ReadAsByteArrayAsync(ct);
        }
        catch (HttpRequestException)
        {
            return null;
        }
        catch (TaskCanceledException)
        {
            return null;
        }
    }

    /// <summary>Finds every file-tag info block under a raw candidate node.</summary>
    private static List<FileTagInfo> ExtractFrom(JsonNode? root)
    {
        var found = new List<FileTagInfo>();
        if (root is null)
            return found;

        var stack = new Stack<JsonNode>();
        stack.Push(root);

        while (stack.Count > 0)
        {
            var node = stack.Pop();
            if (node is JsonArray array)
            {
                // A file-tag block: [tag, ..., blob] where blob is the file info list.
                if (array.Count > 2
                    && array[0] is JsonValue tagValue
                    && tagValue.TryGetValue<string>(out string? tag)
                    && tag.StartsWith(FileTagPrefix, StringComparison.Ordinal)
                    && array[2] is JsonArray blob)
                {
                    found.Add(ReadBlob(tag, blob));
                }

                foreach (var child in array)
                {
                    if (child is not null)
                        stack.Push(child);
                }
            }
            else if (node is JsonObject obj)
            {
                foreach (var (_, child) in obj)
                {
                    if (child is not null)
                        stack.Push(child);
                }
            }
        }

        return found;
    }

    private static FileTagInfo ReadBlob(string tag, JsonArray blob)
    {
        string? name = blob.Count > 2 ? blob[2]?.GetValue<string>() : null;
        string? mime = blob.Count > 3 ? blob[3]?.GetValue<string>() : null;
        string? b64 = blob.Count > 5 ? blob[5]?.GetValue<string>() : null;

        string? thumb = null;
        string? download = null;
        string? upload = null;
        if (blob.Count > 7 && blob[7] is JsonArray urls)
        {
            thumb = urls.Count > 0 ? urls[0]?.GetValue<string>() : null;
            download = urls.Count > 1 ? urls[1]?.GetValue<string>() : null;
            upload = urls.Count > 2 ? urls[2]?.GetValue<string>() : null;
        }

        return new FileTagInfo(tag, name, mime, b64, thumb, download, upload);
    }

    private static byte[]? TryDecodeBlob(string? b64)
    {
        if (string.IsNullOrEmpty(b64))
            return null;
        try
        {
            return Convert.FromBase64String(b64);
        }
        catch (FormatException)
        {
            return null;
        }
    }

    /// <summary>Strips characters Windows forbids in file names.</summary>
    internal static string Sanitize(string name)
    {
        foreach (char c in Path.GetInvalidFileNameChars())
            name = name.Replace(c, '_');
        name = name.Trim();
        return name.Length == 0 ? "gemini_output" : name;
    }

    private static string UniqueName(string folder, string fileName)
    {
        string candidate = fileName;
        string stem = Path.GetFileNameWithoutExtension(fileName);
        string ext = Path.GetExtension(fileName);
        for (int i = 1; File.Exists(Path.Combine(folder, candidate)) && i < 1000; i++)
            candidate = $"{stem} ({i}){ext}";
        return candidate;
    }

    /// <summary>Mime type by extension (AOT-safe: no registry lookup).</summary>
    internal static string GuessMime(string fileName)
    {
        return Path.GetExtension(fileName).ToLowerInvariant() switch
        {
            ".txt" or ".md" or ".log" => "text/plain",
            ".csv" => "text/csv",
            ".html" or ".htm" => "text/html",
            ".json" => "application/json",
            ".pdf" => "application/pdf",
            ".png" => "image/png",
            ".jpg" or ".jpeg" => "image/jpeg",
            ".gif" => "image/gif",
            ".webp" => "image/webp",
            ".bmp" => "image/bmp",
            ".svg" => "image/svg+xml",
            ".mp4" => "video/mp4",
            ".mov" => "video/quicktime",
            ".webm" => "video/webm",
            ".mkv" => "video/x-matroska",
            ".avi" => "video/x-msvideo",
            ".mp3" => "audio/mpeg",
            ".wav" => "audio/wav",
            ".m4a" => "audio/mp4",
            ".ogg" => "audio/ogg",
            ".xlsx" => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls" => "application/vnd.ms-excel",
            ".docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".py" or ".cs" or ".js" or ".ts" or ".java" => "text/x-script",
            _ => "application/octet-stream",
        };
    }

    private static string Truncate(string s, int max) =>
        s.Length <= max ? s : s[..max] + "…";

    /// <summary>
    /// Builds a curl_mime-style multipart body: bare boundary, one <c>file</c> part.
    /// </summary>
    private static byte[] BuildMultipartBody(string boundary, string fileName, string mime, byte[] bytes)
    {
        string header =
            $"--{boundary}\r\n" +
            $"Content-Disposition: form-data; name=\"file\"; filename=\"{fileName}\"\r\n" +
            $"Content-Type: {mime}\r\n\r\n";
        string footer = $"\r\n--{boundary}--\r\n";

        byte[] head = System.Text.Encoding.UTF8.GetBytes(header);
        byte[] tail = System.Text.Encoding.UTF8.GetBytes(footer);
        var body = new byte[head.Length + bytes.Length + tail.Length];
        Buffer.BlockCopy(head, 0, body, 0, head.Length);
        Buffer.BlockCopy(bytes, 0, body, head.Length, bytes.Length);
        Buffer.BlockCopy(tail, 0, body, head.Length + bytes.Length, tail.Length);
        return body;
    }

    private sealed record FileTagInfo(
        string Tag,
        string? FileName,
        string? MimeType,
        string? BlobB64,
        string? ThumbUrl,
        string? DownloadUrl,
        string? UploadUrl);
}

/// <summary>A file Gemini generated during a chat turn.</summary>
/// <param name="FileName">The file name reported by Gemini.</param>
/// <param name="SavedPath">Where the file was written, or <c>null</c> when no downloads folder is set.</param>
/// <param name="Size">The file size in bytes.</param>
/// <param name="MimeType">The mime type.</param>
/// <param name="DownloadUrl">The Google download URL, when one was served.</param>
/// <param name="Source">How the bytes were obtained: <c>blob</c> or <c>url</c>.</param>
internal sealed record GeneratedFile(
    string FileName,
    string? SavedPath,
    long Size,
    string MimeType,
    string? DownloadUrl,
    string Source);
