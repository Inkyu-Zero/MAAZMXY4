using System.Diagnostics;
using System.Reflection;
using System.Text.Json.Nodes;
using MFAAvalonia.Services;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        if (args.Length == 2 && args[0] == "--worker")
        {
            var request = Console.ReadLine();
            if (args[1] == "eof") return 0;
            if (args[1] == "invalid") { Console.WriteLine("{invalid"); return 0; }
            var id = JsonNode.Parse(request!)!["id"]!.GetValue<long>();
            Console.WriteLine(new JsonObject { ["id"] = id, ["ok"] = false, ["error"] = "expected validation error" }.ToJsonString());
            request = Console.ReadLine();
            if (request != null && JsonNode.Parse(request)?["id"] is JsonNode next)
                Console.WriteLine(new JsonObject { ["id"] = next.DeepClone(), ["ok"] = true, ["result"] = new JsonObject() }.ToJsonString());
            Console.ReadLine();
            return 0;
        }
        var failed = 0;
        foreach (var mode in new[] { "eof", "invalid", "application-error" })
        {
            try
            {
                var info = new ProcessStartInfo(Environment.ProcessPath!)
                {
                    UseShellExecute = false, CreateNoWindow = true,
                    RedirectStandardInput = true, RedirectStandardOutput = true
                };
                if (string.Equals(Path.GetFileNameWithoutExtension(Environment.ProcessPath), "dotnet", StringComparison.OrdinalIgnoreCase))
                    info.ArgumentList.Add(Assembly.GetExecutingAssembly().Location);
                info.ArgumentList.Add("--worker"); info.ArgumentList.Add(mode);
                using var worker = Process.Start(info)!;
                using var client = new ScriptStudioClient(Path.GetTempPath());
                typeof(ScriptStudioClient).GetField("_process", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(client, worker);
                ScriptStudioGuard.BeginCapture();
                Exception? caught = null;
                try { await client.CallAsync("capture_status"); }
                catch (Exception error) { caught = error; }
                Console.WriteLine($"TRACE mode={mode} error={caught?.GetType().Name} disconnected={client.IsDisconnected} capturing={ScriptStudioGuard.IsCapturing}");
                if (caught == null) throw new Exception("Expected worker error");
                if (mode == "application-error")
                {
                    if (client.IsDisconnected) throw new Exception("Validation must not destroy the session");
                    await client.CallAsync("list");
                }
                else
                {
                    if (!client.IsDisconnected || ScriptStudioGuard.IsCapturing) throw new Exception("Broken transport must disconnect and release capture");
                    try { await client.CallAsync("list"); throw new Exception("Must not silently restart an empty session"); }
                    catch (IOException) { }
                }
                Console.WriteLine("PASS " + mode);
            }
            catch (Exception error) { failed++; Console.WriteLine("FAIL " + mode + ": " + error.Message); }
            finally { ScriptStudioGuard.EndCapture(); }
        }
        return failed == 0 ? 0 : 1;
    }
}

namespace MFAAvalonia.Extensions.MaaFW
{
    public sealed class MaaProcessor
    {
        public static List<MaaProcessor> Processors { get; } = new();
        public bool IsTaskRunActive { get; set; }
    }
}
