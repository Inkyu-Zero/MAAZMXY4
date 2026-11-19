using System.Diagnostics;
using System.Runtime.InteropServices;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Media.Imaging;
using Avalonia.Media;
using Avalonia.Styling;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Services;
using MFAAvalonia.Views.UserControls;

internal static class Program
{
    private static string _evidence = "";
    private static string _run = "";
    private static int _cases;
    [STAThread] public static int Main(string[] args)
    {
        if (!LogCleanup.IsSupported) { Console.WriteLine("SKIP Windows handle cleanup"); return 0; }
        // Only operate in a caller-supplied evidence directory, never AppPaths or a client root.
        _evidence = Path.GetFullPath(args[0]);
        if (Path.GetFileName(_evidence) != "log-cleanup-fixtures") throw new ArgumentException("Use an isolated fixture directory");
        Directory.CreateDirectory(_evidence);
        _run = Path.Combine(_evidence, "run-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_run);
        Action[] tests = [PreviewAndProtectedData, ActiveAndChangedFiles, ReplacedIdentity, JunctionAndHardLink, CancelAndForeignPlan, EmptyAndReadOnly, UserFlowAndNarrowLayout, DetachedPreview, RunningTaskAndCleanupOwnership, RetentionSwitchLifecycle];
        if (args.Length > 1 && args[1] == "--ui") tests = [UserFlowAndNarrowLayout, DetachedPreview, RunningTaskAndCleanupOwnership, RetentionSwitchLifecycle];
        AppBuilder.Configure<TestApplication>().UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false }).SetupWithoutStarting();
        var failed = 0;
        foreach (var test in tests)
            try { test(); Console.WriteLine("PASS " + test.Method.Name); }
            catch (Exception error) { failed++; Console.WriteLine("FAIL " + test.Method.Name + ": " + error); }
        Console.WriteLine($"{tests.Length - failed}/{tests.Length} checks passed; all files are isolated fixtures.");
        return failed == 0 ? 0 : 1;
    }
    private static string Root() { var path = Path.Combine(_run, (++_cases).ToString()); Directory.CreateDirectory(path); return path; }
    private static string Write(string root, string name, string value = "fixture")
    { var path = Path.Combine(root, name); Directory.CreateDirectory(Path.GetDirectoryName(path)!); File.WriteAllText(path, value); return path; }
    private static void Check(bool value, string message) { if (!value) throw new Exception(message); }
    private static void PreviewAndProtectedData()
    {
        var root = Root(); var logs = new[] { "debug/maafw.bak.2026.log", "debug/box-speed/123/maafw.log", "logs/log-20261115.log" };
        foreach (var path in logs) Write(root, path, "1234567890");
        string[] protectedFiles = ["recordings/mine.log", "config/settings.json", "backup/settings.zip", "debug/task-runs/round.json", "debug/screencap/frame.png", "debug/adventure/collected.jsonl", "debug/exact_cache/result.log", "debug/backup/evidence.log", "debug/notes.txt"];
        foreach (var path in protectedFiles) Write(root, path);
        var cleaner = new LogCleanup(root); var plan = cleaner.Inspect();
        Check(plan.Files == 3 && plan.Bytes == 30 && logs.All(p => File.Exists(Path.Combine(root, p))), "Preview is read-only and scoped");
        var result = cleaner.Clear(plan);
        Check(result.Deleted == 3 && result.Bytes == 30 && result.Skipped == 0, "Counts and released lengths are accurate");
        Check(protectedFiles.All(p => File.ReadAllText(Path.Combine(root, p)) == "fixture"), "User data, state, screenshots and unknown files survive");
    }
    private static void ActiveAndChangedFiles()
    {
        var root = Root(); var active = Write(root, "debug/maafw.log"); var changed = Write(root, "logs/changed.log");
        var cleaner = new LogCleanup(root); var plan = cleaner.Inspect();
        using var writer = new FileStream(active, FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite | FileShare.Delete);
        File.AppendAllText(changed, " changed"); var fresh = Write(root, "debug/new.log");
        var result = cleaner.Clear(plan);
        Check(result.Deleted == 0 && result.Skipped == 2 && File.Exists(fresh), "Open, changed and newly-created logs are preserved");
        writer.WriteByte(65); writer.Flush();
        Check(File.Exists(active), "Active logger remains usable");
    }
    private static void ReplacedIdentity()
    {
        var root = Root(); var path = Write(root, "debug/replaced.log", "old"); var cleaner = new LogCleanup(root);
        var plan = cleaner.Inspect(); var stamp = File.GetLastWriteTimeUtc(path); var created = File.GetCreationTimeUtc(path);
        File.Move(path, path + ".held"); Write(root, "debug/replaced.log", "new");
        File.SetLastWriteTimeUtc(path, stamp); File.SetCreationTimeUtc(path, created);
        var result = cleaner.Clear(plan);
        Check(result.Deleted == 0 && result.Skipped == 1 && File.ReadAllText(path) == "new", "File identity protects replacement even when size and dates match");
    }
    private static void JunctionAndHardLink()
    {
        var root = Root(); var outside = Root(); var target = Write(outside, "private.log");
        Directory.CreateDirectory(Path.Combine(root, "debug"));
        var junction = Path.Combine(root, "debug", "linked");
        var start = new ProcessStartInfo("cmd.exe") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        start.ArgumentList.Add("/c"); start.ArgumentList.Add("mklink"); start.ArgumentList.Add("/J"); start.ArgumentList.Add(junction); start.ArgumentList.Add(outside);
        using (var command = Process.Start(start)!) { command.WaitForExit(); Check(command.ExitCode == 0, "Fixture junction created"); }
        Check(CreateHardLinkW(Path.Combine(root, "debug", "hard.log"), target, IntPtr.Zero), "Fixture hard link created");
        var cleaner = new LogCleanup(root); var plan = cleaner.Inspect();
        Check(plan.Files == 0 && plan.Unavailable >= 2, "Linked paths are not candidates");
        cleaner.Clear(plan); Check(File.ReadAllText(target) == "fixture", "No deletion outside the log roots");
    }
    private static void CancelAndForeignPlan()
    {
        var root = Root(); var path = Write(root, "debug/keep.log"); var cleaner = new LogCleanup(root); var plan = cleaner.Inspect();
        using var cancelled = new CancellationTokenSource(); cancelled.Cancel();
        var result = cleaner.Clear(plan, cancelled.Token);
        Check(result.Cancelled && result.Deleted == 0 && File.Exists(path), "Cancellation before confirmation work deletes nothing");
        var rejected = false;
        try { new LogCleanup(Root()).Clear(plan); } catch (ArgumentException) { rejected = true; }
        Check(rejected && File.Exists(path), "A plan cannot be applied to another root/owner");
    }
    private static void EmptyAndReadOnly()
    {
        var root = Root(); var cleaner = new LogCleanup(root); Check(cleaner.Inspect().Files == 0, "Missing log folders are empty");
        var path = Write(root, "logs/readonly.log"); File.SetAttributes(path, FileAttributes.ReadOnly);
        var result = cleaner.Clear(cleaner.Inspect());
        Check(result.Skipped == 1 && result.Deleted == 0 && File.Exists(path), "Read-only files are reported and preserved");
    }
    private static void Await(Task task)
    {
        var timer = Stopwatch.StartNew();
        while (!task.IsCompleted && timer.Elapsed < TimeSpan.FromSeconds(10)) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(1); }
        Check(task.IsCompleted, "UI work completed"); task.GetAwaiter().GetResult(); Dispatcher.UIThread.RunJobs();
    }
    private static void UserFlowAndNarrowLayout()
    {
        var root = Root(); var path = Write(root, "debug/clear.log"); var view = new LogCleanupView(new LogCleanup(root));
        var window = new Window { Width = 330, Height = 620, Content = new Border { Padding = new Thickness(25), Child = view } }; window.Show(); Dispatcher.UIThread.RunJobs();
        Await(view.PreviewAsync());
        var confirm = view.GetVisualDescendants().OfType<Button>().Single(b => b.Name == "LogCleanupConfirm");
        var status = view.GetVisualDescendants().OfType<TextBlock>().Single(b => b.Name == "LogCleanupStatus");
        Check(confirm.IsVisible && File.Exists(path) && status.Text!.Contains("永久删除"), "Preview requires an explicit confirmation");
        view.CancelPreview(); Await(view.ConfirmAsync()); Check(File.Exists(path), "Cancel invalidates the plan");
        Await(view.PreviewAsync()); confirm.Focus(); Dispatcher.UIThread.RunJobs();
        window.UpdateLayout();
        Check(confirm.IsFocused && view.Bounds.Width > 0 && confirm.Bounds.Right <= view.Bounds.Width && status.Bounds.Width <= view.Bounds.Width, "Buttons are focusable and fit a narrow view");
        // Render the card itself to exclude headless window-decoration offsets.
        view.Background = Brushes.White;
        using (var image = new RenderTargetBitmap(new PixelSize((int)view.Bounds.Width, (int)view.Bounds.Height), new Vector(96, 96)))
        { image.Render(view); image.Save(Path.Combine(_evidence, "narrow.png")); }
        Await(view.ConfirmAsync()); Check(!File.Exists(path) && status.Text!.Contains("已清理 1"), "Confirmed cleanup reports its result");
        window.Close();
    }
    private static void DetachedPreview()
    {
        var root = Root(); var path = Write(root, "logs/keep.log"); var view = new LogCleanupView(new LogCleanup(root));
        var panel = new StackPanel(); panel.Children.Add(view); var window = new Window { Content = panel }; window.Show();
        var preview = view.PreviewAsync(); panel.Children.Remove(view); Await(preview); panel.Children.Add(view); Dispatcher.UIThread.RunJobs();
        Check(!view.GetVisualDescendants().OfType<Button>().Single(b => b.Name == "LogCleanupConfirm").IsVisible && File.Exists(path), "Unloaded preview cannot restore a stale confirmation");
        Await(view.PreviewAsync()); Check(view.GetVisualDescendants().OfType<Button>().Single(b => b.Name == "LogCleanupConfirm").IsVisible, "View remains usable when reopened");
        window.Close();
    }
    private static void RunningTaskAndCleanupOwnership()
    {
        var root = Root(); var path = Write(root, "debug/keep.log"); var view = new LogCleanupView(new LogCleanup(root));
        var window = new Window { Content = view }; window.Show();
        var active = new MFAAvalonia.Extensions.MaaFW.MaaProcessor { IsTaskRunActive = true };
        MFAAvalonia.Extensions.MaaFW.MaaProcessor.Processors.Add(active);
        try
        {
            Await(view.PreviewAsync()); Await(view.ConfirmAsync());
            Check(File.Exists(path) && ScriptStudioGuard.UpdateBlockReason == "", "Running tasks block deletion without stopping them");
            active.IsTaskRunActive = false;
            Await(view.PreviewAsync()); var deleting = view.ConfirmAsync();
            Check(ScriptStudioGuard.UpdateBlockReason.Contains("清理日志"), "Cleanup holds the existing task/capture start gate");
            Await(deleting);
            Check(!File.Exists(path) && ScriptStudioGuard.UpdateBlockReason == "", "Finished cleanup releases its own gate");
        }
        finally { MFAAvalonia.Extensions.MaaFW.MaaProcessor.Processors.Clear(); window.Close(); }
    }
    private static void RetentionSwitchLifecycle()
    {
        LogRetention.Reset();
        var root = Root(); var path = Write(root, "debug/manual.log"); var view = new LogCleanupView(new LogCleanup(root));
        var panel = new StackPanel(); panel.Children.Add(view);
        var window = new Window { Width = 330, Height = 620, Content = new Border { Padding = new Thickness(25), Child = panel } }; window.Show(); Dispatcher.UIThread.RunJobs();
        var toggle = view.GetVisualDescendants().OfType<ToggleSwitch>().Single(t => t.Name == "LogRetentionEnabled");
        var status = view.GetVisualDescendants().OfType<TextBlock>().Single(t => t.Name == "LogRetentionStatus");
        Check(toggle.IsChecked == true && LogRetention.Writes == 0, "Opening settings shows enabled without writing or starting work");
        toggle.Focus(); Dispatcher.UIThread.RunJobs(); window.UpdateLayout();
        Check(toggle.IsFocused && toggle.Bounds.Right <= view.Bounds.Width, "Switch is focusable and fits a 280-pixel card");
        toggle.IsChecked = false; Dispatcher.UIThread.RunJobs();
        Check(!LogRetention.Enabled && LogRetention.Writes == 1, "The switch event saves disabled");
        Await(view.PreviewAsync()); Await(view.ConfirmAsync()); Check(!File.Exists(path), "Manual cleanup remains available with rolling disabled");
        LogRetention.FailSave = true; Await(view.SetRetentionEnabledAsync(true));
        Check(toggle.IsChecked == false && toggle.IsEnabled && status.Text!.Contains("设置未完成"), "Save failure restores the persisted state and explains the error");
        LogRetention.FailSave = false; LogRetention.Pending = new TaskCompletionSource();
        var saving = view.SetRetentionEnabledAsync(true); panel.Children.Remove(view); panel.Children.Add(view); Dispatcher.UIThread.RunJobs();
        Check(!toggle.IsEnabled && LogRetention.Subscribers == 1, "Reattached view cannot submit a second in-flight save or duplicate listeners");
        LogRetention.Pending.SetResult(); Await(saving);
        Check(toggle.IsChecked == true && toggle.IsEnabled && !status.Text!.Contains("正在保存"), "Save completes across a page change without a stale busy state");
        panel.Children.Remove(view); Check(LogRetention.Subscribers == 0, "Detached control releases its global listener");
        Await(LogRetention.SetEnabledAsync(false)); panel.Children.Add(view); Dispatcher.UIThread.RunJobs();
        Check(toggle.IsChecked == false && LogRetention.Subscribers == 1, "Reopening settings refreshes the global saved choice");
        window.Close(); Check(LogRetention.Subscribers == 0, "Closed settings retain no listener"); LogRetention.Reset();
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateHardLinkW(string link, string target, IntPtr security);
}
public sealed class TestApplication : Application
{ public override void Initialize() { RequestedThemeVariant = ThemeVariant.Light; Styles.Add(new FluentTheme()); } }

namespace MFAAvalonia.Helper
{
    public static class AppPaths { public static string DataRoot => throw new InvalidOperationException("Tests must inject a fixture root"); }
    public static class LoggerHelper { public static void Info(string text) => Console.WriteLine(text); public static void Warning(string text) => Console.WriteLine(text); }
}
namespace MFAAvalonia.Extensions.MaaFW
{
    public sealed class MaaProcessor
    { public static List<MaaProcessor> Processors { get; } = new(); public bool IsTaskRunActive { get; set; } }
}
namespace MFAAvalonia.Services
{
    // UI isolation only. Real persistence, restart and worker behavior are
    // exercised by frontend/tests/LogRetention with the production classes.
    public static class LogRetention
    {
        public static bool Enabled { get; private set; } = true;
        public static int Writes { get; private set; }
        public static bool FailSave { get; set; }
        public static TaskCompletionSource? Pending { get; set; }
        public static event Action? SettingChanged;
        public static int Subscribers => SettingChanged?.GetInvocationList().Length ?? 0;
        public static async Task SetEnabledAsync(bool enabled)
        {
            if (Pending != null) await Pending.Task;
            if (FailSave) throw new IOException("Injected preference write failure");
            Enabled = enabled; Writes++; SettingChanged?.Invoke();
        }
        public static void Reset() { Enabled = true; Writes = 0; FailSave = false; Pending = null; }
    }
    public static class ScriptStudioGuard
    {
        public static object SyncRoot { get; } = new();
        public static string UpdateBlockReason { get; set; } = "";
        public static void ThrowIfCapturing() { if (UpdateBlockReason != "") throw new InvalidOperationException(UpdateBlockReason); }
    }
}
