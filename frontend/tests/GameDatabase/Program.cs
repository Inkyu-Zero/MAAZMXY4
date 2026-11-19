using System.Diagnostics;
using System.Reflection;
using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Input;
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
        Check(catalog.Entries.Count(e=>e.Behavior!=null)==5,"Exactly five reviewed Boss samples have behavior trees");
        var sword=catalog.Entries.Single(e=>e.Name=="绝仙剑");
        Check(sword.Behavior!.MonsterIds.SequenceEqual(new[]{"3627"}),"Tree scope binds the normal variant rather than every namesake");
        Check(catalog.Search("瞬移判定","Boss").Any(e=>e.Name=="戮仙剑"),"Behavior conditions are searchable");
        ValidateBehaviorFiles(root);
        var boss = catalog.Entries.Single(entry => entry.Name == "九尾天狐");
        Check(GameDatabasePresentation.Facts(boss).All(fact => fact.Label != "编号"), "Player facts exclude technical IDs");
        Check(GameDatabasePresentation.Stat(new("命中","0.03;0.04")) == "3% ～ 4%", "Stat ranges use readable percentages");
        Check(GameDatabasePresentation.Stat(new("攻击","404;520")) == "404 ～ 520", "Stat ranges preserve numeric meaning");
        Check(GameDatabasePresentation.Stat(new("破魔","0.1"))=="10%", "Magic penetration is a percentage");
        Check(GameDatabasePresentation.Stat(new("生命增幅（比例）","0.125"))=="12.5%", "Ratio variants use percent units");
        Check(GameDatabasePresentation.FormatText("魔抗: 0.05;0.07\n魔抗+ 0.007\n防御+ 5.4\n暴击: 4%-6%")
            =="魔抗: 5% ～ 7%\n魔抗+ 0.7%\n防御+ 5.4\n暴击: 4% ～ 6%", "Inline attributes convert ratios once while absolute values stay numeric");
        var recipe=GameDatabasePresentation.Facts(catalog.Search("朱电霹雳棍")[0]).First(f=>f.Label=="制作方法");
        Check(string.Concat(catalog.LinkSpans(recipe).Select(s=>s.Text))==recipe.Value,"Links preserve the complete fact text");
        Check(catalog.LinkSpans(recipe).Any(s=>s.Text=="七彩晶" && catalog.FindLinkEntry(s.Target!)?.Name=="七彩晶"),"Recipes resolve to stable local entries");
        var unsafeFact=new GameFact("用途","禁止链接") { Links=[new("禁止链接","file:///C:/Windows")] };
        Check(catalog.LinkSpans(unsafeFact).All(span=>span.Target==null),"Disallowed links remain harmless text");
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
        window.UpdateLayout();Dispatcher.UIThread.RunJobs();
        var materialLink=view.GetVisualDescendants().OfType<HyperlinkButton>().First(link=>Avalonia.Automation.AutomationProperties.GetName(link)=="七彩晶");
        var linkLabel=(TextBlock)materialLink.Content!;
        var paragraph=materialLink.GetVisualAncestors().OfType<SelectableTextBlock>().First();
        var baseline=linkLabel.TranslatePoint(new Point(0,linkLabel.TextLayout.TextLines[0].Baseline),paragraph)!.Value.Y;
        Check(Math.Abs(baseline-paragraph.TextLayout.TextLines[0].Baseline)<1.1,"Inline link glyphs share the prose baseline");
        materialLink.RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Button.ClickEvent));
        Check(Field<GameDatabaseEntry?>(view,"_currentEntry")?.Name=="七彩晶","Inline hyperlink opens the material without launching a browser");
        Field<Button>(view,"_back").RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Button.ClickEvent));
        Check(Field<GameDatabaseEntry?>(view,"_currentEntry")?.Name=="朱电霹雳棍" && search.Text=="朱电霹雳棍","Back restores the previous entry and preserves search");
        if (renderDirectory!=null)
        {
            Render(window,Path.Combine(renderDirectory,"database-equipment.png"));
        }
        search.Text="绝仙剑";
        PumpUntil(()=>(results.SelectedItem as GameDatabaseEntry)?.Name=="绝仙剑");
        window.Height=900;window.UpdateLayout();Dispatcher.UIThread.RunJobs();
        var tree=view.GetVisualDescendants().OfType<TreeView>().Single(t=>t.Name=="BossBehaviorTree");
        var treeRoots=tree.ItemsSource!.Cast<TreeViewItem>().ToArray();
        Check(treeRoots.Length==2 && treeRoots.All(t=>t.IsExpanded),"Action and judgment roots start open");
        Check(treeRoots[0].ItemsSource!.Cast<TreeViewItem>().Any(t=>Avalonia.Automation.AutomationProperties.GetName(t)?.Contains("不高于 90%") == true),"Conditional healing branch is rendered");
        var judgment=treeRoots[1];judgment.BringIntoView();window.UpdateLayout();Dispatcher.UIThread.RunJobs();
        Check(judgment.Focus(),"Behavior tree supports keyboard focus");
        judgment.IsSelected=true;
        window.KeyPressQwerty(PhysicalKey.ArrowLeft,RawInputModifiers.None);
        window.KeyReleaseQwerty(PhysicalKey.ArrowLeft,RawInputModifiers.None);Dispatcher.UIThread.RunJobs();
        Check(!judgment.IsExpanded,"Keyboard left collapses the selected tree branch");
        window.KeyPressQwerty(PhysicalKey.ArrowRight,RawInputModifiers.None);
        window.KeyReleaseQwerty(PhysicalKey.ArrowRight,RawInputModifiers.None);Dispatcher.UIThread.RunJobs();
        Check(judgment.IsExpanded,"Keyboard right expands the selected tree branch");
        var behaviorCard=Field<StackPanel>(view,"_details").Children.OfType<Border>().First(b=>b.GetVisualDescendants().OfType<TreeView>().Contains(tree));
        Field<ScrollViewer>(view,"_detailScroll").Offset=new Vector(0,behaviorCard.Bounds.Y);
        if (renderDirectory!=null) Render(window,Path.Combine(renderDirectory,"boss-behavior-dark.png"));
        window.Width=640;window.Height=960;Dispatcher.UIThread.RunJobs();window.UpdateLayout();
        var attackBranch=treeRoots[0].ItemsSource!.Cast<TreeViewItem>().First(t=>Avalonia.Automation.AutomationProperties.GetName(t)?.StartsWith("分支计时") == true);
        var visibleTitle=((StackPanel)attackBranch.Header!).Children.OfType<TextBlock>().First();
        var right=visibleTitle.TranslatePoint(new Point(visibleTitle.Bounds.Width,0),tree)!.Value.X;
        Check(right<=tree.Bounds.Width+1,"Long behavior conditions wrap within a narrow tree");
        if (renderDirectory!=null)
        {
            Application.Current!.RequestedThemeVariant=Avalonia.Styling.ThemeVariant.Light;
            Field<ScrollViewer>(view,"_detailScroll").Offset=new Vector(0,behaviorCard.Bounds.Y);
            Render(window,Path.Combine(renderDirectory,"boss-behavior-narrow.png"));
        }
        window.Width=1080;window.Height=760;Dispatcher.UIThread.RunJobs();
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
        Console.WriteLine($"PASS catalogue/search/recipe/skills/behavior evidence/keyboard/narrow layout/retry/paging/disposal: {catalog.Entries.Count} entries; no client, controller or browser opened.");
        return 0;
    }
    private static void ValidateBehaviorFiles(string root)
    {
        var temporary=Path.Combine(Environment.GetEnvironmentVariable("GAME_DATABASE_TEST_DIR") ?? Path.GetTempPath(),"boss-behavior-"+Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(temporary);
        File.Copy(Path.Combine(root,"catalog.json"),Path.Combine(temporary,"catalog.json"));
        Check(GameDatabaseCatalog.Load(temporary).Entries.All(e=>e.Behavior==null),"Older catalogue-only packages remain readable");
        var original=File.ReadAllText(Path.Combine(root,"boss-behaviors.json"));
        void Reject(Action<JsonNode> change,string message)
        {
            var doc=JsonNode.Parse(original)!;change(doc);
            File.WriteAllText(Path.Combine(temporary,"boss-behaviors.json"),doc.ToJsonString());
            try { GameDatabaseCatalog.Load(temporary);throw new Exception("Expected rejection: "+message); }
            catch (InvalidDataException) { }
        }
        Reject(doc=>doc["profiles"]![0]!["roots"]![0]!["evidence_ids"]=new JsonArray("missing-evidence"),"dangling evidence");
        Reject(doc=>doc["profiles"]![0]!["monster_ids"]=new JsonArray("90097-wrong-variant"),"wrong variant");
        Reject(doc=>doc["evidence"]![0]!["url"]="file:///C:/Windows","unsafe evidence URL");
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
