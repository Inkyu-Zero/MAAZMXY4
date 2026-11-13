using System.Text.Json.Nodes;
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Polygon = Avalonia.Controls.Shapes.Polygon;
using Avalonia.Headless;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Themes.Fluent;
using Avalonia.Styling;
using Avalonia.Threading;
using MFAAvalonia.Configuration;
using MFAAvalonia.Services;
using MFAAvalonia.Views.UserControls;

internal static class Program
{
    [STAThread]
    public static int Main()
    {
        try { AppBuilder.Configure<TestApplication>().UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false }).SetupWithoutStarting(); }
        catch (Exception error) { Console.WriteLine("FAIL headless setup: " + error); return 1; }
        Action[] tests = [ProgressCompatibility, StableResumeAndUnknownFields, MalformedRecordsAndWriteFailure, FreshInstallEvidence,
            ConfigScopeFence, PageLifecycleAndInstanceScope, BusyVisitAndMidTour, StepButtonsAreExplanations,
            MissingTargetsAndNarrowLayout, PointerAndKeyboardPassThrough, PendingReplayAndUnload, ReplayOnlySelectedPage, BusyBeforeDeferredFocus, UserNavigationCancelsReplay, ThemeAndScaledTargets];
        var failures = 0;
        foreach (var test in tests)
            try { test(); Console.WriteLine("PASS " + test.Method.Name); }
            catch (Exception error) { failures++; Console.WriteLine("FAIL " + test.Method.Name + ": " + error); }
        Console.WriteLine($"{tests.Length - failures}/{tests.Length} onboarding checks passed; headless UI and fixture configuration only.");
        return failures == 0 ? 0 : 1;
    }

    private static readonly string[] Steps = ["one", "two", "three"];
    private static PageTourProgress Progress(string? json = null, bool fresh = true, bool legacy = false, Action<string>? save = null) =>
        new(json, fresh, legacy, save ?? (_ => { }), _ => { });
    private static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static void ProgressCompatibility()
    {
        Check(Progress().StartIndex("task_page", Steps, false) == 0, "New installation starts independently");
        Check(Progress(fresh: false).StartIndex("task_page", Steps, false) == null, "Existing configuration must not auto-play");
        Check(Progress(legacy: true).StartIndex("task_page", Steps, false) == null, "Even a false legacy key means existing configuration");
        Check(Progress(fresh: false).StartIndex("task_page", Steps, true) == 0, "Existing users retain replay");
    }
    private static void StableResumeAndUnknownFields()
    {
        var json = "";
        var progress = Progress("""{"version":1,"automatic":true,"extra":{"keep":7},"pages":{"task_page":{"version":1,"status":"reading","step":"two","future":9},"script_library":{"version":1,"status":"skipped","step":"one"}}}""", save: s => json = s);
        Check(progress.StartIndex("task_page", ["two", "one", "three"], false) == 0, "Reordered steps resume by ID");
        Check(progress.StartIndex("task_page", ["one", "three"], false) == 0, "Removed step falls back to start");
        progress.Record("task_page", "three", "completed");
        var restored = Progress(json, fresh: false);
        Check(restored.StartIndex("task_page", Steps, false) == null && restored.StartIndex("script_library", Steps, false) == null, "Completed and skipped pages survive restart");
        Check(restored.StartIndex("timeline_editor", Steps, false) == 0, "Other pages stay new");
        var node = JsonNode.Parse(json)!;
        Check((int)node["extra"]!["keep"]! == 7 && (int)node["pages"]!["task_page"]!["future"]! == 9, "Unknown fields retained");
        Check(restored.StartIndex("task_page", Steps, true) == 0, "Replay begins at first step");
    }
    private static void MalformedRecordsAndWriteFailure()
    {
        foreach (var json in new[] { "broken", "[]", """{"version":2,"pages":{}}""", """{"version":1,"automatic":[],"pages":{}}""" })
        {
            var writes = 0;
            var progress = Progress(json, save: _ => writes++);
            progress.Initialize(); progress.Record("task_page", "one", "completed");
            Check(writes == 0 && !progress.Automatic, "Unrecognized state must not be overwritten");
            Check(progress.StartIndex("task_page", Steps, true) == 0, "Broken state still permits a manual explanation");
        }
        var brokenPage = Progress("""{"version":1,"automatic":true,"pages":{"task_page":42}}""");
        Check(brokenPage.StartIndex("task_page", Steps, false) == null, "Malformed page is not a new page");
        var failures = 0;
        var failedSave = new PageTourProgress(null, true, false, _ => throw new IOException("fixture"), _ => failures++);
        failedSave.Initialize(); failedSave.Record("task_page", "one", "reading");
        Check(failures == 2, "Persistence failure remains advisory");
    }
    private static void FreshInstallEvidence()
    {
        var directory = Path.Combine(Path.GetTempPath(), "page-tour-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var global = Path.Combine(directory, "appsettings.json");
        var existing = Path.Combine(directory, "existing.json");
        try
        {
            Check(PageTourProgress.IsFreshConfiguration(directory, global), "Empty location is new");
            PageTourConfiguration.CaptureFirstRun(directory, global);
            File.WriteAllText(existing, "{}");
            Check(!PageTourProgress.IsFreshConfiguration(directory, global), "Any existing config suppresses automatic upgrade prompts");
            File.Delete(existing); File.WriteAllText(global, "{}");
            Check(!PageTourProgress.IsFreshConfiguration(directory, global), "Global configuration also means existing installation");
        }
        finally { File.Delete(existing); File.Delete(global); Directory.Delete(directory); }
    }
    private static void ConfigScopeFence()
    {
        var config = new ConfigurationFixture(); ConfigurationManager.Current = config;
        config.Config["business"] = "unchanged";
        config.Config[ConfigurationKeys.HasCompletedFirstUseTutorial] = false;
        var first = PageTourConfiguration.Current();
        first.Record("task_page", "one", "completed");
        var otherInstance = PageTourConfiguration.Current();
        Check(otherInstance.StartIndex("task_page", Steps, false) == null, "Instance changes share assistant configuration");
        Check((string)config.Config["business"] == "unchanged" && !(bool)config.Config[ConfigurationKeys.HasCompletedFirstUseTutorial], "Legacy and business keys unchanged");
        config.Config = new Dictionary<string, object>();
        first.Record("task_page", "two", "reading");
        Check(config.Config.Count == 0, "Old callbacks cannot write into a new configuration");
        var newUser = PageTourConfiguration.Current();
        var saved = (string)config.Config[PageTourProgress.Key];
        Check(Progress(saved, fresh: false).StartIndex("task_page", Steps, false) == 0, "First busy startup saves eligibility for later visits and restarts without completing a page");
        ConfigurationManager.Current = new ConfigurationFixture();
        newUser.Record("task_page", "one", "completed");
        Check((string)config.Config[PageTourProgress.Key] == saved, "An old scope cannot write after switching configurations");
    }

    private sealed class Fixture : IDisposable
    {
        public readonly Grid Root = new();
        public readonly Border Page = new() { Background = Brushes.White };
        public readonly Button Business = new() { Content = "Business", Width = 130, Height = 36, HorizontalAlignment = Avalonia.Layout.HorizontalAlignment.Left, VerticalAlignment = Avalonia.Layout.VerticalAlignment.Top, Margin = new Thickness(40) };
        public readonly TeachingTipOverlay Overlay = new();
        public readonly Window Window;
        public readonly PageTourHost Host;
        public string? PageId = "task_page";
        public object Instance = new(), Scope = new();
        public bool Busy;
        public string? Json;
        public int Actions;
        public Fixture(bool busy = false)
        {
            Busy = busy; Page.Child = Business; Root.Children.Add(Page); Root.Children.Add(Overlay);
            Business.Click += (_, _) => Actions++;
            Window = new Window { Content = Root, Width = 1000, Height = 680 };
            Window.Show(); Draw(Window);
            Host = new PageTourHost(Root, Overlay, () => PageId == null ? null : new PageTourPage(PageId, Page, Instance, Scope, PageTourCatalog.Steps(PageId, _ => Business)),
                () => Busy, () => Progress(Json, save: s => Json = s));
            Host.Observe(); Draw(Window);
        }
        public void Click(string name) { Overlay.FindControl<Button>(name)!.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Draw(Window); }
        public void Visit(string? page) { PageId = page; Host.Observe(); Draw(Window); }
        public void Dispose() { Host.Dispose(); Window.Close(); Dispatcher.UIThread.RunJobs(); }
    }
    private static void Draw(Window window)
    {
        Dispatcher.UIThread.RunJobs(); window.UpdateLayout();
        using var bitmap = new RenderTargetBitmap(new PixelSize((int)window.ClientSize.Width, (int)window.ClientSize.Height), new Vector(96, 96));
        bitmap.Render(window);
    }
    private static void Pump(Task task)
    {
        var deadline = DateTime.UtcNow.AddSeconds(3);
        while (!task.IsCompleted && DateTime.UtcNow < deadline) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(10); }
        Check(task.IsCompleted, "Bounded UI operation did not finish"); task.GetAwaiter().GetResult(); Dispatcher.UIThread.RunJobs();
    }
    private static void PageLifecycleAndInstanceScope()
    {
        using var f = new Fixture();
        Check(f.Overlay.IsRunning, "First idle entry auto-starts");
        f.Click("NextButton"); f.Click("NextButton");
        f.Visit(null); Check(!f.Overlay.IsRunning, "Leaving closes the old position");
        f.Visit("task_page"); Check(f.Overlay.CurrentStep == 2, "Interrupted page resumes");
        f.Click("CloseButton"); f.Instance = new object(); f.Host.Observe();
        Check(!f.Overlay.IsRunning, "Skipped state is shared across instances");
        f.Visit("script_library"); Check(f.Overlay.IsRunning && f.Overlay.CurrentStep == 0, "A different page starts independently");
        f.Click("CloseButton"); f.Visit("timeline_editor");
        f.Click("NextButton"); f.Click("NextButton"); f.Click("NextButton");
        f.Visit(null); f.Visit("timeline_editor"); Check(!f.Overlay.IsRunning, "Completed page stays closed");
    }
    private static void BusyVisitAndMidTour()
    {
        using var f = new Fixture(busy: true);
        Check(!f.Overlay.IsRunning && f.Json == null, "Busy entry does not mark a page completed");
        f.Busy = false; f.Host.Observe(); Check(!f.Overlay.IsRunning, "Finishing work does not cause a late popup");
        f.Visit(null); f.Visit("task_page"); Check(f.Overlay.IsRunning, "Next idle visit starts");
        f.Click("NextButton"); f.Busy = true; f.Host.Observe();
        Check(!f.Overlay.IsRunning, "Starting work interrupts the tip");
        f.Busy = false; f.Host.Observe(); Check(!f.Overlay.IsRunning, "No restart after interrupted business");
        f.Visit(null); f.Visit("task_page"); Check(f.Overlay.CurrentStep == 1, "Interrupted step is retained");
    }
    private static void StepButtonsAreExplanations()
    {
        using var f = new Fixture();
        foreach (var page in PageTourCatalog.Pages)
        {
            f.Visit(page.Id);
            var count = PageTourCatalog.Steps(page.Id, _ => null).Count;
            Check(!f.Overlay.FindControl<Button>("PrevButton")!.IsVisible, "First step has no Previous");
            for (var i = 1; i < count; i++) f.Click("NextButton");
            Check((string)f.Overlay.FindControl<Button>("NextButton")!.Content! == "完成", "Last button is Complete");
            f.Click("PrevButton"); Check(f.Overlay.CurrentStep == count - 2, "Previous only changes the current explanation");
            f.Click("NextButton"); f.Click("NextButton"); Check(!f.Overlay.IsRunning, "Complete dismisses only this page");
        }
        Check(f.Actions == 0, "Reading all steps must not click any business control");
    }
    private static void MissingTargetsAndNarrowLayout()
    {
        using var f = new Fixture();
        var arrow = f.Overlay.FindControl<Polygon>("TipArrow")!;
        Check(arrow.IsVisible, "Visible target gets an arrow");
        f.Business.IsVisible = false;
        f.Click("NextButton");
        Check(!arrow.IsVisible && !f.Overlay.FindControl<Border>("OverlayMask")!.IsVisible, "Missing target clears the previous arrow and highlight");
        f.Window.Width = 320; f.Window.Height = 420; Draw(f.Window);
        f.Click("NextButton");
        var tip = f.Overlay.FindControl<Border>("TipBorder")!;
        var next = f.Overlay.FindControl<Button>("NextButton")!;
        var bounds = TeachingTipOverlay.VisibleBounds(tip, f.Root)!.Value;
        Check(bounds.Width >= 280 && bounds.Height == tip.Bounds.Height && bounds.Width == tip.Bounds.Width, "Tip fits narrow window");
        Check(TeachingTipOverlay.VisibleBounds(next, f.Root)?.Height == next.Bounds.Height, "Buttons are not clipped");
        var text = PageTourCatalog.Steps("script_library", _ => null)[2].Description();
        Check(text.Contains("每行"), "Empty library has an explanatory fallback");
        var scroller = new ScrollViewer { Width = 100, Height = 80, Content = new Border { Height = 500, Child = new Button { VerticalAlignment = Avalonia.Layout.VerticalAlignment.Bottom, Height = 30 } } };
        f.Page.Child = scroller; Draw(f.Window);
        Check(TeachingTipOverlay.VisibleBounds((Control)((Border)scroller.Content!).Child!, f.Root) == null, "Offscreen targets do not produce stale arrows");
    }
    private static void PointerAndKeyboardPassThrough()
    {
        using var f = new Fixture();
        var point = f.Business.TranslatePoint(new Point(50, 15), f.Window)!.Value;
        f.Window.MouseDown(point, MouseButton.Left); f.Window.MouseUp(point, MouseButton.Left); Draw(f.Window);
        Check(f.Actions == 1 && !f.Overlay.IsRunning, "Outside controls remain clickable and interrupt the tour");
        f.Visit(null); f.Visit("task_page");
        f.Business.Focus();
        var outsideEsc = new KeyEventArgs { RoutedEvent = InputElement.KeyDownEvent, Key = Key.Escape };
        f.Business.RaiseEvent(outsideEsc);
        Check(!outsideEsc.Handled && !f.Overlay.IsRunning, "Outside Escape is not intercepted");
        f.Visit(null); f.Visit("task_page");
        var next = f.Overlay.FindControl<Button>("NextButton")!; next.Focus();
        var insideEsc = new KeyEventArgs { RoutedEvent = InputElement.KeyDownEvent, Key = Key.Escape };
        next.RaiseEvent(insideEsc);
        Check(insideEsc.Handled && !f.Overlay.IsRunning, "Tip Escape skips the page");
        f.Visit(null); f.Visit("task_page"); Check(!f.Overlay.IsRunning, "Escape skip is persisted");
    }
    private static void PendingReplayAndUnload()
    {
        using var f = new Fixture();
        f.Host.Dispose();
        var done = new TaskCompletionSource<bool>();
        var host = new PageTourHost(f.Root, f.Overlay, () => new PageTourPage("task_page", f.Page, f.Instance, f.Scope, PageTourCatalog.Steps("task_page", _ => f.Business)), () => false, () => Progress());
        var replay = host.ReplayAsync("task_page", _ => done.Task);
        host.Dispose(); done.SetResult(true); Pump(replay);
        Check(!replay.Result && !f.Overlay.IsRunning, "Pending navigation cannot reopen after unload");
        f.Overlay.StartTutorial(PageTourCatalog.Steps("task_page", _ => f.Business));
        f.Root.Children.Remove(f.Overlay); Draw(f.Window); f.Root.Children.Add(f.Overlay); Draw(f.Window);
        Check(!f.Overlay.IsRunning, "Detached positioning callback stays cancelled after reattach");
        f.Overlay.StartTutorial(PageTourCatalog.Steps("task_page", _ => f.Business)); Draw(f.Window);
        f.Click("NextButton"); Check(f.Overlay.CurrentStep == 1, "Repeated attachment does not duplicate button handlers");
    }
    private static void ReplayOnlySelectedPage()
    {
        using var f = new Fixture(); f.Click("CloseButton"); f.Visit("script_library"); f.Click("CloseButton");
        var replay = f.Host.ReplayAsync("task_page", _ => { f.PageId = "task_page"; return Task.FromResult(true); }); Pump(replay);
        Check(replay.Result && f.Overlay.CurrentStep == 0, "Explicit replay begins at step one");
        f.Click("CloseButton"); f.Visit("script_library"); Check(!f.Overlay.IsRunning, "Replay does not reset another page");
        var cancelled = f.Host.ReplayAsync("timeline_editor", _ => Task.FromResult(false)); Pump(cancelled);
        Check(!cancelled.Result && f.PageId == "script_library" && !f.Overlay.IsRunning, "Cancelled dirty-document navigation does not start the destination");
    }

    private static void BusyBeforeDeferredFocus()
    {
        using var f = new Fixture();
        f.Visit(null); f.Business.Focus();
        f.PageId = "task_page"; f.Host.Observe();
        f.Busy = true; Draw(f.Window);
        Check(!f.Overlay.IsRunning && f.Business.IsFocused, "Queued positioning does not steal focus after business starts");
        f.Busy = false; f.Host.Observe(); Check(!f.Overlay.IsRunning, "Deferred focus cancellation does not leave an auto-popup pending");
    }

    private static void UserNavigationCancelsReplay()
    {
        using var f = new Fixture(); f.Click("CloseButton");
        var decision = new TaskCompletionSource<bool>();
        var replay = f.Host.ReplayAsync("timeline_editor", async cancellation =>
        {
            await decision.Task;
            if (cancellation.IsCancellationRequested) return false;
            f.PageId = "timeline_editor"; return true;
        });
        f.Business.RaiseEvent(new KeyEventArgs { RoutedEvent = InputElement.KeyDownEvent, Key = Key.Tab });
        decision.SetResult(true); Pump(replay);
        Check(!replay.Result && f.PageId == "task_page" && !f.Overlay.IsRunning, "New user input cancels an old navigation callback");
    }

    private static void ThemeAndScaledTargets()
    {
        using var f = new Fixture();
        foreach (var theme in new[] { ThemeVariant.Light, ThemeVariant.Dark })
        foreach (var scale in new[] { 1.0, 1.25, 1.5 })
        {
            Application.Current!.RequestedThemeVariant = theme;
            f.Page.RenderTransformOrigin = RelativePoint.TopLeft;
            f.Page.RenderTransform = new ScaleTransform(scale, scale);
            f.Window.Width = 800; f.Window.Height = 600; Draw(f.Window);
            f.Click("NextButton"); f.Click("PrevButton");
            var tip = f.Overlay.FindControl<Border>("TipBorder")!;
            var title = f.Overlay.FindControl<TextBlock>("TipTitle")!;
            var arrow = f.Overlay.FindControl<Polygon>("TipArrow")!;
            Check(arrow.IsVisible && arrow.Fill != null && tip.BorderBrush != null, "The host accent colors render the arrow and highlight");
            Check(tip.Background is ISolidColorBrush background && title.Foreground is ISolidColorBrush foreground && background.Color != foreground.Color,
                "The themed tip has an opaque surface and readable text");
            var bounds = TeachingTipOverlay.VisibleBounds(tip, f.Root)!.Value;
            var target = TeachingTipOverlay.VisibleBounds(f.Business, f.Root)!.Value;
            Check(!bounds.Intersects(target) && Math.Abs(target.Width - f.Business.Bounds.Width * scale) < 1,
                $"Target scaling is reflected in placement: {theme}, {scale}, tip={bounds}, target={target}");
            f.Window.Width = 320; f.Window.Height = 420; Draw(f.Window); f.Click("NextButton");
            var next = f.Overlay.FindControl<Button>("NextButton")!;
            Check(TeachingTipOverlay.VisibleBounds(next, f.Root)?.Height == next.Bounds.Height, "Theme and scale do not clip step controls");
            if (theme == ThemeVariant.Dark && scale == 1.5 && Environment.GetEnvironmentVariable("PAGE_TOUR_RENDER_PATH") is { } path)
            {
                using var bitmap = new RenderTargetBitmap(new PixelSize(320, 420), new Vector(96, 96));
                bitmap.Render(f.Window); bitmap.Save(path);
            }
            f.Click("PrevButton");
        }
    }
}

internal sealed class TestApplication : Application
{
    public override void Initialize()
    {
        Styles.Add(new FluentTheme());
        if (Environment.GetEnvironmentVariable("PAGE_TOUR_THEME_DIR") is { } directory)
        {
            AppDomain.CurrentDomain.AssemblyResolve += (_, args) =>
            {
                var file = Path.Combine(directory, new AssemblyName(args.Name).Name + ".dll");
                return File.Exists(file) ? Assembly.LoadFrom(file) : null;
            };
            foreach (var entry in new[] { ("FluentAvalonia", "FluentAvalonia.Styling.FluentAvaloniaTheme"), ("SukiUI", "SukiUI.SukiTheme") })
            {
                var style = (IStyle)Activator.CreateInstance(Assembly.LoadFrom(Path.Combine(directory, entry.Item1 + ".dll")).GetType(entry.Item2)!)!;
                // Match App.axaml: assigning ThemeColor initializes the Suki accent resources.
                if (style.GetType().GetProperty("ThemeColor") is { } color) color.SetValue(style, Enum.Parse(color.PropertyType, "Blue"));
                Styles.Add(style);
            }
        }
        else
        {
            Resources["SukiPrimaryColor"] = Colors.SteelBlue;
            Resources.ThemeDictionaries[ThemeVariant.Light] = new ResourceDictionary { ["SukiText"] = Colors.Black, ["SukiCardBackground"] = Colors.White };
            Resources.ThemeDictionaries[ThemeVariant.Dark] = new ResourceDictionary { ["SukiText"] = Colors.White, ["SukiCardBackground"] = Color.Parse("#323232") };
        }
    }
}
