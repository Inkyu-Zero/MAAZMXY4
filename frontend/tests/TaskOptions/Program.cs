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
using MFAAvalonia.Configuration;
using MFAAvalonia.Helper;
using MFAAvalonia.Helper.Converters;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.ViewModels.Windows;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using MFAAvalonia.Services;
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
        VerifyManualTaskLoading(source);
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
        root.Idle=true;
        task = new MaaInterface.MaaInterfaceTask { Entry="GenericScriptPlayback", Name="通用脚本播放",
            Repeatable=true, RepeatCount=7, Option=[new() { Name="持续播放", Index=0 }] };
        Set(item,"_interfaceItem",task);
        panel.Children.Clear();generator.GenerateCommonPanelContent(panel,item);
        window.Width=360;Draw(window,"generic-playback-finite.png");
        Check(panel.GetVisualDescendants().OfType<TextBlock>().Count(label=>label.Text=="播放次数")==1,
            "Pure playback uses a single playback-count label");
        var continuous=panel.GetVisualDescendants().OfType<CheckBox>().Single();
        var repeat=panel.GetVisualDescendants().OfType<NumericUpDown>().Single();
        Check(repeat.Minimum==1 && repeat.Value==7,"Pure playback has a positive finite count");
        continuous.IsChecked=true;Draw(window,"generic-playback-continuous.png");
        Check(!repeat.IsEffectivelyEnabled && task.RepeatCount==7,"Continuous keeps the saved count disabled");
        continuous.IsChecked=false;Dispatcher.UIThread.RunJobs();
        Check(repeat.IsEffectivelyEnabled && repeat.Value==7,"Turning continuous off restores count");
        Check(!panel.GetVisualDescendants().OfType<TextBlock>().Any(label=>label.Text=="战败处理"),
            "Pure playback never offers combat retry settings");
        root.Idle=false;Dispatcher.UIThread.RunJobs();
        Check(!continuous.IsEffectivelyEnabled && !repeat.IsEffectivelyEnabled,"Running locks task options");
        Check(saves>0,"Changes flow through the host save callback");
        var pureProcessor=Empty<MaaProcessor>();Set(pureProcessor,"_inputOnlyPlayback",true);
        Check(pureProcessor.GetTaskerAsync().GetAwaiter().GetResult()==null,"Pure playback cannot initialize a capture controller");
        pureProcessor.MeasureScreencapPerformanceAsync(CancellationToken.None).GetAwaiter().GetResult();
        Check(pureProcessor.PostScreencapPipelined()==null,"Live preview cannot queue screenshots in pure mode");
        Check(pureProcessor.GetBitmapImage()==null && pureProcessor.GetLiveViewCached()==null,"Direct preview also stays empty");
        var iterations=0;
        var finite=new MFATask {Count=3,MaaAction=()=> {iterations++;return Task.FromResult(MaaFramework.Binding.MaaJobStatus.Succeeded);} };
        Check(finite.Run(CancellationToken.None).GetAwaiter().GetResult().Status==MFATask.MFATaskStatus.SUCCEEDED && iterations==3,
            "The existing host scheduler plays the exact finite count without a Maa tasker");
        using var stop=new CancellationTokenSource();iterations=0;
        var unlimited=new MFATask {Count=-1,MaaAction=()=> {if(++iterations==5)stop.Cancel();return Task.FromResult(MaaFramework.Binding.MaaJobStatus.Succeeded);} };
        Check(unlimited.Run(stop.Token).GetAwaiter().GetResult().Status==MFATask.MFATaskStatus.STOPPED && iterations==5 && unlimited.Count==-1,
            "Continuous playback remains unlimited and stops through the host token");
        iterations=0;
        var failed=new MFATask {Count=3,ContinueOnError=false,MaaAction=()=> {iterations++;return Task.FromResult(MaaFramework.Binding.MaaJobStatus.Failed);} };
        Check(failed.Run(CancellationToken.None).GetAwaiter().GetResult().Status==MFATask.MFATaskStatus.FAILED && iterations==1,
            "A failed playback is not counted or retried as success");
        VerifySpeedTaskLifecycle(source,panel,item,generator,window,root);
        window.Close();
        Console.WriteLine("PASS actual built task options: one count label, AFK dependencies, saved values, legacy migration; no app/controllers/processes created.");
    }

    private static void VerifySpeedTaskLifecycle(MaaInterface source,StackPanel panel,DragItemViewModel item,
        TaskOptionGenerator generator,Window window,RootViewModel root)
    {
        var calls=new List<string>();
        var task=new MFATask {Count=3,MaaAction=()=> {calls.Add("play");return Task.FromResult(MaaFramework.Binding.MaaJobStatus.Succeeded);},
            CompletionCleanup=()=> {calls.Add("cleanup");return Task.CompletedTask;} };
        Check(task.Run(CancellationToken.None).GetAwaiter().GetResult().Status==MFATask.MFATaskStatus.SUCCEEDED
            && calls.SequenceEqual(new[]{"play","play","play","cleanup"}),"One speed cleanup follows all host repetitions");
        using var cancellation=new CancellationTokenSource();calls.Clear();
        task.Count=-1;task.MaaAction=()=> {calls.Add("play");cancellation.Cancel();return Task.FromResult(MaaFramework.Binding.MaaJobStatus.Succeeded);};
        Check(task.Run(cancellation.Token).GetAwaiter().GetResult().Status==MFATask.MFATaskStatus.STOPPED
            && calls.SequenceEqual(new[]{"play","cleanup"}),"Manual stop still awaits native reset");
        calls.Clear();task.Count=1;task.MaaAction=()=>Task.FromResult(MaaFramework.Binding.MaaJobStatus.Failed);
        Check(task.Run(CancellationToken.None).GetAwaiter().GetResult().Status==MFATask.MFATaskStatus.FAILED && calls.Count==1,
            "Failed task also releases native speed once");
        task.ContinueOnError=true;task.MaaAction=()=>Task.FromResult(MaaFramework.Binding.MaaJobStatus.Succeeded);
        task.CompletionCleanup=()=>throw new InvalidOperationException("reset not confirmed");
        var failure=task.Run(CancellationToken.None).GetAwaiter().GetResult();
        Check(failure.Status==MFATask.MFATaskStatus.FAILED && !failure.ContinueQueue,"A failed native reset cannot continue the queue");
        var normal=new NativeBoxSpeedSession(1);
        Check(normal.EnsureAsync(Empty<MaaProcessor>(),null,CancellationToken.None).GetAwaiter().GetResult()==null,
            "Default 1x never starts a worker, captures, or reads window state");
        normal.CloseAsync().GetAwaiter().GetResult();
        var remembered=NativeBoxSpeedSession.WithProof("{}",new System.Text.Json.Nodes.JsonObject { ["state"]="ready",["speed"]=2 });
        var cleared=System.Text.Json.Nodes.JsonNode.Parse(NativeBoxSpeedSession.WithProof(remembered,null))!.AsObject();
        var settings=TaskScriptPlan.Parameters(cleared,"脚本任务设置");
        Check(settings["box_speed"]!.GetValue<int>()==1 && settings["box_speed_proof"]==null,
            "A default-speed task clears a previous task's native confirmation");
        var choices=source.Option!["盒子与脚本倍速"].Cases!;
        for(var index=0;index<2;index++)
        {
            var parameters=JsonConvert.SerializeObject(choices[index].PipelineOverride);
            Check(NativeBoxSpeedSession.Read(parameters,"Mainline_001")==index+1
                && NativeBoxSpeedSession.Read(parameters,GenericScriptPlayback.Entry)==index+1,
                "The same option controls farming and generic playback");
        }
        var selection=new MaaInterface.MaaInterfaceTask {Name="speed",Entry=GenericScriptPlayback.Entry,
            Option=[new(){Name="持续播放",Index=0},new(){Name="盒子与脚本倍速",Index=0}]};
        Set(item,"_interfaceItem",selection);panel.Children.Clear();generator.GenerateCommonPanelContent(panel,item);
        window.Width=360;Draw(window,"native-speed-default.png");
        var combo=panel.GetVisualDescendants().OfType<ComboBox>().Single(c=>c.Items.OfType<MaaInterface.MaaInterfaceOptionCase>().Any(v=>v.Name=="2 倍"));
        Check(combo.SelectedIndex==0,"Native speed defaults to 1x in a narrow layout");
        combo.SelectedIndex=1;Dispatcher.UIThread.RunJobs();
        Check(selection.Option[1].Index==1,"2x selection uses the host's saved task option");
        root.Idle=false;Dispatcher.UIThread.RunJobs();
        Check(!combo.IsEffectivelyEnabled,"Running tasks cannot change the selected factor");
        root.Idle=true;
        Console.WriteLine("PASS native-speed options and task-wide cleanup: repetition, cancellation, failure, and failed-reset queue stop.");
    }
    private static void Draw(Window window,string? name=null)
    {
        Dispatcher.UIThread.RunJobs(); window.UpdateLayout();
        using var image=new RenderTargetBitmap(new PixelSize((int)window.Width,(int)window.Height),new Vector(96,96));
        image.Render(window);
        if(name!=null && Environment.GetEnvironmentVariable("TASK_OPTIONS_RENDER_DIR") is {} folder)
        { Directory.CreateDirectory(folder); image.Save(Path.Combine(folder,name)); }
    }

    private static void VerifyManualTaskLoading(MaaInterface source)
    {
        var config = Empty<InstanceConfiguration>();
        Set(config,"_instanceId","manual-task-test");
        Set(config,"_config",new Dictionary<string,object> { [ConfigurationKeys.InstancePresetKey]="" });
        var processor = Empty<MaaProcessor>();
        Set(processor,"<InstanceConfiguration>k__BackingField",config);
        var vm = Empty<TaskQueueViewModel>(); Set(vm,"_processorField",processor);
        var legacy = JsonConvert.DeserializeObject<MaaInterface.MaaInterfaceTask>("{\"name\":\"Legacy\",\"entry\":\"Old\"}")!;
        var manual = JsonConvert.DeserializeObject<MaaInterface.MaaInterfaceTask>("{\"name\":\"Mainline\",\"entry\":\"Stage001\",\"default_add\":false,\"default_check\":false}")!;
        Check(legacy.DefaultAdd!=false && manual.DefaultAdd==false && manual.Clone().DefaultAdd==false,"Clone preserves manual-only metadata and legacy defaults");
        var merged=legacy.Clone(); merged.Merge(manual); merged.Merge(legacy);
        Check(merged.DefaultAdd==false,"Absent metadata in a later resource does not restore auto-add");
        var definitions = new List<MaaInterface.MaaInterfaceTask> { legacy, manual };
        var loader = new TaskLoader(new MaaInterface { Task=definitions },vm);
        var sync = typeof(TaskLoader).GetMethod("SynchronizeTaskItems",BindingFlags.Instance|BindingFlags.NonPublic)!;
        (List<DragItemViewModel> Rows,List<string> History) Refresh(List<string> history,List<DragItemViewModel> rows)
        {
            object[] args = [history,rows,definitions];
            var result = ((List<DragItemViewModel>,List<DragItemViewModel>))sync.Invoke(loader,args)!;
            return (result.Item1,(List<string>)args[0]);
        }
        var fresh = Refresh([],[]);
        Check(fresh.Rows.Count==1 && fresh.Rows[0].InterfaceItem.Name=="Legacy","Fresh queues omit manual-only tasks");
        manual.Check=true;
        var chosen = new DragItemViewModel(manual.Clone()) { OwnerViewModel=vm };
        var restored = Refresh(["Mainline<|||>Stage001"],[chosen]);
        Check(restored.Rows.Contains(chosen) && chosen.InterfaceItem.Check==true,"Explicit + additions and checked state survive reload");
        var removed = Refresh(["Mainline<|||>Stage001"],[]);
        Check(removed.Rows.All(row=>row.InterfaceItem.Name!="Mainline"),"Deleted manual tasks stay deleted");
        Check(definitions.Count==2 && definitions.Any(task=>task.Name=="Mainline"),"Definition stays available to the + task source");
        definitions=source.Task!;
        var actual=new TaskLoader(source,vm);
        object[] actualArgs=[new List<string>(),new List<DragItemViewModel>(),definitions];
        var actualRows=((List<DragItemViewModel>,List<DragItemViewModel>))sync.Invoke(actual,actualArgs)!;
        Check(actualRows.Item1.All(row=>row.InterfaceItem?.DefaultAdd!=false),"Real mainline definitions never populate fresh queues");
        var mainline=definitions.Where(task=>task.Entry?.StartsWith("Mainline_")==true).ToList();
        Check(mainline.Count==62 && mainline.All(task=>task.DefaultAdd==false),"All 62 stages are explicitly add-only");
        using var addDialog=new AddTaskDialogViewModel(null!,definitions.Select(task=>new DragItemViewModel(task.Clone())).ToList(),vm);
        Check(addDialog.Sources.Count(item=>item.Source?.InterfaceItem?.Entry?.StartsWith("Mainline_")==true)==62,
            "The real + dialog offers every mainline task");
        Check(mainline.SelectMany(task=>task.Group??[]).Distinct().Count()==5,"Mainline tasks use five native map groups");
        var selected=mainline.Select(task=>new DragItemViewModel(task.Clone()){OwnerViewModel=vm}).ToList();
        actualArgs=[selected.Select(row=>row.InterfaceItem!.Name+"<|||>"+row.InterfaceItem.Entry).ToList(),selected,definitions];
        var restoredActual=((List<DragItemViewModel>,List<DragItemViewModel>))sync.Invoke(actual,actualArgs)!;
        Check(selected.All(row=>restoredActual.Item1.Contains(row)),"Every explicitly added stage survives reload");
        Console.WriteLine("PASS actual task loader: legacy auto-add, manual-only fresh queues, explicit additions and deleted-task history.");
    }
}
internal sealed class TestApp : Application { public override void Initialize() => Styles.Add(new FluentTheme()); }
