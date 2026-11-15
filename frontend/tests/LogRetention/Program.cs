using System.Diagnostics;
using System.Reflection;
using MFAAvalonia.Services;
using MFAAvalonia.Configuration;
using System.Text.Json.Nodes;

internal static class Program
{
    private static string _evidence = "";
    public static async Task<int> Main(string[] args)
    {
        if (args[0] == "--disabled-start")
        {
            var root = Path.GetFullPath(args[1]);
            Check(Path.GetFileName(Path.GetDirectoryName(root)) == "retention-fixtures", "Startup probe uses a fixture");
            GlobalConfiguration.FixturePath = Path.Combine(root, "appsettings.json");
            LogRetention.Start(root);
            try
            {
                await Task.Delay(700);
                Check(!LogRetention.Enabled && Directory.GetFiles(Path.Combine(root, "debug"), "*.log").Length == 6,
                    "Persisted disabled starts without reclaiming an oversized backlog");
            }
            finally { LogRetention.Stop(); }
            Console.WriteLine("DISABLED_STARTUP no deletion in a fresh process");
            return 0;
        }
        if (args[0] == "--writer")
        {
            var root = Path.GetFullPath(args[1]);
            for (var index = 0; index < 6; index++)
            { Create(root, $"debug/maafw.bak.{index}.log", 16 * 1024 * 1024, index); await Task.Delay(25); }
            return 0;
        }
        _evidence = Path.GetFullPath(args[0]);
        if (Path.GetFileName(_evidence) != "retention-fixtures") throw new ArgumentException("Only isolated fixtures are allowed");
        Directory.CreateDirectory(_evidence);
        Func<Task>[] tests = [StartupOldestFirst, IncrementalBurst, CrossProcessUpdates, RenameDeleteAndReconcile, LockedAndCurrentStreams, StopAndUnrelatedFiles,
            PreferenceRoundTripAndFailures, PreferenceControlsRetention, DisabledStartupInNewProcess];
        if (args.Length > 1 && args[1] == "--burst") tests = [IncrementalBurst];
        var failed = 0;
        foreach (var test in tests)
            try { await test(); Console.WriteLine("PASS " + test.Method.Name); }
            catch (Exception error) { failed++; Console.WriteLine("FAIL " + test.Method.Name + ": " + error); }
        return failed == 0 ? 0 : 1;
    }
    private static string Root()
    { var root = Path.Combine(_evidence, Guid.NewGuid().ToString("N")); Directory.CreateDirectory(root); return root; }
    private static string Create(string root, string name, long bytes, int age = 0)
    {
        var path = Path.Combine(root, name); Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using (var file = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.ReadWrite)) file.SetLength(bytes);
        File.SetLastWriteTimeUtc(path, DateTime.UtcNow.AddDays(-10).AddMinutes(age)); return path;
    }
    private static void Check(bool value, string message) { if (!value) throw new Exception(message); }
    private static Task PreferenceRoundTripAndFailures()
    {
        var root = Root(); var path = Path.Combine(root, "appsettings.json"); GlobalConfiguration.FixturePath = path;
        Check(GlobalConfiguration.GetLogRetentionEnabled() && !File.Exists(path), "Absent preference defaults on without writing a file");
        var original = "{\"DefaultConfig\":\"Mine\",\"Unknown\":{\"Keep\":17},\"LogRetention.Enabled\":true}";
        File.WriteAllText(path, original);
        GlobalConfiguration.SetLogRetentionEnabled(false);
        var stored = JsonNode.Parse(File.ReadAllText(path))!;
        Check(!GlobalConfiguration.GetLogRetentionEnabled() && (int)stored["Unknown"]!["Keep"]! == 17 && (string)stored["DefaultConfig"]! == "Mine", "False and unrelated nested fields survive reload");
        var backup = Directory.GetFiles(root, "*.bak").Single();
        Check(File.ReadAllText(backup) == original, "Atomic replacement retains original bytes in a unique backup");
        var before = File.ReadAllBytes(path); var failed = false;
        using (var held = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            try { GlobalConfiguration.SetLogRetentionEnabled(true); }
            catch (IOException) { failed = true; }
        }
        Check(failed && before.SequenceEqual(File.ReadAllBytes(path)) && !GlobalConfiguration.GetLogRetentionEnabled(), "Injected replacement denial leaves the saved preference intact");
        Check(Directory.GetFiles(root, "*.tmp").Length == 0, "Failed save cleans only its own temporary file");
        File.WriteAllText(path, "{broken"); failed = false;
        try { GlobalConfiguration.SetLogRetentionEnabled(true); }
        catch (System.Text.Json.JsonException) { failed = true; }
        Check(failed && File.ReadAllText(path) == "{broken", "Malformed config is diagnosed rather than reset");
        return Task.CompletedTask;
    }
    private static async Task PreferenceControlsRetention()
    {
        var root = Root(); GlobalConfiguration.FixturePath = Path.Combine(root, "appsettings.json");
        var paths = Enumerable.Range(0, 6).Select(i => Create(root, $"debug/block-{i}.log", 16 * 1024 * 1024, i)).ToArray();
        LogRetention.Start(root);
        try
        {
            await Until(() => !File.Exists(paths[0]) && !File.Exists(paths[1]));
            Check(LogRetention.Enabled, "Default-on applies at host startup");
            await LogRetention.SetEnabledAsync(false);
            var extra = Create(root, "debug/after-disable.log", 32 * 1024 * 1024, 10);
            await Task.Delay(500);
            Check(paths.Skip(2).All(File.Exists) && File.Exists(extra), "Disable finishes old work and leaves oversized new logs untouched");
            var current = File.ReadAllBytes(GlobalConfiguration.FixturePath);
            using (var held = new FileStream(GlobalConfiguration.FixturePath, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                var failed = false;
                try { await LogRetention.SetEnabledAsync(true); } catch (IOException) { failed = true; }
                Check(failed && !LogRetention.Enabled && paths.Skip(2).All(File.Exists), "Failed setting save does not start retention");
            }
            Check(current.SequenceEqual(File.ReadAllBytes(GlobalConfiguration.FixturePath)), "Failed runtime save keeps configuration bytes");
            await LogRetention.SetEnabledAsync(true);
            await Until(() => !File.Exists(paths[2]) && !File.Exists(paths[3]));
            for (var i = 0; i < 5; i++)
            { await LogRetention.SetEnabledAsync(false); await LogRetention.SetEnabledAsync(true); }
            await LogRetention.SetEnabledAsync(false);
            var final = Create(root, "debug/final-disabled.log", 32 * 1024 * 1024, 12);
            await Task.Delay(500);
            Check(File.Exists(paths[4]) && File.Exists(paths[5]) && File.Exists(extra) && File.Exists(final), "Repeated toggles leave no retired watcher or deletion worker active");
        }
        finally { LogRetention.Stop(); }
        var stopped = false;
        try { await LogRetention.SetEnabledAsync(true); } catch (InvalidOperationException) { stopped = true; }
        Check(stopped && !GlobalConfiguration.GetLogRetentionEnabled(), "Late setting work cannot restart a shutting-down host");
    }
    private static async Task DisabledStartupInNewProcess()
    {
        var root = Root(); GlobalConfiguration.FixturePath = Path.Combine(root, "appsettings.json");
        GlobalConfiguration.SetLogRetentionEnabled(false);
        for (var i = 0; i < 6; i++) Create(root, $"debug/startup-{i}.log", 16 * 1024 * 1024, i);
        var start = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false, CreateNoWindow = true };
        if (string.Equals(Path.GetFileNameWithoutExtension(Environment.ProcessPath), "dotnet", StringComparison.OrdinalIgnoreCase)) start.ArgumentList.Add(Assembly.GetExecutingAssembly().Location);
        start.ArgumentList.Add("--disabled-start"); start.ArgumentList.Add(root);
        using var child = Process.Start(start)!; await child.WaitForExitAsync();
        Check(child.ExitCode == 0 && Directory.GetFiles(Path.Combine(root, "debug"), "*.log").Length == 6, "A restarted process honors disabled before any cleanup");
    }
    private static async Task Until(Func<bool> ready)
    {
        var end = Stopwatch.StartNew();
        while (!ready() && end.Elapsed < TimeSpan.FromSeconds(10)) await Task.Delay(20);
        Check(ready(), "Background reconciliation finished");
    }
    private static async Task StartupOldestFirst()
    {
        var root = Root();
        var paths = Enumerable.Range(0, 6).Select(i => Create(root, $"debug/maafw.bak.{i}.log", 16 * 1024 * 1024, i)).ToArray();
        using var retention = new LogRetention(root);
        await Until(() => retention.Snapshot.Deleted == 2);
        Check(retention.Snapshot.Bytes == LogRetention.WindowBytes && !File.Exists(paths[0]) && !File.Exists(paths[1]) && paths.Skip(2).All(File.Exists), "96 MiB evicts oldest two blocks to 64 MiB");
        retention.Dispose(); await retention.Completion;
    }
    private static async Task IncrementalBurst()
    {
        var root = Root(); var path = Create(root, "logs/log-current.log", 100);
        using var retention = new LogRetention(root, watch: false);
        await Until(() => retention.Snapshot.FullScans == 1);
        var elapsed = Stopwatch.StartNew();
        File.AppendAllText(path, "abc");
        for (var i = 0; i < 100000; i++) retention.NotifyFile(path);
        elapsed.Stop();
        try { await Until(() => retention.Snapshot.Bytes == 103 && retention.Snapshot.Notifications == 100000); }
        catch { Console.WriteLine("BURST_STATE " + retention.Snapshot); throw; }
        await Task.Delay(300); // Drain the final coalesced notification batch.
        var result = retention.Snapshot;
        Check(result.FullScans == 1 && result.MetadataReads < 20, "100k notifications coalesce without repeated directory scans");
        Console.WriteLine($"BURST notifications={result.Notifications} metadata_reads={result.MetadataReads} full_scans={result.FullScans} caller_ms={elapsed.Elapsed.TotalMilliseconds:F2}");
        await Task.Delay(400);
        Check(retention.Snapshot.MetadataReads == result.MetadataReads, "Idle service performs no polling reads");
        retention.Dispose(); await retention.Completion;
    }
    private static async Task CrossProcessUpdates()
    {
        var root = Root(); Directory.CreateDirectory(Path.Combine(root, "debug"));
        using var retention = new LogRetention(root);
        await Until(() => retention.Snapshot.FullScans == 1);
        var start = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false, CreateNoWindow = true };
        if (string.Equals(Path.GetFileNameWithoutExtension(Environment.ProcessPath), "dotnet", StringComparison.OrdinalIgnoreCase)) start.ArgumentList.Add(Assembly.GetExecutingAssembly().Location);
        start.ArgumentList.Add("--writer"); start.ArgumentList.Add(root);
        using var writer = Process.Start(start)!; await writer.WaitForExitAsync(); Check(writer.ExitCode == 0, "Fixture writer completed");
        await Until(() => retention.Snapshot.Deleted >= 2 && retention.Snapshot.Bytes <= LogRetention.WindowBytes);
        Check(retention.Snapshot.FullScans == 1 && retention.Snapshot.MetadataReads > 0, "Other-process log updates refresh only changed paths");
        Console.WriteLine($"CROSS_PROCESS bytes={retention.Snapshot.Bytes} deletes={retention.Snapshot.Deleted} scans={retention.Snapshot.FullScans}");
        retention.Dispose(); await retention.Completion;
    }
    private static async Task RenameDeleteAndReconcile()
    {
        var root = Root(); var path = Create(root, "logs/first.log", 100);
        using var retention = new LogRetention(root);
        await Until(() => retention.Snapshot.Bytes == 100);
        var renamed = Path.Combine(root, "logs/renamed.log"); File.Move(path, renamed);
        await Until(() => retention.Snapshot.Files == 1 && retention.Snapshot.MetadataReads >= 2);
        File.Delete(renamed); await Until(() => retention.Snapshot.Bytes == 0);
        Create(root, "debug/new-folder/new.log", 20);
        await Until(() => retention.Snapshot.Bytes == 20);
        var scans = retention.Snapshot.FullScans;
        retention.Reconcile(); await Until(() => retention.Snapshot.FullScans > scans);
        Check(retention.Snapshot.Files == 1, "Directory changes and watcher overflow recovery rebuild the index");
        retention.Dispose(); await retention.Completion;
    }
    private static async Task LockedAndCurrentStreams()
    {
        var root = Root(); var current = Create(root, "debug/maafw.log", 20); var oldest = Create(root, "debug/maafw.bak.1.log", 20, -1);
        using var locked = new FileStream(oldest, FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite | FileShare.Delete);
        using var retention = new LogRetention(root, 20);
        await Until(() => retention.Snapshot.FullScans == 1);
        Check(File.Exists(oldest) && File.Exists(current), "Open block and current native stream are never truncated");
        locked.Dispose();
        await Until(() => retention.Snapshot.Bytes == 20 && retention.Snapshot.Deleted == 1);
        Check(File.Exists(current), "A bounded retry reclaims the closed block");
        retention.Dispose(); await retention.Completion;
    }
    private static async Task StopAndUnrelatedFiles()
    {
        var root = Root(); var files = new[] { "recordings/user.log", "debug/config/settings.log", "debug/score.json", "debug/screencap/frame.png", "debug/exact_cache/result.log" };
        foreach (var name in files) Create(root, name, 100);
        using var retention = new LogRetention(root, 10);
        await Until(() => retention.Snapshot.FullScans == 1);
        Check(retention.Snapshot.Bytes == 0 && files.All(p => File.Exists(Path.Combine(root, p))), "Only log roots and unprotected .log files count");
        retention.Dispose(); await retention.Completion;
        var untouched = Create(root, "debug/after-stop.log", 100);
        retention.NotifyFile(untouched); await Task.Delay(250);
        Check(File.Exists(untouched), "Stopped owner cannot delete late notifications");
    }
}
