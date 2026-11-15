namespace MyGeminiWebMcp;

/// <summary>
/// Resolves the on-disk locations the server reads its configuration from.
/// </summary>
/// <remarks>
/// Everything lives under a single directory so a user can point the server at their own
/// folder with the <c>GEMINIWEB_MCP_HOME</c> environment variable. The default sits under the
/// user's local application data, which keeps credentials out of the repository and out of
/// the current working directory.
/// </remarks>
internal sealed class AppPaths
{
    /// <summary>Environment variable that overrides the home directory.</summary>
    public const string HomeVariable = "GEMINIWEB_MCP_HOME";

    /// <summary>The home directory holding the pool file and the download folder.</summary>
    public string Home { get; }

    /// <summary>The JSON file describing the account pool.</summary>
    public string PoolFile { get; }

    /// <summary>The folder where generated files are written.</summary>
    public string DownloadsFolder { get; }

    private AppPaths(string home)
    {
        Home = home;
        PoolFile = Path.Combine(home, "accounts.json");
        DownloadsFolder = Path.Combine(home, "downloads");
    }

    /// <summary>Resolves the paths, creating the folders when they are missing.</summary>
    public static AppPaths Resolve()
    {
        string home = Environment.GetEnvironmentVariable(HomeVariable) is { Length: > 0 } custom
            ? custom
            : Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "MyGeminiWebMcp");

        Directory.CreateDirectory(home);
        Directory.CreateDirectory(Path.Combine(home, "downloads"));

        return new AppPaths(home);
    }
}
