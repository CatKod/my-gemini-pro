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
var pool = AccountPool.Load(paths.PoolFile);

// The bridge lets a browser extension hand over session cookies. It binds to the loopback
// interface only and requires a shared token, so it is not reachable from the network.
var bridge = new CookieBridge(pool);
try
{
    bridge.Start();
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
