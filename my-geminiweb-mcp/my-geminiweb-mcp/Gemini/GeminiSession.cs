using System.Text.Json.Nodes;

namespace MyGeminiWebMcp.Gemini;

/// <summary>Immutable session tokens scraped from the Gemini web app bootstrap page.</summary>
/// <param name="AccessToken">The <c>SNlM0e</c> value sent as the <c>at</c> form field.</param>
/// <param name="BuildLabel">The <c>cfb2h</c> frontend build label, sent as the <c>bl</c> query parameter.</param>
/// <param name="SessionId">The <c>FdrFJe</c> frontend session id, sent as the <c>f.sid</c> query parameter.</param>
/// <param name="Language">The <c>TuX5cc</c> account language.</param>
/// <param name="PushId">The <c>qKIAYe</c> file-upload push id.</param>
internal sealed record GeminiSession(
    string AccessToken,
    string BuildLabel,
    string SessionId,
    string Language,
    string PushId);
