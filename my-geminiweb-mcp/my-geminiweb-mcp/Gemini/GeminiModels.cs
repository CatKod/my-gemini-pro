namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// Named Gemini models and their JSPB selection headers.
/// </summary>
/// <remarks>
/// Model identity is a per-account server concept: the header carries an internal model id
/// plus a capacity tier and a model number. Names below mirror the Python reference
/// implementation (<c>gemini_webapi.constants.Model</c>), whose ids were captured from live
/// traffic. An unknown value that already looks like a JSPB array (<c>[...]</c>) is passed
/// through untouched, so a future model can be used without a code change.
/// </remarks>
internal static class GeminiModels
{
    private const string HeaderKey = "x-goog-ext-525001261-jspb";

    /// <summary>Resolves a model name (or raw JSPB) to the full header value, or null for default.</summary>
    public static string? ResolveHeader(string? model)
    {
        if (string.IsNullOrWhiteSpace(model)
            || string.Equals(model, "default", StringComparison.OrdinalIgnoreCase))
            return null;

        string name = model.Trim();

        // Already a JSPB array — pass through.
        if (name.StartsWith('['))
            return name;

        return name.ToLowerInvariant() switch
        {
            "gemini-flash" or "flash" => Build("fbb127bbb056c959", "1", 1),
            "gemini-flash-lite" or "flash-lite" or "lite" => Build("cf41b0e0dd7d53e5", "1", 6),
            "gemini-pro" or "pro" => Build("9d8ca3786ebdfbea", "1", 3),
            "gemini-flash-plus" or "flash-plus" => Build("56fdd199312815e2", "4", 1),
            "gemini-flash-lite-plus" or "lite-plus" => Build("8c46e95b1a07cecc", "4", 6),
            "gemini-pro-plus" or "pro-plus" => Build("e6fa609c3fa255c0", "4", 3),
            "gemini-flash-advanced" or "flash-advanced" => Build("56fdd199312815e2", "2", 1),
            "gemini-flash-lite-advanced" or "lite-advanced" => Build("8c46e95b1a07cecc", "2", 6),
            "gemini-pro-advanced" or "pro-advanced" => Build("e6fa609c3fa255c0", "2", 3),
            _ => name.StartsWith('[') ? name : throw new ArgumentException(
                $"Unknown model '{model}'. Use flash, flash-lite, pro (optionally -plus/-advanced), " +
                "'default', or a raw JSPB array.",
                nameof(model)),
        };
    }

    /// <summary>The header key carrying the model selection.</summary>
    public static string Key => HeaderKey;

    private static string Build(string modelId, string capacityTail, int modelNumber) =>
        $"[1,null,null,null,\"{modelId}\",null,null,0,[4,5,6,8],null,null,{capacityTail},null,null,{modelNumber}]";
}
