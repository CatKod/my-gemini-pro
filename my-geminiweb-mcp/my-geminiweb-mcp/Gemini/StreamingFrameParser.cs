using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Gemini;

/// <summary>
/// Parse Google's length-prefixed streaming frames ("NNN\n" + payload).
/// </summary>
/// <remarks>
/// The length marker counts <b>UTF-16 code units</b> and the payload starts <b>at</b> the
/// newline character — the newline itself is part of the counted payload. Getting this
/// off-by-one wrong makes every frame unparseable, so the rule is verified against the
/// Python reference implementation (<c>gemini_webapi.utils.parsing.StreamingFrameParser</c>).
/// </remarks>
internal sealed class StreamingFrameParser
{
    private readonly StringBuilder _buffer = new();
    private int? _expectedUnits;
    private int _payloadStart;
    private int _scannedChars;
    private int _scannedUnits;
    private bool _prefixChecked;

    /// <summary>Feeds newly decoded stream text and returns every completed JSON frame.</summary>
    public IReadOnlyList<JsonNode?> Feed(string content)
    {
        if (!string.IsNullOrEmpty(content))
            _buffer.Append(content);

        StripPrefixOnce();

        var frames = new List<JsonNode?>();
        while (_expectedUnits is not null || TryReadLengthMarker())
        {
            if (_expectedUnits is null)
                break;

            ScanAvailablePayload();
            if (_scannedUnits < _expectedUnits)
                break;

            int endPos = _payloadStart + _scannedChars;
            string chunk = _buffer.ToString(_payloadStart, endPos - _payloadStart);
            _buffer.Remove(0, endPos);
            ResetFrameState();

            if (string.IsNullOrWhiteSpace(chunk))
                continue;

            JsonNode? parsed;
            try
            {
                parsed = JsonNode.Parse(chunk);
            }
            catch (JsonException)
            {
                continue;
            }

            if (parsed is JsonArray array)
                frames.AddRange(array);
            else if (parsed is not null)
                frames.Add(parsed);
        }

        return frames;
    }

    /// <summary>Parses any frame left over once the stream has ended.</summary>
    public IReadOnlyList<JsonNode?> Flush() => Feed(string.Empty);

    private void ResetFrameState()
    {
        _expectedUnits = null;
        _payloadStart = 0;
        _scannedChars = 0;
        _scannedUnits = 0;
    }

    private void StripPrefixOnce()
    {
        if (_prefixChecked)
            return;

        const string prefix = ")]}'";
        if (_buffer.Length < prefix.Length && prefix.StartsWith(_buffer.ToString(), StringComparison.Ordinal))
            return;

        if (_buffer.Length >= prefix.Length && _buffer.ToString(0, prefix.Length) == prefix)
            _buffer.Remove(0, prefix.Length);

        _prefixChecked = true;
    }

    private bool TryReadLengthMarker()
    {
        string buf = _buffer.ToString();
        int consumed = 0;
        while (consumed < buf.Length && char.IsWhiteSpace(buf[consumed]))
            consumed++;

        if (consumed > 0)
        {
            _buffer.Remove(0, consumed);
            buf = _buffer.ToString();
        }

        if (buf.Length == 0)
            return false;

        int newline = buf.IndexOf('\n');
        if (newline < 0)
            return false;

        string digits = buf[..newline].Trim();
        if (!int.TryParse(digits, NumberStyles.Integer, CultureInfo.InvariantCulture, out int units))
            return false;

        _expectedUnits = units;
        // The payload starts AT the newline: the newline counts toward the frame length.
        _payloadStart = newline;
        _scannedChars = 0;
        _scannedUnits = 0;
        return true;
    }

    private void ScanAvailablePayload()
    {
        if (_expectedUnits is null)
            return;

        string buf = _buffer.ToString();
        int idx = _payloadStart + _scannedChars;
        while (_scannedUnits < _expectedUnits && idx < buf.Length)
        {
            int units = char.IsHighSurrogate(buf[idx]) && idx + 1 < buf.Length && char.IsLowSurrogate(buf[idx + 1]) ? 2 : 1;
            if (_scannedUnits + units > _expectedUnits)
                break;
            _scannedUnits += units;
            _scannedChars += 1;
            idx++;
        }
    }
}
