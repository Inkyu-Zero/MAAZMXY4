namespace MFAAvalonia.Configuration;

// The production partial uses these same members in the pinned host. Never
// resolve AppPaths here: every test must supply a private fixture path.
public static partial class GlobalConfiguration
{
    private static readonly object _fileLock = new();
    public static string FixturePath { get; set; } = "";
    private static string ConfigFilePath => string.IsNullOrEmpty(FixturePath)
        ? throw new InvalidOperationException("A fixture config path is required") : FixturePath;
}
