using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using MyGeminiWebMcp;
using MyGeminiWebMcp.Accounts;
using MyGeminiWebMcp.Bridge;
using MyGeminiWebMcp.Gemini;
using MyGeminiWebMcp.Tools;

var builder = Host.CreateApplicationBuilder(args);

// All logs must go to stderr: stdout carries the MCP protocol messages.
builder.Logging.AddConsole(o => o.LogToStandardErrorThreshold = LogLevel.Trace);

var paths = AppPaths.Resolve();

// A logger that writes to stderr. stdout carries the MCP protocol, so nothing
// may be written there.
ILogger LoggerFor(string category) =>
    LoggerFactory
        .Create(b => b.AddConsole(o => o.LogToStandardErrorThreshold = LogLevel.Trace))
        .CreateLogger(category);

// `--bridge-only` runs just the cookie bridge, without the MCP server.
//
// Why it exists: an MCP server speaks over stdio, so it lives and dies with the client
// that started it — and it shuts down the moment that client's stdin closes. The browser
// extension, on the other hand, needs a bridge that is always there. This mode gives
// the extension a stable counterpart: start it once, and every MCP session finds a
// bridge already listening (on the same port, and with the same persisted token).
if (args.Contains("--bridge-only", StringComparer.OrdinalIgnoreCase))
{
    var bridgeOnlyPool = AccountPool.Load(paths.PoolFile);
    using var bridgeOnly = new CookieBridge(bridgeOnlyPool, paths.BridgeTokenFile, LoggerFor("bridge"));
    try
    {
        bridgeOnly.Start(paths.BridgePort);
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"[geminiweb] cookie bridge could not start: {ex.Message}");
        return 1;
    }

    Console.Error.WriteLine("[geminiweb] cookie bridge endpoint: " + bridgeOnly.Endpoint);
    Console.Error.WriteLine("[geminiweb] cookie bridge token:    " + bridgeOnly.Token);
    Console.Error.WriteLine("[geminiweb] bridge-only mode — press Ctrl+C to stop.");

    await Task.Delay(Timeout.Infinite, CancellationToken.None);
    return 0;
}

var pool = AccountPool.Load(paths.PoolFile);

// The bridge lets a browser extension hand over session cookies. It binds to the loopback
// interface only and requires a shared token, so it is not reachable from the network.
var bridge = new CookieBridge(pool, paths.BridgeTokenFile);
try
{
    bridge.Start(paths.BridgePort);
}
catch (Exception ex)
{
    // A busy port must not stop the server: the chat tools still work without the bridge.
    Console.Error.WriteLine($"[geminiweb] cookie bridge could not start: {ex.Message}");
}

var keepAlive = new SessionKeepAlive(pool);
keepAlive.Start();

builder.Services.AddSingleton(paths);
builder.Services.AddSingleton(pool);
builder.Services.AddSingleton(bridge);
builder.Services.AddSingleton(keepAlive);

builder.Services
    .AddMcpServer()
    .WithStdioServerTransport()
    .WithTools<GeminiTools>();

// The bridge token is what the extension must present. It goes to stderr, never stdout.
Console.Error.WriteLine("[geminiweb] cookie bridge endpoint: " + bridge.Endpoint);
Console.Error.WriteLine("[geminiweb] cookie bridge token:    " + bridge.Token);

try
{
    await builder.Build().RunAsync();
}
finally
{
    keepAlive.Dispose();
    bridge.Dispose();
}

return 0;
