using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// A model the account is actually offered, as reported by Gemini's own RPC.
/// </summary>
/// <remarks>
/// <para>
/// Model identity is a per-account, per-frontend-build concept. The header carries an
/// internal model id, a capacity tier and a model number, and Google re-numbers and
/// re-tiers models without notice: on 2026-11-16 the id behind "Flash" had already been
/// replaced by "Hydrogen" and "Carbon" within a single day. A hardcoded table therefore
/// goes stale and silently breaks — a stale id makes Google open a stream that never
/// produces a reply.
/// </para>
/// <para>
/// The values here are therefore discovered at runtime (see <see cref="ModelRegistry"/>)
/// rather than hardcoded, mirroring the Python reference implementation
/// (<c>gemini_webapi.types.AvailableModel</c>).
/// </para>
/// </remarks>
/// <param name="ModelId">The internal hex id Google uses in the model header.</param>
/// <param name="ModelName">The canonical name, derived from the model's category.</param>
/// <param name="DisplayName">The localised name shown in the Gemini web UI.</param>
/// <param name="Description">The model description, e.g. "3.1 Pro".</param>
/// <param name="Capacity">The capacity tier that goes into the header's capacity slot.</param>
/// <param name="CapacityField">The proto field number the capacity is written to.</param>
/// <param name="ModelNumber">The model number that goes into the header's last slot.</param>
/// <param name="Aliases">Alternative names and identifiers that resolve to this model.</param>
/// <param name="IsSelectable">
/// Whether this account may pick the model. False when the payload does not put the
/// model into any tier the account holds.
/// </param>
/// <param name="CurrentName">
/// The name the current launch lineup uses for this model number, when it differs from
/// the name in the model list.
/// </param>
internal sealed record GeminiModel(
    string ModelId,
    string ModelName,
    string DisplayName,
    string Description,
    int Capacity,
    int CapacityField,
    int ModelNumber,
    IReadOnlyList<string> Aliases,
    bool IsSelectable = true,
    string? CurrentName = null)
{
    /// <summary>The canonical name, or the display name when there is none.</summary>
    public override string ToString() =>
        string.IsNullOrEmpty(ModelName) ? DisplayName : ModelName;
}

/// <summary>
/// The models one account is offered, discovered from Gemini at runtime.
/// </summary>
/// <remarks>
/// <para>
/// The registry is populated from the <c>GET_USER_STATUS</c> RPC, which every Gemini web
/// session already calls. The payload carries the model list plus the account's tier flags,
/// from which the capacity tier is derived — the same way the Python reference
/// implementation derives it (<c>AvailableModel.compute_capacity</c>).
/// </para>
/// <para>
/// Names are not stable either: Google renamed "Flash" to "Hydrogen" and "Flash" to
/// "Carbon" within a day, and the code name of a model ("Carbon") differs from the label
/// in the UI ("4 Carbon"). Lookup therefore accepts several spellings of the same model —
/// the code name, the UI label, the plain label and the hex id — and normalises them
/// before matching.
/// </para>
/// </remarks>
internal sealed class ModelRegistry
{
    /// <summary>The RPC that returns the account's model list.</summary>
    private const string GetUserStatusRpcId = "otAQ7b";

    private readonly List<GeminiModel> _models = [];
    private readonly Dictionary<string, GeminiModel> _byId = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>Every model the account is offered, in Google's own order.</summary>
    public IReadOnlyList<GeminiModel> Models => _models;

    /// <summary>Whether discovery has run and produced at least one model.</summary>
    public bool HasModels => _models.Count > 0;

    /// <summary>
    /// Resolves a user-supplied model name to a discovered model.
    /// </summary>
    /// <param name="name">
    /// A code name (<c>hydrogen</c>), a UI label (<c>4 hydrogen</c>), a description
    /// (<c>3.1 pro</c>), a hex id, or <c>default</c>.
    /// </param>
    /// <returns>The matching model, or <c>null</c> when nothing matches.</returns>
    public GeminiModel? Resolve(string? name)
    {
        if (string.IsNullOrWhiteSpace(name))
            return null;

        string target = Normalize(name);
        if (target.Length == 0 || target == "default")
            return null;

        // Exact id match first: a hex id is unambiguous.
        if (_byId.TryGetValue(target, out var byId))
            return byId;

        foreach (var model in _models)
        {
            if (Normalize(model.ModelName) == target
                || Normalize(model.DisplayName) == target
                || Normalize(model.Description) == target)
            {
                return model;
            }
        }

        // Alias match: the registry derives several spellings per model.
        foreach (var model in _models)
        {
            foreach (var alias in model.Aliases)
            {
                if (Normalize(alias) == target)
                    return model;
            }
        }

        // Last resort: a unique substring match, so "carbon" finds "4 Carbon" even when
        // Google renames the label around it.
        var hits = _models
            .Where(m => Normalize(m.ModelName).Contains(target, StringComparison.Ordinal)
                || Normalize(m.DisplayName).Contains(target, StringComparison.Ordinal)
                || Normalize(m.Description).Contains(target, StringComparison.Ordinal))
            .ToList();
        return hits.Count == 1 ? hits[0] : null;
    }

    /// <summary>
    /// Resolves a name and reports every available name when the lookup fails.
    /// </summary>
    /// <param name="name">The name the caller asked for.</param>
    /// <param name="model">The resolved model, or <c>null</c>.</param>
    /// <returns><c>true</c> when the name resolved.</returns>
    public bool TryResolve(string? name, out GeminiModel? model)
    {
        model = Resolve(name);
        return model is not null;
    }

    /// <summary>A human-readable list of the names this account accepts.</summary>
    public string AvailableNames =>
        _models.Count == 0
            ? "(none discovered yet)"
            : string.Join(", ", _models.Select(m => $"{m.DisplayName} ({m.ModelName})"));

    /// <summary>
    /// The models the account is shown but may not select.
    /// </summary>
    /// <remarks>
    /// These are the models whose tier list is empty for the account: the account sees
    /// them in the picker, but picking one makes Google open a stream that never
    /// answers. Reporting them is what lets the caller fail fast instead of waiting.
    /// </remarks>
    public IReadOnlyList<GeminiModel> UnselectableModels =>
        _models.Where(m => !m.IsSelectable).ToList();

    /// <summary>
    /// Parses the model list out of a <c>GET_USER_STATUS</c> response body.
    /// </summary>
    /// <param name="body">The parsed response body (the outer array of the RPC payload).</param>
    internal void ApplyUserStatus(JsonArray body)
    {
        _models.Clear();
        _byId.Clear();

        if (body.Count <= 15 || body[15] is not JsonArray modelList)
            return;

        var tierFlags = ReadIntList(body.Count > 16 ? body[16] : null);
        var capabilityFlags = ReadIntList(body.Count > 17 ? body[17] : null);
        (int capacity, int capacityField) = ComputeCapacity(tierFlags, capabilityFlags);

        // The current launch lineup names the models separately (slot 24). A model can
        // still be listed under its old name while the lineup already calls it something
        // else, so the lineup is read first and matched by model number.
        var lineup = ReadLineup(body);

        foreach (var entry in modelList)
        {
            if (entry is not JsonArray model || model.Count == 0)
                continue;

            string? modelId = ReadString(model, 0);
            if (string.IsNullOrEmpty(modelId))
                continue;

            string category = ReadString(model, 1) ?? ReadString(model, 10) ?? "";
            string display = ReadString(model, 11)
                ?? ReadString(model, 19)
                ?? ReadString(model, 1)
                ?? "";
            string description = ReadString(model, 12) ?? ReadString(model, 2) ?? "";

            // The model number lives in slot 17; slot 9 is the fallback. Slot 4 carries a
            // per-model capacity that overrides the account-wide tier when present.
            int modelNumber = ReadInt(model, 17) ?? ReadInt(model, 9) ?? 1;
            int modelCapacity = ReadInt(model, 4) ?? capacity;

            // Slot 6 lists the model ids of the tier that may use this model. A null there
            // means the account belongs to no tier that offers the model: the account is
            // shown the model in the list, but cannot select it. Sending its id anyway
            // makes Google open a stream that never answers — verified 2026-11-19: the
            // locked model kept a stream alive for 901 s before the turn budget cut it.
            bool selectable = model.Count > 6 && model[6] is JsonArray;

            bool inLineup = lineup.TryGetValue(modelNumber, out var current);
            string? currentName = inLineup ? current.Description : null;
            string? currentCategory = inLineup ? current.Category : null;

            var parsed = new GeminiModel(
                modelId,
                DeriveName(category, display, modelId),
                display,
                description,
                modelCapacity,
                capacityField,
                modelNumber,
                DeriveAliases(category, display, description, modelId,
                    currentName, currentCategory),
                selectable,
                currentName);

            _models.Add(parsed);
            _byId[modelId] = parsed;
        }
    }

    /// <summary>
    /// Reads the current launch lineup from the status payload.
    /// </summary>
    /// <remarks>
    /// The lineup sits in a policy entry whose second element holds one entry per model.
    /// Each entry carries the model number and the name the lineup uses for it. The
    /// payload carries several policy entries; the one with a model list is the lineup.
    /// </remarks>
    private static Dictionary<int, (string Category, string Description)> ReadLineup(JsonArray body)
    {
        var result = new Dictionary<int, (string, string)>();

        foreach (var slot in body)
        {
            if (slot is not JsonArray policy || policy.Count < 2)
                continue;
            if (policy[0] is not JsonValue name || !name.TryGetValue<string>(out string? policyName))
                continue;
            if (!policyName.Contains("policy", StringComparison.OrdinalIgnoreCase))
                continue;
            if (policy[1] is not JsonArray entries)
                continue;

            foreach (var entry in entries)
            {
                if (entry is not JsonArray modelEntry || modelEntry.Count < 2)
                    continue;
                if (modelEntry[0] is not JsonArray fields || fields.Count < 6)
                    continue;

                int? number = ReadInt(fields, 1);
                string category = ReadString(fields, 4) ?? "";
                string description = ReadString(fields, 5) ?? "";
                if (number is not null && category.Length > 0)
                    result[number.Value] = (category, description);
            }

            if (result.Count > 0)
                break;
        }

        return result;
    }

    /// <summary>
    /// Derives the account's capacity tier from the flags in the status payload.
    /// </summary>
    /// <remarks>
    /// This mirrors <c>gemini_webapi.types.AvailableModel.compute_capacity</c>: the tier
    /// decides which quota bucket the request is billed from, so getting it wrong sends the
    /// turn to the wrong allowance.
    /// </remarks>
    internal static (int Capacity, int CapacityField) ComputeCapacity(
        IReadOnlyList<int> tierFlags, IReadOnlyList<int> capabilityFlags)
    {
        if (tierFlags.Contains(21))
            return (1, 13);
        if (tierFlags.Contains(22))
            return (2, 13);

        if (capabilityFlags.Contains(115))
            return (4, 12);   // Plus accounts.
        if (tierFlags.Contains(16) || capabilityFlags.Contains(106))
            return (3, 12);   // Pro accounts.

        // Advanced accounts report flag 8 in the tier list, flag 19 among the capabilities.
        if (tierFlags.Contains(8) || capabilityFlags.Contains(19))
            return (2, 12);

        return (1, 12);       // Free accounts.
    }

    /// <summary>Builds the canonical model name from the model's category and display label.</summary>
    private static string DeriveName(string category, string display, string modelId)
    {
        if (category.Length > 0)
            return "gemini-" + Slug(category);

        if (display.Length > 0)
            return "gemini-" + Slug(display);

        return "gemini-" + modelId;
    }

    /// <summary>Builds the alternative spellings that resolve to the same model.</summary>
    private static IReadOnlyList<string> DeriveAliases(
        string category,
        string display,
        string description,
        string modelId,
        string? currentName = null,
        string? currentCategory = null)
    {
        var aliases = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { modelId };

        AddSlugAliases(aliases, category);
        AddSlugAliases(aliases, display);
        AddSlugAliases(aliases, description);

        // "4 Hydrogen" also answers to "hydrogen" and "gemini-4-hydrogen".
        if (category.Length > 0)
        {
            string withoutVersion = StripLeadingVersion(category);
            if (withoutVersion.Length > 0 && withoutVersion != category)
                AddSlugAliases(aliases, withoutVersion);
        }

        // The launch lineup may already call the model something else (Carbon became
        // Argon on 2026-11-18). Both spellings have to resolve, otherwise the current
        // name of a model cannot be asked for at all.
        if (currentName is not null)
            AddSlugAliases(aliases, currentName);
        if (currentCategory is not null)
            AddSlugAliases(aliases, currentCategory);

        return aliases.OrderBy(a => a, StringComparer.OrdinalIgnoreCase).ToList();
    }

    private static void AddSlugAliases(HashSet<string> aliases, string label)
    {
        if (label.Length == 0)
            return;

        aliases.Add(label);
        aliases.Add(Slug(label));

        string withoutVersion = StripLeadingVersion(label);
        if (withoutVersion.Length > 0)
        {
            aliases.Add(withoutVersion);
            aliases.Add(Slug(withoutVersion));
        }
    }

    /// <summary>Removes a leading version token such as "4 " or "3.1 " from a label.</summary>
    private static string StripLeadingVersion(string label)
    {
        int i = 0;
        while (i < label.Length && (char.IsDigit(label[i]) || label[i] == '.'))
            i++;

        if (i == 0 || i >= label.Length)
            return "";

        return label[i..].Trim();
    }

    /// <summary>Lowercases a label and turns spaces into dashes.</summary>
    private static string Slug(string label) =>
        label.Trim().ToLowerInvariant().Replace(' ', '-');

    /// <summary>Normalises a name for comparison: lowercase, dashes instead of spaces.</summary>
    private static string Normalize(string name) =>
        name.Trim().ToLowerInvariant().Replace(' ', '-').Replace('_', '-');

    private static string? ReadString(JsonArray array, int index) =>
        index < array.Count
        && array[index] is JsonValue value
        && value.TryGetValue<string>(out string? s)
            ? s
            : null;

    private static int? ReadInt(JsonArray array, int index) =>
        index < array.Count
        && array[index] is JsonValue value
        && value.TryGetValue<int>(out int n)
            ? n
            : null;

    private static IReadOnlyList<int> ReadIntList(JsonNode? node)
    {
        if (node is not JsonArray array)
            return [];

        var result = new List<int>(array.Count);
        foreach (var item in array)
        {
            if (item is JsonValue value && value.TryGetValue<int>(out int n))
                result.Add(n);
        }
        return result;
    }
}
