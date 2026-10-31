using System.Collections.Concurrent;
using System.Reflection;
using System.Runtime.CompilerServices;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Media.Imaging;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using MFAAvalonia.Helper.Converters;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.ViewModels.Windows;
using Newtonsoft.Json;

// Exercise the actual built host generator, without constructing the app,
// queue, controller or processor. Collaborators are inert memory objects; the
// host's static toolkit initialization is confined to this test's output root.
internal static class Program
{
    private static T Empty<T>() => (T)RuntimeHelpers.GetUninitializedObject(typeof(T));
    private static void Set(object target, string field, object? value) => target.GetType()
        .GetField(field, BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(target, value);
    private static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    [STAThread]
    public static int Main(string[] args)
    {
        try { Run(args); return 0; }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }
    private static void Run(string[] args)
    {
        AppBuilder.Configure<TestApp>().UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false }).SetupWithoutStarting();
        Directory.CreateDirectory(Path.Combine(AppContext.BaseDirectory,"plugins"));
        var root = Empty<RootViewModel>(); Set(root, "_idle", true);
        var services = (ConcurrentDictionary<Type,Lazy<object>>)typeof(Instances)
            .GetField("ServiceCache", BindingFlags.NonPublic | BindingFlags.Static)!.GetValue(null)!;
        services[typeof(RootViewModel)] = new Lazy<object>(() => root);
        var viewModel = Empty<TaskQueueViewModel>(); Set(viewModel, "_processorField", Empty<MaaProcessor>());
        var source = JsonConvert.DeserializeObject<MaaInterface>(File.ReadAllText(args[0]),
            new MaaInterfaceSelectAdvancedConverter(false), new MaaInterfaceSelectOptionConverter(false))!;
        foreach (var pair in source.Option!) pair.Value.Name = pair.Key;
        // Bypass the production setter's metadata/language/background services.
        typeof(MaaProcessor).GetField("<Interface>k__BackingField", BindingFlags.NonPublic | BindingFlags.Static)!.SetValue(null, source);
        var saves=0;
        var generator = new TaskOptionGenerator(viewModel, () => saves++);
        var task = new MaaInterface.MaaInterfaceTask { Entry="Counter", Name="测试任务", Option =
        [new() { Name="挂机模式", Index=0 }, new() { Name="刷取次数", Data=new() { ["刷取次数"]="7" } },
         new() { Name="战斗录制内容", Index=1 }, new() { Name="失败自动重试", Index=0 }] };
        var item=Empty<DragItemViewModel>(); Set(item,"_interfaceItem",task);
        var panel=new StackPanel();
        generator.GenerateCommonPanelContent(panel,item);
        var window=new Window { Content=new ScrollViewer { Content=panel }, Width=450,Height=700 };
        window.Show(); Draw(window,"task-options-finite.png");
        Check(panel.GetVisualDescendants().OfType<TextBlock>().Count(label=>label.Text=="完成轮数")==1,"Completion count must have exactly one visible label");
        var afk=panel.GetVisualDescendants().OfType<CheckBox>().Single();
        var count=panel.GetVisualDescendants().OfType<TextBox>().Single(input=>input.Text=="7");
        afk.IsChecked=true; Dispatcher.UIThread.RunJobs();
        Check(!count.IsEffectivelyEnabled && task.Option[0].Index==1 && count.Text=="7","AFK disables saved count without losing it");
        Draw(window,"task-options-afk.png");
        afk.IsChecked=false; Dispatcher.UIThread.RunJobs();
        Check(count.IsEffectivelyEnabled && count.Text=="7","Disabling AFK restores the finite count");
        task.Option[1].Data!["刷取次数"]="-1";
        panel.Children.Clear(); generator.GenerateCommonPanelContent(panel,item); Draw(window);
        Check(task.Option[0].Index==1 && task.Option[1].Data!["刷取次数"]=="1","Legacy -1 is migrated to AFK with a usable finite value");
        task.Option[0].Index=0;
        task.Option[3].Index=1;
        task.Option[3].SubOptions=[new() { Name="失败重试设置", Data=new() { ["失败重试次数"]="0" } }];
        panel.Children.Clear(); generator.GenerateCommonPanelContent(panel,item); Draw(window);
        Check(task.Option[3].Index==0 && task.Option[3].SubOptions![0].Data!["失败重试次数"]=="3",
            "Legacy enabled-plus-zero becomes an explicit disabled retry with a usable limit");
        task.Repeatable=true;task.RepeatCount=-1;task.Option.RemoveAt(1);
        panel.Children.Clear();generator.GenerateCommonPanelContent(panel,item);Draw(window);
        Check(task.Option[0].Index==1 && task.RepeatCount==1,"Host-owned unlimited repeat uses the same AFK migration");
        Check(panel.GetVisualDescendants().OfType<TextBlock>().Count(label=>label.Text=="完成轮数")==1,
            "Host-owned repetition also shows one count label");
        root.Idle=false;Dispatcher.UIThread.RunJobs();
        Check(panel.GetVisualDescendants().OfType<CheckBox>().All(check=>!check.IsEffectivelyEnabled),
            "Idle bindings keep AFK controls locked while tasks run");
        Check(saves>0,"Changes flow through the host save callback");
        window.Close();
        Console.WriteLine("PASS actual built task options: one count label, AFK dependencies, saved values, legacy migration; no app/controllers/processes created.");
    }
    private static void Draw(Window window,string? name=null)
    {
        Dispatcher.UIThread.RunJobs(); window.UpdateLayout();
        using var image=new RenderTargetBitmap(new PixelSize((int)window.Width,(int)window.Height),new Vector(96,96));
        image.Render(window);
        if(name!=null && Environment.GetEnvironmentVariable("TASK_OPTIONS_RENDER_DIR") is {} folder)
        { Directory.CreateDirectory(folder); image.Save(Path.Combine(folder,name)); }
    }
}
internal sealed class TestApp : Application { public override void Initialize() => Styles.Add(new FluentTheme()); }
