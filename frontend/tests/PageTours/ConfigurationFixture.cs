namespace MFAAvalonia.Configuration
{
    public static class ConfigurationKeys { public const string HasCompletedFirstUseTutorial = "UI.HasCompletedFirstUseTutorial"; }
    public static class ConfigurationManager { public static ConfigurationFixture Current { get; set; } = new(); }
    public sealed class ConfigurationFixture
    {
        public Dictionary<string, object> Config { get; set; } = new();
        public string FileName { get; set; } = "fixture";
        public bool ContainsKey(string key) => Config.ContainsKey(key);
        public void SetValue(string key, object value) => Config[key] = value;
    }
}
namespace MFAAvalonia.Helper
{
    public static class LoggerHelper { public static void Warning(string message) { } }
}
