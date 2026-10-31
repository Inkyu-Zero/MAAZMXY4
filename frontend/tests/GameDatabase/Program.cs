using System.Diagnostics;
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Services;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.Views.Pages;

internal static class Program
{
    private static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static T Field<T>(object owner, string name) => (T)owner.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(owner)!;
    private static void PumpUntil(Func<bool> done)
    {
        // RunJobs drains queued work but does not run the headless platform's
        // timer loop. Pump a DispatcherFrame so the real debounce can fire.
        var frame = new DispatcherFrame();
        var elapsed = Stopwatch.StartNew();
        var timer = new DispatcherTimer { Interval=TimeSpan.FromMilliseconds(5) };
        timer.Tick += (_,_) => { if (done() || elapsed.Elapsed > TimeSpan.FromSeconds(10)) frame.Continue=false; };
        timer.Start();
        try { Dispatcher.UIThread.PushFrame(frame); }
        finally { timer.Stop(); }
        Dispatcher.UIThread.RunJobs(); Check(done(), "Headless page operation timed out");
    }
    [STAThread]
    public static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
    private static int Run(string[] args)
    {
        var renderDirectory=Environment.GetEnvironmentVariable("GAME_DATABASE_RENDER_DIR");
        AppBuilder.Configure<TestApplication>().UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing=renderDirectory==null }).SetupWithoutStarting();
        var root = args.Length > 0 ? args[0] : Path.Combine(Environment.CurrentDirectory,"agent","data","game_database");
        var catalog = GameDatabaseCatalog.Load(root);
        var boss = catalog.Entries.Single(entry => entry.Name == "九尾天狐");
        Check(GameDatabasePresentation.Facts(boss).All(fact => fact.Label != "编号"), "Player facts exclude technical IDs");
        Check(GameDatabasePresentation.Stat(new("命中","0.03;0.04")) == "3% ～ 4%", "Stat ranges use readable percentages");
        Check(GameDatabasePresentation.Stat(new("攻击","404;520")) == "404 ～ 520", "Stat ranges preserve numeric meaning");
        Check(catalog.Search("复活8次", "Boss").Any(entry => entry.Name=="九尾天狐"), "Skill descriptions are searchable");
        Check(catalog.Entries.Count > 2000,"Catalogue should contain the audited official indexes");
        Check(catalog.Search("琉璃宫", "Boss").Any(e=>e.Name=="九尾天狐"),"Boss acquisition/location search");
        Check(catalog.Search("朱电霹雳棍")[0].Name=="朱电霹雳棍","Exact name ranks first");
        Check(catalog.Search("七彩晶", "装备").Any(e=>e.Name=="朱电霹雳棍"),"Recipe is searchable");
        Check(catalog.Search("不存在的材料abcdef").Length==0,"Empty search");
        Check(!GameDatabaseCatalog.IsReferenceUrl("file:///C:/Windows") && !GameDatabaseCatalog.IsReferenceUrl("https://news.4399.com.evil.invalid/x"),"Reference URL policy");
        var calls=0;
        var model=new GameDatabaseViewModel(()=> { if (++calls==1) throw new IOException("injected load failure"); return catalog; });
        var view=new GameDatabaseView(model);
        var window=new Window { Width=1080,Height=760,Content=view };
        window.Show();
        PumpUntil(()=>Field<Button>(view,"_retry").IsVisible);
        var retry=Field<Button>(view,"_retry");retry.RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Button.ClickEvent));
        PumpUntil(()=>Field<GameDatabaseCatalog?>(view,"_catalog")!=null);
        Check(calls==2,"Failed initialization must retry");
        var results=Field<ListBox>(view,"_results");Check(results.ItemCount==60,"Result pages stay bounded");
        var search=Field<TextBox>(view,"_search");search.Text="九尾天狐";
        var expected=catalog.Search("九尾天狐");
        Check(expected.Length==6,"Boss query includes its five drop recipes");
        PumpUntil(()=>results.ItemCount==expected.Length);
        Check((results.SelectedItem as GameDatabaseEntry)?.Name=="九尾天狐","Exact Boss result is selected first");
        Check(view.GetVisualDescendants().OfType<SelectableTextBlock>().Any(t=>t.Text?.Contains("破魔珠璃")==true)
            || view.GetVisualDescendants().OfType<TextBlock>().Any(t=>t.Text=="破魔珠璃"),"Boss skills are rendered");
        if (renderDirectory!=null)
        {
            Directory.CreateDirectory(renderDirectory);
            Application.Current!.RequestedThemeVariant=Avalonia.Styling.ThemeVariant.Dark;
            Dispatcher.UIThread.RunJobs();
            Render(window,Path.Combine(renderDirectory,"database-dark.png"));
        }
        search.Text="朱电霹雳棍";
        PumpUntil(()=>(results.SelectedItem as GameDatabaseEntry)?.Name=="朱电霹雳棍");
        Check(Field<Avalonia.Media.Imaging.Bitmap?>(view,"_bitmap")!=null,"Selected equipment decodes its packaged icon");
        if (renderDirectory!=null)
        {
            Render(window,Path.Combine(renderDirectory,"database-equipment.png"));
        }
        search.Text="九尾天狐";
        PumpUntil(()=>(results.SelectedItem as GameDatabaseEntry)?.Name=="九尾天狐");
        window.Width=640;Dispatcher.UIThread.RunJobs();
        Check(Field<Grid>(view,"_body").ColumnDefinitions.Count==1,"Narrow page stacks details below results");
        if (renderDirectory!=null)
        {
            Application.Current!.RequestedThemeVariant=Avalonia.Styling.ThemeVariant.Light;
            Dispatcher.UIThread.RunJobs();
            Render(window,Path.Combine(renderDirectory,"database-narrow.png"));
        }
        // Switching sidebar pages before debounce must apply the pending
        // query on return, while retaining the one loaded data snapshot.
        search.Text="金丝布";Dispatcher.UIThread.RunJobs();
        window.Content=null;Dispatcher.UIThread.RunJobs();
        Check(!Field<DispatcherTimer>(view,"_debounce").IsEnabled,"Hidden page stops debounce");
        window.Content=view;Dispatcher.UIThread.RunJobs();
        Check((results.SelectedItem as GameDatabaseEntry)?.Name=="金丝布","Pending search resumes after reattach");
        search.Text="无此条目abcdef";PumpUntil(()=>results.ItemCount==0);
        Check(!Field<Button>(view,"_next").IsEnabled,"Empty page cannot advance");
        window.Close();
        Check(!Field<DispatcherTimer>(view,"_debounce").IsEnabled,"Detached page stops search timer");
        Check(calls==2,"Search/page operations must reuse the loaded snapshot");
        Console.WriteLine($"PASS catalogue/search/recipe/skills/retry/paging/narrow layout/disposal: {catalog.Entries.Count} entries; no client, controller or browser opened.");
        return 0;
    }
    private static void Render(Window window,string path)
    {
        Dispatcher.UIThread.RunJobs();window.UpdateLayout();
        using var image=new Avalonia.Media.Imaging.RenderTargetBitmap(new PixelSize((int)window.Width,(int)window.Height),new Vector(96,96));
        image.Render(window);image.Save(path);
    }
}
internal sealed class TestApplication : Application
{
    public override void Initialize() => Styles.Add(new FluentTheme());
}
namespace MFAAvalonia.ViewModels.Pages { public abstract class ViewModelBase { } }
