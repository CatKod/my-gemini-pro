using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using MyGeminiWebMcp;
using MyGeminiWebMcp.Accounts;
using MyGeminiWebMcp.Tools;

var builder = Host.CreateApplicationBuilder(args);

// All logs must go to stderr: stdout carries the MCP protocol messages.
builder.Logging.AddConsole(o => o.LogToStandardErrorThreshold = LogLevel.Trace);

var paths = AppPaths.Resolve();
var pool = AccountPool.Load(paths.PoolFile);

builder.Services.AddSingleton(paths);
builder.Services.AddSingleton(pool);

builder.Services
    .AddMcpServer()
    .WithStdioServerTransport()
    .WithTools<GeminiTools>();

await builder.Build().RunAsync();
