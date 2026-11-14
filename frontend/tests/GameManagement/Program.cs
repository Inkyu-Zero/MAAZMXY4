using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Interactivity;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using Avalonia.Media;
using Avalonia.Markup.Xaml.MarkupExtensions;
using System.Reflection;
using MFAAvalonia.Services;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.Views.UserControls;

internal static class Program
{
    public static int Main()
    {
        try { Run(); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
    private static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static void Run()
    {
        StartupAndBindingPolicy();
        var render = Environment.GetEnvironmentVariable("GAME_MANAGEMENT_RENDER_DIR");
        AppBuilder.Configure<TestApp>().UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions
            { UseHeadlessDrawing = render == null }).SetupWithoutStarting();
        var first = new TaskQueueViewModel(); var second = new TaskQueueViewModel();
        var control = new GameManagementView { DataContext = first };
        var window = new Window { Width = 430, Height = 940, Content = control };
        if (Environment.GetEnvironmentVariable("GAME_MANAGEMENT_THEME_DIR") != null)
        {
            window.Bind(Window.BackgroundProperty, new DynamicResourceExtension("SukiCardBackground"));
            window.Bind(Window.ForegroundProperty, new DynamicResourceExtension("SukiText"));
        }
        window.Show(); Dispatcher.UIThread.RunJobs();
        var advanced = control.GetVisualDescendants().OfType<Expander>().Single(e => Equals(e.Header, "高级设置"));
        Require(!advanced.IsExpanded, "Advanced settings start collapsed");
        var actions = new[] { "启动并连接盒子", "重启盒子", "关闭盒子" }
            .Select(label => control.GetVisualDescendants().OfType<Button>().Single(b => Equals(b.Content, label))).ToArray();
        Require(actions.All(b => b.IsEffectivelyVisible), "All three actions are available before expanding settings");
        var visiblePolicies = control.GetVisualDescendants().OfType<CheckBox>().ToArray();
        Require(visiblePolicies.Length == 3 && visiblePolicies.All(c => c.IsEffectivelyVisible && !c.GetVisualAncestors().Contains(advanced)),
            "All three automatic maintenance options remain outside advanced settings");
        Require(actions[0].Background is ISolidColorBrush blue && blue.Color.B > blue.Color.R &&
            actions[0].Foreground is ISolidColorBrush white && white.Color == Colors.White, "Start and connect is blue with white text");
        Require(!control.GetVisualDescendants().OfType<TextBlock>().Any(t => t.IsEffectivelyVisible &&
            (t.Text?.StartsWith("当前绑定：") == true || t.Text?.StartsWith("当前生效：") == true ||
             t.Text?.StartsWith("设置已保存") == true || t.Text?.StartsWith("首次使用可直接") == true)), "Removed explanatory banners stay absent");
        advanced.IsExpanded = true; Dispatcher.UIThread.RunJobs();
        var rows = new AfkOptionControls();
        var count = new NumericUpDown { Value = 37 };
        var row = new StackPanel(); row.Children.Add(count); rows.Add(row);
        rows.SetEnabled(true);
        Require(!row.IsEnabled && count.Value == 37, "AFK disables count without overwriting it");
        var retryRow = new StackPanel(); rows.Add(retryRow);
        Require(!retryRow.IsEnabled, "Late-created retry options remain disabled");
        rows.SetEnabled(false);
        Require(row.IsEnabled && retryRow.IsEnabled && count.Value == 37, "Leaving AFK restores original values");
        var checks = control.GetVisualDescendants().OfType<CheckBox>().ToArray();
        Require(checks.Length == 3 && checks.All(c => c.IsChecked != true), "All three policies default off");
        var texts = control.GetVisualDescendants().OfType<TextBox>().ToArray();
        var minutes = texts.Single(t => t.Width == 80);
        var exe = texts.Single(t => t.Watermark?.Contains("EXE") == true);
        var account = texts.Single(t => t.Watermark?.Contains("账号") == true);
        var role = texts.Single(t => t.Watermark?.Contains("角色") == true);
        var save = control.GetVisualDescendants().OfType<Button>().First(b => Equals(b.Content,"保存设置"));
        checks[1].IsChecked = true; Dispatcher.UIThread.RunJobs();
        Require(checks[1].IsChecked != true && !GameManagementService.For(first.Processor).Options.Enabled,
            "An invalid policy save restores the actual switch state");
        exe.Text = Path.GetFullPath("zmBox.exe"); account.Text = "历史账号甲"; role.Text = "角色甲";
        save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Dispatcher.UIThread.RunJobs();
        checks[1].IsChecked = true; checks[2].IsChecked = true; Dispatcher.UIThread.RunJobs();
        Require(GameManagementService.For(first.Processor).Options.RestartOnMemory
            && GameManagementService.For(first.Processor).Options.RestartOnInterval, "Restart switches save immediately");
        checks[0].IsChecked = true; Dispatcher.UIThread.RunJobs();
        Require(GameManagementService.For(first.Processor).Options.RestartOnFailure, "Failure restart enabled immediately");
        checks[0].IsChecked = false; Dispatcher.UIThread.RunJobs();
        Require(!GameManagementService.For(first.Processor).Options.RestartOnFailure, "Unchecking really disables restart without Save");
        first.Processor.IsTaskRunActive = true;
        checks[1].IsChecked = false; Dispatcher.UIThread.RunJobs();
        Require(checks[1].IsChecked == true && GameManagementService.For(first.Processor).Options.RestartOnMemory,
            "A rejected active-task change cannot look like a disabled saved policy");
        first.Processor.IsTaskRunActive = false;
        minutes.Text = "0"; save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Dispatcher.UIThread.RunJobs();
        Require(first.Processor.Logs.Last().Contains("分钟") && GameManagementService.For(first.Processor).Options.IntervalMinutes == 60,
            "Invalid interval logs and preserves saved options");
        minutes.Text = "60"; save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Dispatcher.UIThread.RunJobs();
        Require(GameManagementService.For(first.Processor).Options.RestartOnMemory, "Save applies memory policy");
        // Unsaved invalid form edits must never prevent disabling a restart policy.
        role.Text = ""; minutes.Text = "invalid";
        var service = GameManagementService.For(first.Processor);
        var saveCalls = service.SaveCalls;
        var savedOptions = service.Options;
        foreach (var button in actions) { button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Dispatcher.UIThread.RunJobs(); }
        Require(service.SaveCalls == saveCalls && service.Options == savedOptions && service.StartCalls == 1
            && service.RestartCalls == 1 && service.CloseCalls == 1, "Actions use saved settings and never submit invalid drafts");
        checks[0].IsChecked = true; Dispatcher.UIThread.RunJobs();
        Require(GameManagementService.For(first.Processor).Options.RoleName == "角色甲", "Policy toggle must not persist a draft login");
        checks[0].IsChecked = false; checks[2].IsChecked = false; Dispatcher.UIThread.RunJobs();
        Require(!GameManagementService.For(first.Processor).Options.RestartOnFailure && !GameManagementService.For(first.Processor).Options.RestartOnInterval,
            "Invalid drafts cannot prevent disabling restart switches");
        role.Text = "角色甲"; minutes.Text = "60"; checks[2].IsChecked = true; Dispatcher.UIThread.RunJobs();
        control.DataContext = second; Dispatcher.UIThread.RunJobs();
        Require(checks.All(c => c.IsChecked != true), "Another instance does not inherit policy");
        control.DataContext = first; Dispatcher.UIThread.RunJobs();
        Require(checks[1].IsChecked == true && checks[2].IsChecked == true && minutes.Text == "60", "Saved instance restores its options");
        if (render != null)
        {
            advanced.IsExpanded = false;
            Directory.CreateDirectory(render);
            Pump();
            using var light = window.CaptureRenderedFrame(); light!.Save(Path.Combine(render,"game-management-light.png"));
            Application.Current!.RequestedThemeVariant = Avalonia.Styling.ThemeVariant.Dark;
            Pump();
            using var dark = window.CaptureRenderedFrame(); dark!.Save(Path.Combine(render,"game-management-dark.png"));
            window.Width = 300; window.Height = 380; Pump();
            var scroll = control.GetVisualDescendants().OfType<ScrollViewer>().First();
            Require(scroll.Extent.Height > scroll.Viewport.Height, "Short cards keep controls scrollable");
            using var compact = window.CaptureRenderedFrame(); compact!.Save(Path.Combine(render,"game-management-compact.png"));
        }
        window.Close();
        Console.WriteLine("PASS game-management controls, validation, instance isolation and compact layout; no game/process/device used.");
    }
    private static void StartupAndBindingPolicy()
    {
        var root = Path.GetFullPath("fixture client 中文");
        var initial = new GameManagementOptions().WithBundledDefault(root);
        Require(initial.UseBundledBox && initial.ExePath == Path.Combine(root, "造梦盒子", "zmBox.exe"), "New instances bind the bundled box");
        initial.Validate(requireTarget: true, requireLogin: false);
        Require(!initial.HasLogin, "First launch needs no account or role");
        var custom = initial with { UseBundledBox = false, ExePath = Path.GetFullPath("custom box.exe") };
        Require(custom.WithBundledDefault(root) == custom, "Existing custom binding is preserved");
        Require(initial.WithBundledDefault(root + " moved").ExePath != initial.ExePath, "Bundled binding follows the installation directory");
        var launches = 0; var logs = new List<string>();
        var once = new BoxStartupAttempt();
        once.Try(() => launches++, () => false, logs.Add);
        once.Try(() => launches++, () => false, logs.Add);
        Require(launches == 1, "Startup attempts once across repeated initialization");
        var running = new BoxStartupAttempt();
        running.Try(() => launches++, () => true, logs.Add);
        Require(launches == 1, "Existing box is not duplicated");
        var failed = new BoxStartupAttempt();
        failed.Try(() => { launches++; throw new IOException("injected"); }, () => false, logs.Add);
        failed.Try(() => launches++, () => false, logs.Add);
        Require(launches == 2 && logs.Last().Contains("injected"), "Launch failure is logged without throwing or retrying");
    }
    private static void Pump()
    {
        var frame = new DispatcherFrame();
        var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
        timer.Tick += (_, _) => { timer.Stop(); frame.Continue=false; };
        timer.Start(); Dispatcher.UIThread.PushFrame(frame);
    }
}
internal sealed class TestApp : Application
{
    public override void Initialize()
    {
        Styles.Add(new FluentTheme());
        if (Environment.GetEnvironmentVariable("GAME_MANAGEMENT_THEME_DIR") is not { } directory) return;
        AppDomain.CurrentDomain.AssemblyResolve += (_, args) =>
        {
            var path = Path.Combine(directory, new AssemblyName(args.Name).Name + ".dll");
            return File.Exists(path) ? Assembly.LoadFrom(path) : null;
        };
        foreach (var entry in new[] { ("FluentAvalonia", "FluentAvalonia.Styling.FluentAvaloniaTheme"), ("SukiUI", "SukiUI.SukiTheme") })
        {
            var style = (Avalonia.Styling.IStyle)Activator.CreateInstance(Assembly.LoadFrom(Path.Combine(directory, entry.Item1 + ".dll")).GetType(entry.Item2)!)!;
            if (style.GetType().GetProperty("ThemeColor") is { } color) color.SetValue(style, Enum.Parse(color.PropertyType, "Blue"));
            Styles.Add(style);
        }
    }
}
