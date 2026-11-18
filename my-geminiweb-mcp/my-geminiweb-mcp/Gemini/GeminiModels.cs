using System.Text.Json;
using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// Builds the JSPB model-selection header for a Gemini model.
/// </summary>
/// <remarks>
/// <para>
/// The header shape mirrors the Python reference implementation
/// (<c>gemini_webapi.constants.build_model_header</c>): an 18-slot array whose slot 4 holds
/// the model id, slot 11 the capacity tier and the last slot the model number.
/// </para>
/// <para>
/// The values are not constants. Google renumbers and re-tiers models without notice, so
/// the ids and model numbers come from <see cref="ModelRegistry"/> — a registry that is
/// populated from the account's own RPC response. A hardcoded table goes stale and makes
/// Google open a stream that never answers.
/// </para>
/// </remarks>
internal static class GeminiModels
{
    /// <summary>The header key carrying the model selection.</summary>
    public const string HeaderKey = "x-goog-ext-525001261-jspb";

    /// <summary>
    /// Builds the model-selection header for a discovered model.
    /// </summary>
    /// <param name="model">The model to select.</param>
    /// <returns>The raw JSPB header value.</returns>
    public static string BuildHeader(GeminiModel model)
    {
        // The capacity slot is a plain number, except for the rarely seen field 13, where
        // the tier is written as a two-element "null,<capacity>" pair.
        string capacity = model.CapacityField == 13
            ? $"null,{model.Capacity}"
            : model.Capacity.ToString();

        return $"[1,null,null,null,\"{model.ModelId}\",null,null,0,[4,5,6,8],null,null,{capacity},null,null,{model.ModelNumber}]";
    }

    /// <summary>
    /// The last segment of a JSPB model header: the model number Google also wants in
    /// slot 79 of the request payload.
    /// </summary>
    /// <param name="header">The raw header value.</param>
    /// <returns>The model number, or <c>null</c> when the header does not carry one.</returns>
    public static int? ModelNumberOf(string? header)
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
            // A malformed header simply means "no model number".
        }
        return null;
    }
}
