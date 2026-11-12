using System.Reflection;
using System.Text.Json.Nodes;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Interactivity;
using Avalonia.Input;
using Avalonia.Input.Platform;
using Avalonia.Media.Imaging;
using Avalonia.Media;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Services;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.Views.Pages;
using MFAAvalonia.Controls;

internal static class Program
{
    [STAThread]
    public static int Main()
    {
        AppBuilder.Configure<TestApplication>().UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false }).SetupWithoutStarting();
        var failed = 0;
        var tests = new Action[] { FirstOpenDuringTask, RefreshAndMutationGuard, RetryFailedInitialization, RecordingUsesCurrentTaskController, NoImplicitRecordingTarget, RenameCancelCollisionAndSave, RenameOtherFileKeepsEditorState, DurationTextApplies, DurationTextSavesWithoutExtraApply, TimelineBoxSelectMoveAndCancel, TimelineDocumentChangeCancelsGesture, EditorBatchRequestsAndTaskGuard, DiscardRecordingStatesAndRestart, DiscardStoppedDraftAndProtectSavedScript, DiscardFailureCanRetry, DiscardIgnoresLateStatusAndSaveRequest, TerminalPollLocksUntilDraftIsReady, RecordingActionLayout, RecordingDiscardClickModes, RecordingDoubleClickCancellationAndRetry };
        tests=tests.Concat([TimelineClipboardShortcuts, DialogThemeAndDismissal]).ToArray();
        foreach (var test in tests)
        {
            try { test(); Console.WriteLine("PASS " + test.Method.Name); }
            catch (Exception error) { failed++; Console.WriteLine("FAIL " + test.Method.Name + ": " + error.Message); }
        }
        Console.WriteLine($"{tests.Length-failed}/{tests.Length} page regression checks passed; no real processes, hooks, or game input were used.");
        return failed == 0 ? 0 : 1;
    }

    private static T Field<T>(object target, string name) => (T)target.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(target)!;
    private static StudioOperation Operation(string id, int key, double start, double duration) => new() { Data = new JsonObject
        { ["id"] = id, ["kind"] = "key_hold", ["key"] = key, ["start_ms"] = start, ["duration_ms"] = duration } };
    private static void Draw(Window window, string? filename = null)
    {
        Dispatcher.UIThread.RunJobs(); window.UpdateLayout();
        using var image = new RenderTargetBitmap(new PixelSize((int)Math.Ceiling(window.ClientSize.Width), (int)Math.Ceiling(window.ClientSize.Height)), new Vector(96, 96));
        image.Render(window);
        if (filename != null && Environment.GetEnvironmentVariable("STUDIO_RENDER_DIR") is { } folder)
        { Directory.CreateDirectory(folder); image.Save(Path.Combine(folder, filename)); }
    }
    private static void TimelineBoxSelectMoveAndCancel()
    {
        var timeline = new ScriptTimeline();
        var window = new Window { Content = timeline, Width = 1000, Height = 320 };
        window.Show(); timeline.Update([Operation("a",65,1000,1000),Operation("b",66,1600,600),Operation("c",67,7000,500)],10000);
        timeline.Fit(1000); Draw(window);
        var moves = new List<(string[] Ids,double Delta)>();
        timeline.Moved += (ids, delta) => moves.Add((ids.ToArray(), delta));
        window.MouseDown(new Point(150,38), MouseButton.Left);
        window.MouseMove(new Point(400,130), RawInputModifiers.LeftMouseButton);
        window.MouseUp(new Point(400,130), MouseButton.Left);
        Check(timeline.SelectedIds.ToHashSet().SetEquals(["a","b"]), "Marquee must select across two tracks only");
        Draw(window,"timeline-selection.png");
        window.MouseDown(new Point(240,58), MouseButton.Left);
        window.MouseMove(new Point(326.6,58), RawInputModifiers.LeftMouseButton);
        window.MouseUp(new Point(326.6,58), MouseButton.Left);
        Check(moves.Count==1 && moves[0].Ids.ToHashSet().SetEquals(["a","b"]) && moves[0].Delta==1000, "Drag emits one group translation");
        Draw(window);
        window.MouseDown(new Point(240,58), MouseButton.Left);
        window.MouseMove(new Point(0,58), RawInputModifiers.LeftMouseButton);
        window.MouseUp(new Point(0,58), MouseButton.Left);
        Check(moves[^1].Delta == -1000, "Group clamps as a unit at time zero");
        Draw(window);
        var count=moves.Count;
        window.MouseDown(new Point(240,58), MouseButton.Left);
        window.MouseMove(new Point(300,58), RawInputModifiers.LeftMouseButton);
        window.KeyPressQwerty(PhysicalKey.Escape, RawInputModifiers.None);
        window.MouseUp(new Point(300,58), MouseButton.Left);
        Check(moves.Count==count, "Escape cancels a pending group move");
        timeline.Editable=false; Draw(window);
        window.MouseDown(new Point(240,58), MouseButton.Left);
        window.MouseMove(new Point(340,58), RawInputModifiers.LeftMouseButton);
        window.MouseUp(new Point(340,58), MouseButton.Left);
        Check(moves.Count==count,"Read-only timeline must not edit");
        window.Close();
    }
    private static void TimelineDocumentChangeCancelsGesture()
    {
        var timeline=new ScriptTimeline();
        var window=new Window { Content=timeline,Width=1000,Height=320 };
        var changes=0;
        timeline.Moved+=(_,_)=>changes++; timeline.Edited+=(_,_,_)=>changes++;
        window.Show();
        try
        {
            timeline.Update([Operation("a",65,1000,1000)],10000); timeline.Fit(1000); Draw(window);
            window.MouseDown(new Point(240,58),MouseButton.Left);
            window.MouseMove(new Point(280,58),RawInputModifiers.LeftMouseButton);
            // The same update happens when undo removes the held operation.
            timeline.Update([],10000);
            Console.WriteLine($"TRACE drag document replacement: selected={timeline.SelectedIds.Count}");
            window.MouseMove(new Point(300,58),RawInputModifiers.LeftMouseButton);
            window.MouseUp(new Point(300,58),MouseButton.Left);
            Check(changes==0,"Document replacement cancels the old group edit");

            timeline.Update([Operation("a",65,1000,1000)],10000); Draw(window);
            window.MouseDown(new Point(282,58),MouseButton.Left); // right resize handle
            timeline.Update([Operation("a",65,4000,1000)],10000);
            window.MouseMove(new Point(320,58),RawInputModifiers.LeftMouseButton);
            window.MouseUp(new Point(320,58),MouseButton.Left);
            Check(changes==0,"A same-ID refresh also invalidates the old resize");

            timeline.Update([Operation("a",65,1000,1000)],10000); Draw(window);
            timeline.Update([],10000); // No rendered frame between update and press.
            window.MouseDown(new Point(240,58),MouseButton.Left);
            window.MouseMove(new Point(300,58),RawInputModifiers.LeftMouseButton);
            window.MouseUp(new Point(300,58),MouseButton.Left);
            Check(changes==0 && timeline.SelectedIds.Count==0,"Stale hit rectangles cannot revive deleted operations");
        }
        finally { window.Close(); }
    }
    private static void EditorBatchRequestsAndTaskGuard()
    {
        var (window, view, model) = Open(false);
        try
        {
            var operations = new JsonArray(Operation("a",65,1000,1000).Data, Operation("b",66,1600,600).Data, Operation("c",67,7000,500).Data);
            model.Apply(new JsonObject { ["file"]="user.json", ["name"]="User recording", ["duration_ms"]=10000.0, ["operations"]=operations, ["dirty"]=false });
            model.Client.BatchSnapshot = model.Document!.DeepClone();
            Field<TabControl>(view,"_tabs").SelectedIndex=1;
            var timeline=Field<ScriptTimeline>(view,"_timeline");
            Draw(window); timeline.Focus(); window.KeyPressQwerty(PhysicalKey.A, RawInputModifiers.Control);
            Dispatcher.UIThread.RunJobs();
            Check(timeline.SelectedIds.Count==3 && Field<StackPanel>(view,"_batchInspector").IsVisible,"Ctrl+A opens the batch inspector");
            Field<Expander>(view,"_operationList").IsExpanded=true; Draw(window);
            model.Apply(model.Document!.DeepClone()); Dispatcher.UIThread.RunJobs();
            Check(timeline.SelectedIds.Count==3,"A document refresh preserves the selected IDs");
            Field<Expander>(view,"_operationList").IsExpanded=false;
            Draw(window,"editor-batch-light.png");
            window.Width=800;window.Height=600;Draw(window,"editor-batch-compact.png");
            Check(Field<ComboBox>(view,"_editorPicker").IsVisible && !Field<Control>(view,"_editorLibraryHost").IsVisible,
                "Compact editor uses its picker and gives space back to the timeline");
            Application.Current!.RequestedThemeVariant=Avalonia.Styling.ThemeVariant.Dark;Draw(window,"editor-batch-dark.png");
            Application.Current!.RequestedThemeVariant=Avalonia.Styling.ThemeVariant.Light;
            Field<NumericUpDown>(view,"_batchOffset").Value=-.1m;
            Click(Find(window,"移动选中操作"));
            Check(model.Client.LastBatchMethod=="move_many" && model.Client.LastBatchParameters?["delta_ms"]?.GetValue<double>()==-100
                && model.Client.LastBatchParameters?["ids"]?.AsArray().Count==3,"Batch shift sends every ID and signed milliseconds");
            timeline.Focus(); window.KeyPressQwerty(PhysicalKey.Delete, RawInputModifiers.None); Dispatcher.UIThread.RunJobs();
            Check(model.Client.LastBatchMethod=="delete_many" && model.Client.LastBatchParameters?["ids"]?.AsArray().Count==3,"Delete is one batch request");
            var calls=model.Client.Calls.Count;
            Field<TabControl>(view,"_tabs").SelectedIndex=0;
            view.Focus(); window.KeyPressQwerty(PhysicalKey.Delete,RawInputModifiers.None);Dispatcher.UIThread.RunJobs();
            Check(model.Client.Calls.Count==calls,"Library Delete must not delete the hidden editor selection");
            Field<TabControl>(view,"_tabs").SelectedIndex=1;
            MaaProcessor.Processors[0].IsTaskRunActive=true;
            window.KeyPressQwerty(PhysicalKey.Delete, RawInputModifiers.None); Dispatcher.UIThread.RunJobs();
            Check(model.Client.Calls.Count==calls,"Running task blocks batch edits");
        }
        finally { Close(window,model); }
    }
    private static void TimelineClipboardShortcuts()
    {
        var (window,view,model)=Open(false);
        try
        {
            var source=new JsonObject { ["file"]="clipboard.json",["name"]="Clipboard",["duration_ms"]=10000.0,["dirty"]=false,
                ["operations"]=new JsonArray(Operation("a",65,1000,1000).Data,Operation("b",66,1600,600).Data) };
            model.Apply(source);
            Field<TabControl>(view,"_tabs").SelectedIndex=1;
            var timeline=Field<ScriptTimeline>(view,"_timeline");Draw(window);timeline.Focus();
            window.KeyPressQwerty(PhysicalKey.A,RawInputModifiers.Control);Dispatcher.UIThread.RunJobs();
            JsonObject? request=null;
            model.Client.Handler=(method,args)=>
            {
                if (method=="copy_many")
                {
                    Check(args?["ids"]?.AsArray().Count==2,"Ctrl+C includes both selected tracks");
                    return Task.FromResult<JsonNode>(new JsonObject { ["format"]="maazmxy4.timeline",["version"]=1,["recording"]=new JsonObject() });
                }
                if (method!="paste_many") return null;
                request=args!.DeepClone().AsObject();
                var result=source.DeepClone().AsObject();
                result["operations"]!.AsArray().Add(Operation("pasted-a",65,4000,1000).Data);
                result["operations"]!.AsArray().Add(Operation("pasted-b",66,4600,600).Data);
                result["selected_ids"]=new JsonArray("pasted-a","pasted-b");result["dirty"]=true;
                return Task.FromResult<JsonNode>(result);
            };
            window.KeyPressQwerty(PhysicalKey.C,RawInputModifiers.Control);Dispatcher.UIThread.RunJobs();
            Check(window.Clipboard!.TryGetTextAsync().GetAwaiter().GetResult()?.Contains("maazmxy4.timeline")==true,"Copy uses the headless window clipboard");
            Field<NumericUpDown>(view,"_cursor").Value=4;timeline.Focus();
            window.KeyPressQwerty(PhysicalKey.V,RawInputModifiers.Control);Dispatcher.UIThread.RunJobs();
            Check(request?["at_ms"]?.GetValue<long>()==4000,"Paste uses the playhead, not the selected block or mouse location");
            Check(timeline.SelectedIds.Order().SequenceEqual(new[]{"pasted-a","pasted-b"}),"Paste selects all newly created operations");
            var calls=model.Client.Calls.Count;
            window.Clipboard.SetTextAsync("ordinary text").GetAwaiter().GetResult();
            window.KeyPressQwerty(PhysicalKey.V,RawInputModifiers.Control);Dispatcher.UIThread.RunJobs();
            Check(model.Client.Calls.Count==calls && model.Operations.Count==4,"Invalid clipboard preserves the document");
            Draw(window);
            var input=Field<NumericUpDown>(view,"_cursor").GetVisualDescendants().OfType<TextBox>().First();
            input.Focus();window.KeyPressQwerty(PhysicalKey.C,RawInputModifiers.Control);Dispatcher.UIThread.RunJobs();
            Check(model.Client.Calls.Count==calls,"Editing text keeps native Ctrl+C instead of copying timeline blocks");
            timeline.Focus();MaaProcessor.Processors[0].IsTaskRunActive=true;
            window.KeyPressQwerty(PhysicalKey.V,RawInputModifiers.Control);Dispatcher.UIThread.RunJobs();
            Check(model.Client.Calls.Count==calls,"Running task blocks paste");
            MaaProcessor.Processors[0].IsTaskRunActive=false;
            Draw(window,"editor-clipboard.png");
        }
        finally { Close(window,model); }
    }

    private static void DurationTextApplies()
    {
        var (window, view, model) = Open(false);
        try
        {
            model.Apply(JsonNode.Parse("""{"file":"user.json","name":"User recording","duration_ms":31359,"operations":[],"dirty":false}""")!);
            Field<TabControl>(view,"_tabs").SelectedIndex=1;
            Dispatcher.UIThread.RunJobs();
            var duration=Field<NumericUpDown>(view,"_scriptDuration");
            duration.Text="32.500";
            Console.WriteLine($"TRACE duration text={duration.Text} value={duration.Value}");
            Click(Find(window,"应用时长"));
            Check(model.Client.DurationRequest==32500, $"typed duration sent {model.Client.DurationRequest}, expected 32500");
            Check(model.Duration==32500, "duration response must update the page");
        }
        finally { Close(window,model); }
    }
    private static void DurationTextSavesWithoutExtraApply()
    {
        var (window, view, model) = Open(false);
        try
        {
            model.Apply(JsonNode.Parse("""{"file":"user.json","name":"User recording","duration_ms":31359,"operations":[],"dirty":false}""")!);
            Field<TabControl>(view,"_tabs").SelectedIndex=1;
            Dispatcher.UIThread.RunJobs();
            Field<NumericUpDown>(view,"_scriptDuration").Text="32.500";
            Click(Find(window,"保存"));
            Check(model.Client.DurationRequest==32500, "Save ignored the typed total duration");
            Check(model.Client.Calls.IndexOf("set_duration")<model.Client.Calls.IndexOf("save"), "duration must be applied before saving");
        }
        finally { Close(window,model); }
    }
    private static JsonObject CaptureSnapshot(string state, bool save = false) => new()
    { ["state"] = state, ["elapsed_ms"] = 1200.0, ["event_count"] = 6, ["countdown_seconds"] = 3.0, ["save_requested"] = save };
    private static JsonObject RecordingDraft() => new()
    { ["name"] = "本次录制", ["duration_ms"] = 1200.0, ["operations"] = new JsonArray(), ["dirty"] = true, ["capture_draft"] = true };
    private static void StartCapture(ScriptStudioView view)
    {
        Field<DispatcherTimer>(view, "_poll").Stop();
        Field<ComboBox>(view, "_windows").SelectedIndex = 0;
        Click(Field<Button>(view, "_record"));
        Field<DispatcherTimer>(view, "_poll").Stop();
    }
    private static Task Poll(ScriptStudioView view) => (Task)view.GetType()
        .GetMethod("PollCaptureAsync", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(view, null)!;
    private static void DiscardRecordingStatesAndRestart()
    {
        foreach (var state in new[] { "countdown", "recording", "paused", "resume_countdown" })
        {
            var (window, view, model) = Open(false);
            try
            {
                var before = JsonNode.Parse("""{"file":"user.json","name":"User recording","duration_ms":1000,"operations":[],"dirty":false}""")!;
                model.Apply(before.DeepClone());
                model.Client.Handler = (method, _) => method switch
                {
                    "capture_start" or "capture_status" => Task.FromResult<JsonNode>(CaptureSnapshot(state)),
                    "capture_discard" => Task.FromResult(before.DeepClone()), _ => null
                };
                StartCapture(view);
                var discard = Field<Button>(view, "_discardRecording");
                var record = Field<Button>(view, "_record");
                var target = Field<ComboBox>(view, "_windows").SelectedItem;
                var countdown = Field<NumericUpDown>(view, "_countdown").Value;
                var tabs = Field<TabControl>(view, "_tabs");
                var tabChanges = 0;
                tabs.SelectionChanged += (_, args) => { if (ReferenceEquals(args.Source, tabs)) tabChanges++; };
                Check(ReferenceEquals(record.Parent, discard.Parent), "Recording actions must share a layout");
                Check(model.Capturing && ScriptStudioGuard.Capturing && discard.IsEnabled, "Discard must be available in " + state);
                if (state == "recording")
                {
                    Draw(window, "recording-discard-wide.png");
                    window.Width = 840; Draw(window, "recording-discard-narrow.png");
                    var point = discard.TranslatePoint(new Point(), window)!.Value;
                    Check(point.X >= 0 && point.Y >= 0 && point.X + discard.Bounds.Width <= window.Width
                        && point.Y + discard.Bounds.Height <= window.Height, "Discard button must fit the narrow layout");
                }
                Click(discard);
                Check(!model.Capturing && !ScriptStudioGuard.Capturing, "Discard must release capture ownership");
                Check(JsonNode.DeepEquals(model.Document, before), "Active discard must retain the prior saved document");
                Check(window.OwnedWindows.Count == 0 && !model.Client.Calls.Contains("save")
                    && !model.Client.Calls.Contains("capture_stop"), "One click must discard without dialogs or importing a draft");
                Check(Field<Button>(view, "_record").IsEnabled && !discard.IsEnabled, "Recording must be ready immediately");
                Check(tabChanges == 0 && tabs.SelectedIndex == 0, "Discard in the recording area must not switch tabs");
                Check(ReferenceEquals(target, Field<ComboBox>(view, "_windows").SelectedItem)
                    && countdown == Field<NumericUpDown>(view, "_countdown").Value, "Discard preserves the selected window and countdown");
                Click(record); // No helper may reselect a window or reset the form for the next take.
                Field<DispatcherTimer>(view, "_poll").Stop();
                Check(model.Capturing && model.Client.Calls.Count(call => call == "capture_start") == 2, "Can record again after discard");
                Check(tabChanges == 0 && window.OwnedWindows.Count == 0, "Re-record must stay on the same page without prompts");
                Click(discard);
            }
            finally { Close(window, model); }
        }
    }
    private static void DiscardStoppedDraftAndProtectSavedScript()
    {
        var (window, view, model) = Open(false);
        try
        {
            model.Client.Handler = (method, _) => method switch
            {
                "capture_start" => Task.FromResult<JsonNode>(CaptureSnapshot("recording")),
                "capture_stop" or "rename" => Task.FromResult<JsonNode>(RecordingDraft()),
                "capture_discard" => Task.FromResult<JsonNode>(new JsonObject { ["empty"] = true }), _ => null
            };
            StartCapture(view); Click(Field<Button>(view, "_stop"));
            var discard = Field<Button>(view, "_discardRecordingDraft");
            Check(!model.Capturing && model.CaptureDraft && discard.IsVisible && discard.IsEnabled, "Stopped unsaved take must have a discard action in the editor");
            Check(Field<TabControl>(view, "_tabs").SelectedIndex == 1, "Normal stop still opens the editor");
            Draw(window, "recording-draft-discard.png");
            Field<NumericUpDown>(view, "_scriptDuration").Value = 5;
            Click(discard);
            Check(model.Document == null && !model.Dirty && window.OwnedWindows.Count == 0, "Discard must clear the recording draft and pending form edits directly");
            Check(Field<TabControl>(view, "_tabs").SelectedIndex == 0 && Field<Button>(view, "_record").IsEnabled, "Discard returns to recording");
            model.Apply(JsonNode.Parse("""{"file":"user.json","name":"Saved","duration_ms":1000,"operations":[],"dirty":true}""")!);
            var calls = model.Client.Calls.Count;
            Check(!discard.IsVisible && !Field<Button>(view, "_discardRecording").IsEnabled, "Saved script edits must not be exposed as a disposable take");
            Click(discard);
            Check(model.Client.Calls.Count == calls && model.Dirty && model.File == "user.json", "A stale discard event must not delete saved script edits");
        }
        finally { Close(window, model); }
    }
    private static void DiscardFailureCanRetry()
    {
        var (window, view, model) = Open(false);
        try
        {
            var fail = true;
            model.Client.Handler = (method, _) => method switch
            {
                "capture_start" => Task.FromResult<JsonNode>(CaptureSnapshot("recording")),
                "capture_discard" => fail ? Task.FromException<JsonNode>(new IOException("监听器尚未退出"))
                    : Task.FromResult<JsonNode>(new JsonObject { ["empty"] = true }), _ => null
            };
            StartCapture(view); Click(Field<Button>(view, "_discardRecording"));
            Check(model.Capturing && ScriptStudioGuard.Capturing && Field<TextBlock>(view, "_status").Text == "监听器尚未退出", "Cleanup failure must retain ownership and report the error");
            fail = false; Click(Field<Button>(view, "_discardRecording"));
            Check(!model.Capturing && !ScriptStudioGuard.Capturing, "Retry must be available after cleanup failure");
        }
        finally { Close(window, model); }
    }
    private static void DiscardIgnoresLateStatusAndSaveRequest()
    {
        var (window, view, model) = Open(false);
        try
        {
            var status = new TaskCompletionSource<JsonNode>();
            model.Client.Handler = (method, _) => method switch
            {
                "capture_start" => Task.FromResult<JsonNode>(CaptureSnapshot("recording")),
                "capture_status" => status.Task,
                "capture_discard" => Task.FromResult<JsonNode>(new JsonObject { ["empty"] = true }), _ => null
            };
            StartCapture(view);
            var polling = Poll(view);
            Click(Field<Button>(view, "_discardRecording"));
            StartCapture(view);
            status.SetResult(CaptureSnapshot("stopped", save: true)); Dispatcher.UIThread.RunJobs();
            Check(polling.IsCompletedSuccessfully && model.Capturing && Field<string>(view, "_state") == "recording", "Late old status must not stop the next take");
            Check(!model.Client.Calls.Contains("capture_stop") && !model.Client.Calls.Contains("save") && window.OwnedWindows.Count == 0, "Discard must suppress stale F10 save prompts");
            Click(Field<Button>(view, "_discardRecording"));
        }
        finally { Close(window, model); }
    }
    private static void TerminalPollLocksUntilDraftIsReady()
    {
        var (window, view, model) = Open(false);
        try
        {
            var stopped = new TaskCompletionSource<JsonNode>();
            model.Client.Handler = (method, _) => method switch
            {
                "capture_start" => Task.FromResult<JsonNode>(CaptureSnapshot("recording")),
                "capture_status" => Task.FromResult<JsonNode>(CaptureSnapshot("stopped")),
                "capture_stop" => stopped.Task,
                "rename" => Task.FromResult<JsonNode>(RecordingDraft()),
                "capture_discard" => Task.FromResult<JsonNode>(new JsonObject { ["empty"] = true }), _ => null
            };
            StartCapture(view);
            Field<CheckBox>(view, "_doubleClickDiscard").IsChecked = true;
            var polling = Poll(view);
            Check(!Field<Button>(view, "_discardRecording").IsEnabled, "Stop finalization must temporarily lock recording actions");
            Click(Field<Button>(view, "_discardRecording"));
            Check(!model.Client.Calls.Contains("capture_discard"), "Discard must not interleave with stop importing a draft");
            stopped.SetResult(RecordingDraft()); Dispatcher.UIThread.RunJobs();
            Check(polling.IsCompletedSuccessfully && model.CaptureDraft, "Hotkey stop must still produce an editable draft");
            Click(Field<Button>(view, "_discardRecordingDraft"));
            Check(model.Document == null, "The finished draft must be discardable");
        }
        finally { Close(window, model); }
    }
    private static void RecordingActionLayout()
    {
        var (window, view, model) = Open(false);
        try
        {
            model.Client.Handler = (method, _) => method == "capture_start"
                ? Task.FromResult<JsonNode>(CaptureSnapshot("paused")) : null;
            StartCapture(view);
            var record = Field<Button>(view, "_record");
            var stop = Field<Button>(view, "_stop");
            var pause = Field<Button>(view, "_pause");
            var discard = Field<Button>(view, "_discardRecording");
            var option = Field<CheckBox>(view, "_doubleClickDiscard");
            Check(Equals(option.Content, "双击放弃本次录制") && option.IsChecked == false,
                "Double-click option must be labeled and unchecked by default");
            Check(discard.Background is ISolidColorBrush red && red.Color.R > red.Color.G * 1.5 && red.Color.R > red.Color.B * 1.5,
                "The recording discard button must be red");
            foreach (var (width, height) in new[] { (1200.0, 800.0), (840.0, 800.0), (640.0, 600.0) })
            {
                window.Width = width; window.Height = height;
                Draw(window, $"recording-panel-{width:0}.png");
                var buttons = new[] { record, stop, pause, discard };
                var positions = buttons.Select(button => button.TranslatePoint(new Point(), window)!.Value).ToArray();
                Check(buttons.All(button => Math.Abs(button.Bounds.Width - record.Bounds.Width) < .1
                    && Math.Abs(button.Bounds.Height - record.Bounds.Height) < .1), "All recording buttons must have equal size at " + width);
                Check(Math.Abs(positions[0].Y - positions[1].Y) < .1 && positions[0].X < positions[1].X
                    && Math.Abs(positions[2].Y - positions[3].Y) < .1 && positions[2].X < positions[3].X
                    && positions[0].Y < positions[2].Y, "Recording action order must be start/stop, pause/discard at " + width);
                Check(buttons.All(button =>
                {
                    var point = button.TranslatePoint(new Point(), window)!.Value;
                    return point.X >= 0 && point.Y >= 0 && point.X + button.Bounds.Width <= window.Width
                        && point.Y + button.Bounds.Height <= window.Height;
                }), "Recording buttons must fit within the window at " + width);
                var optionPoint = option.TranslatePoint(new Point(), window)!.Value;
                Check(optionPoint.Y >= positions[3].Y + discard.Bounds.Height && optionPoint.X >= 0
                    && optionPoint.X + option.Bounds.Width <= window.Width && optionPoint.Y + option.Bounds.Height <= window.Height,
                    "Double-click option must be visible below the actions at " + width);
                if (height == 600)
                {
                    var scroll = Field<Expander>(view, "_recordingExpander").GetVisualAncestors().OfType<ScrollViewer>().FirstOrDefault();
                    Check(scroll != null && scroll.Extent.Height > scroll.Viewport.Height,
                        "A short window must allow the rest of the recording panel to scroll");
                    var top = scroll!.TranslatePoint(new Point(), window)!.Value.Y;
                    Check(optionPoint.Y + option.Bounds.Height <= top + scroll.Bounds.Height,
                        "The double-click option must remain visible in the short recording panel");
                    scroll.Offset = new Vector(0, scroll.Extent.Height - scroll.Viewport.Height); Draw(window);
                    Check(scroll.Offset.Y > 0, "Recording panel instructions must remain reachable by scrolling");
                }
            }
            Application.Current!.RequestedThemeVariant = Avalonia.Styling.ThemeVariant.Dark;
            Draw(window, "recording-panel-dark.png");
            Application.Current!.RequestedThemeVariant = Avalonia.Styling.ThemeVariant.Light;
        }
        finally { Application.Current!.RequestedThemeVariant = Avalonia.Styling.ThemeVariant.Light; Close(window, model); }
    }
    private static void RecordingDoubleClickCancellationAndRetry()
    {
        var (window, view, model) = Open(false);
        try
        {
            var fail = true;
            model.Client.Handler = (method, _) => method switch
            {
                "capture_start" => Task.FromResult<JsonNode>(CaptureSnapshot("recording")),
                "capture_discard" => fail ? Task.FromException<JsonNode>(new IOException("监听器尚未退出"))
                    : Task.FromResult<JsonNode>(new JsonObject { ["empty"] = true }), _ => null
            };
            StartCapture(view);
            var discard = Field<Button>(view, "_discardRecording");
            var option = Field<CheckBox>(view, "_doubleClickDiscard");
            MouseClick(window, option);
            MouseClick(window, discard);
            var point = discard.TranslatePoint(new Point(discard.Bounds.Width / 2, discard.Bounds.Height / 2), window)!.Value;
            window.MouseDown(point, MouseButton.Left);
            window.MouseMove(new Point(point.X - 200, point.Y), RawInputModifiers.LeftMouseButton);
            window.MouseUp(new Point(point.X - 200, point.Y), MouseButton.Left);
            Dispatcher.UIThread.RunJobs();
            Check(model.Capturing && !model.Client.Calls.Contains("capture_discard"),
                "Releasing the second click outside the button must cancel discard");
            discard.Focus(); window.KeyPressQwerty(PhysicalKey.Space, RawInputModifiers.None);
            window.KeyReleaseQwerty(PhysicalKey.Space, RawInputModifiers.None); Dispatcher.UIThread.RunJobs();
            Check(model.Capturing && !model.Client.Calls.Contains("capture_discard"),
                "A canceled double-click must not arm a later keyboard activation");
            MouseClick(window, option); MouseClick(window, option); // End the old gesture before retrying.
            MouseClick(window, discard); MouseClick(window, discard);
            Check(model.Capturing && model.Client.Calls.Count(call => call == "capture_discard") == 1,
                "A cleanup error must preserve the active recording");
            fail = false;
            MouseClick(window, discard); MouseClick(window, discard);
            Check(!model.Capturing && model.Client.Calls.Count(call => call == "capture_discard") == 2,
                "A new double-click must retry after a cleanup error");
        }
        finally { Close(window, model); }
    }
    private static void MouseClick(Window window, Control control)
    {
        Draw(window);
        var point = control.TranslatePoint(new Point(control.Bounds.Width / 2, control.Bounds.Height / 2), window)!.Value;
        window.MouseMove(point);
        window.MouseDown(point, MouseButton.Left);
        window.MouseUp(point, MouseButton.Left);
        Dispatcher.UIThread.RunJobs();
    }
    private static void RecordingDiscardClickModes()
    {
        var (window, view, model) = Open(false);
        try
        {
            model.Client.Handler = (method, _) => method switch
            {
                "capture_start" => Task.FromResult<JsonNode>(CaptureSnapshot("recording")),
                "capture_discard" => Task.FromResult<JsonNode>(new JsonObject { ["empty"] = true }), _ => null
            };
            StartCapture(view);
            var discard = Field<Button>(view, "_discardRecording");
            var option = Field<CheckBox>(view, "_doubleClickDiscard");
            MouseClick(window, discard);
            Check(!model.Capturing && model.Client.Calls.Count(call => call == "capture_discard") == 1,
                "Unchecked option must preserve the existing single-click behavior");
            StartCapture(view);
            MouseClick(window, option);
            Check(option.IsChecked == true, "Mouse click must enable the double-click option");
            var tabs = Field<TabControl>(view, "_tabs");
            tabs.SelectedIndex = 1; tabs.SelectedIndex = 0; Draw(window);
            Check(option.IsChecked == true, "Changing tabs must preserve the current-page option");
            var point = discard.TranslatePoint(new Point(discard.Bounds.Width / 2, discard.Bounds.Height / 2), window)!.Value;
            window.MouseDown(point, MouseButton.Right); window.MouseUp(point, MouseButton.Right);
            window.MouseDown(point, MouseButton.Right); window.MouseUp(point, MouseButton.Right);
            Dispatcher.UIThread.RunJobs();
            Check(model.Capturing && model.Client.Calls.Count(call => call == "capture_discard") == 1,
                "Double right-click must not discard");
            discard.Focus();
            var keys = new List<Key>();
            view.AddHandler(InputElement.KeyDownEvent, (_, e) =>
            {
                if (!e.Handled && e.Key is Key.F8 or Key.F9 or Key.F10 or Key.F12) keys.Add(e.Key);
            }, RoutingStrategies.Bubble, handledEventsToo: true);
            foreach (var key in new[] { PhysicalKey.F8, PhysicalKey.F9, PhysicalKey.F10, PhysicalKey.F12 })
            {
                window.KeyPressQwerty(key, RawInputModifiers.None);
                window.KeyReleaseQwerty(key, RawInputModifiers.None);
            }
            Check(keys.SequenceEqual(new[] { Key.F8, Key.F9, Key.F10, Key.F12 }),
                "Double-click option must not consume recording hotkey events");
            MouseClick(window, discard);
            Check(model.Capturing && model.Client.Calls.Count(call => call == "capture_discard") == 1,
                "First click in double-click mode must not discard");
            window.MouseDown(point, MouseButton.Left);
            Dispatcher.UIThread.RunJobs();
            Check(model.Capturing, "A double-click must not discard until the second click is released");
            window.MouseUp(point, MouseButton.Left);
            Dispatcher.UIThread.RunJobs();
            Check(!model.Capturing && model.Client.Calls.Count(call => call == "capture_discard") == 2,
                "Second click must discard exactly once");
            Check(Field<Button>(view, "_record").IsFocused, "After discard, focus must return to the next recording action");
            StartCapture(view);
            MouseClick(window, option);
            Check(option.IsChecked == false, "Mouse click must disable the double-click option");
            MouseClick(window, discard);
            Check(!model.Capturing && model.Client.Calls.Count(call => call == "capture_discard") == 3,
                "Disabling the option must immediately restore single-click behavior");
        }
        finally { Close(window, model); }
    }
    private static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static void Click(Button button) { button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Dispatcher.UIThread.RunJobs(); }
    private static Button Find(Window window, string name) => window.GetVisualDescendants().OfType<Button>().First(b => Equals(b.Content, name));
    private static (Window Window, ScriptStudioView View, ScriptStudioViewModel Model) Open(bool active, int failures = 0)
    {
        MaaProcessor.Processors.Clear(); MaaProcessor.Processors.Add(new() { IsTaskRunActive = active });
        WindowFixture.ListCalls = 0;
        var model = new ScriptStudioViewModel(); model.Client.FailLists = failures;
        var view = new ScriptStudioView(model);
        var window = new Window { Content = view, Width = 1200, Height = 800 };
        window.Show(); Dispatcher.UIThread.RunJobs();
        Console.WriteLine($"TRACE attached active={active} lists={model.Client.Calls.Count(x => x == "list")} windows={WindowFixture.ListCalls} initialized={Field<bool>(view,"_initialized")} status={Field<TextBlock>(view,"_status").Text}");
        return (window, view, model);
    }
    private static void Close(Window window, ScriptStudioViewModel model)
    {
        MaaProcessor.Processors.Clear(); model.Capturing = false; ScriptStudioGuard.EndCapture();
        model.Apply(new JsonObject { ["empty"] = true }); window.Close(); Dispatcher.UIThread.RunJobs();
    }
    private static void DialogThemeAndDismissal()
    {
        var (window, view, model) = Open(false);
        var app = Application.Current!;
        try
        {
            window.Width = 800;
            var result = (Task<string?>)typeof(ScriptStudioView).GetMethod("AskNameAsync", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(view, ["另存为", "Sample", null])!;
            Dispatcher.UIThread.RunJobs();
            var dialog = window.OwnedWindows.Single();
            var input = dialog.GetVisualDescendants().OfType<TextBox>().Single();
            Check(input.IsFocused && input.SelectionEnd - input.SelectionStart == input.Text!.Length,
                "Name dialog must focus and select the suggested name");
            foreach (var theme in new[] { Avalonia.Styling.ThemeVariant.Dark, Avalonia.Styling.ThemeVariant.Light, Avalonia.Styling.ThemeVariant.Dark })
            {
                app.RequestedThemeVariant = theme;
                // Suki buttons animate foreground changes for 100 ms; inspect the settled frame.
                var settled = DateTime.UtcNow.AddMilliseconds(250);
                while (DateTime.UtcNow < settled)
                { Dispatcher.UIThread.RunJobs(); AvaloniaHeadlessPlatform.ForceRenderTimerTick(); Thread.Sleep(10); }
                Draw(dialog, $"name-dialog-{theme}.png");
                var background = ((ISolidColorBrush)dialog.Background!).Color;
                var foreground = ((ISolidColorBrush)dialog.Foreground!).Color;
                var expected = theme == Avalonia.Styling.ThemeVariant.Dark ? Color.Parse("#323232") : Colors.White;
                Console.WriteLine($"TRACE dialog theme={theme} background={background} foreground={foreground} button={Find(dialog, "取消").Foreground}");
                Check(background == expected, "Dialog background must follow the host card theme, including while open");
                Check(Contrast(background, ((ISolidColorBrush)Find(dialog, "取消").Foreground!).Color) >= 4.5,
                    "Dialog buttons must remain readable against the background");
                Check(Contrast(background, foreground) >= 4.5, "Dialog text must remain readable");
            }
            dialog.KeyPressQwerty(PhysicalKey.Escape, RawInputModifiers.None);
            Dispatcher.UIThread.RunJobs();
            Check(result.IsCompletedSuccessfully && result.Result == null && view.IsFocused,
                "Escape cancels the name dialog and restores page focus");

            result = (Task<string?>)typeof(ScriptStudioView).GetMethod("AskNameAsync", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(view, ["保存脚本", "Keyboard saved", null])!;
            Dispatcher.UIThread.RunJobs();
            dialog = window.OwnedWindows.Single();
            dialog.KeyPressQwerty(PhysicalKey.Enter, RawInputModifiers.None);
            Dispatcher.UIThread.RunJobs();
            Check(result.IsCompletedSuccessfully && result.Result == "Keyboard saved", "Enter must submit the existing name dialog");

            var deletion = (Task<bool>)typeof(ScriptStudioView).GetMethod("ConfirmDeleteAsync", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(view, ["删除此脚本？这是一段用于验证自动换行的较长确认说明。"])!;
            Dispatcher.UIThread.RunJobs();
            dialog = window.OwnedWindows.Single();
            Draw(dialog, "delete-dialog-Dark.png");
            Check(((ISolidColorBrush)dialog.Background!).Color == Color.Parse("#323232"), "Delete confirmation must share the host theme");
            dialog.Close(); Dispatcher.UIThread.RunJobs();
            Check(deletion.IsCompletedSuccessfully && !deletion.Result, "Closing the delete dialog must cancel deletion");

            deletion = (Task<bool>)typeof(ScriptStudioView).GetMethod("ConfirmDeleteAsync", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(view, ["删除此脚本？"])!;
            Dispatcher.UIThread.RunJobs();
            Click(Find(window.OwnedWindows.Single(), "删除"));
            Check(deletion.IsCompletedSuccessfully && deletion.Result, "Delete confirmation must return true only when selected");
        }
        finally
        {
            foreach (var dialog in window.OwnedWindows.ToArray()) dialog.Close();
            app.RequestedThemeVariant = Avalonia.Styling.ThemeVariant.Light;
            Close(window, model);
        }
    }
    private static double Contrast(Color a, Color b)
    {
        static double Linear(byte value) { var c = value / 255d; return c <= .04045 ? c / 12.92 : Math.Pow((c + .055) / 1.055, 2.4); }
        static double Luminance(Color c) => .2126 * Linear(c.R) + .7152 * Linear(c.G) + .0722 * Linear(c.B);
        var x = Luminance(a); var y = Luminance(b);
        return (Math.Max(x, y) + .05) / (Math.Min(x, y) + .05);
    }
    private static void RenameCancelCollisionAndSave()
    {
        var (window, view, model) = Open(false);
        try
        {
            model.Apply(JsonNode.Parse("""{"file":"user.json","name":"User recording","duration_ms":1000,"operations":[],"dirty":false}""")!);
            Dispatcher.UIThread.RunJobs();
            Click(Find(window,"重命名…"));
            var dialog=window.OwnedWindows.Single();
            dialog.GetVisualDescendants().OfType<TextBox>().Single().Text="Cancelled";
            Click(Find(dialog,"取消"));
            Check(!model.Client.Calls.Contains("rename_file") && model.File=="user.json", "cancel must preserve the saved file");
            Click(Find(window,"重命名…"));
            dialog=window.OwnedWindows.Single();
            var input=dialog.GetVisualDescendants().OfType<TextBox>().Single();
            input.Text="default";
            Click(Find(dialog,"保存"));
            Check(dialog.IsVisible && !model.Client.Calls.Contains("rename_file"),"collision must keep the name dialog open");
            input.Text="Renamed script";
            Click(Find(dialog,"保存"));
            Check(model.File=="Renamed script.json" && model.Name=="Renamed script", "renaming must update the loaded document path and name");
            Check(model.Client.Calls.Count(x=>x=="rename_file")==1, "one confirmation must perform one rename");
            var selected=Field<ListBox>(view,"_library").SelectedItem as JsonNode;
            Check(selected?["file"]?.GetValue<string>()==model.File, "library selection must follow the renamed document");
        }
        finally { Close(window,model); }
    }
    private static void RenameOtherFileKeepsEditorState()
    {
        var (window,view,model)=Open(false);
        try
        {
            model.Apply(JsonNode.Parse("""{"file":"user.json","name":"User recording","duration_ms":1000,"operations":[],"dirty":true}""")!);
            Dispatcher.UIThread.RunJobs();
            var changes=0;
            model.Changed+=()=>changes++;
            var other=Field<JsonArray>(view,"_files")[1]!;
            var rename=(Task)view.GetType().GetMethod("RenameLibraryFileAsync",BindingFlags.Instance|BindingFlags.NonPublic)!.Invoke(view,new object[]{other})!;
            Dispatcher.UIThread.RunJobs();
            var dialog=window.OwnedWindows.Single();
            dialog.GetVisualDescendants().OfType<TextBox>().Single().Text="Other renamed";
            Click(Find(dialog,"保存"));
            Check(rename.IsCompletedSuccessfully && model.File=="user.json" && model.Dirty && changes==0,
                "renaming another row must not rerender or discard the current unsaved inspector");
        }
        finally { Close(window,model); }
    }
    private static void FirstOpenDuringTask()
    {
        var (window, view, model) = Open(true);
        try
        {
            Check(Field<ListBox>(view,"_library").ItemCount == 2, "first open must show both scripts while a task runs");
            Check(WindowFixture.ListCalls == 1, "read-only window discovery must run");
            Check(!Field<Button>(view,"_record").IsEnabled, "recording must remain disabled");
        }
        finally { Close(window, model); }
    }
    private static void RefreshAndMutationGuard()
    {
        var (window, view, model) = Open(false);
        try
        {
            MaaProcessor.Processors[0].IsTaskRunActive = true;
            Click(Find(window,"刷新"));
            Check(model.Client.Calls.Count(x=>x=="list") == 2, "library refresh must work after a task starts");
            Check(Field<ListBox>(view,"_library").ItemCount == 2, "refresh must preserve visible scripts");
            Click(Find(window,"新建"));
            Click(Field<Button>(view,"_record"));
            Check(!model.Client.Calls.Contains("new") && !model.Client.Calls.Contains("capture_start"), "mutations must still be blocked");
            MaaProcessor.Processors[0].IsTaskRunActive = false;
            Click(Find(window,"新建"));
            Check(model.Client.Calls.Contains("new"), "editing must unlock after the task stops");
        }
        finally { Close(window, model); }
    }
    private static void RetryFailedInitialization()
    {
        var (window, view, model) = Open(false, 1);
        try
        {
            Check(!Field<bool>(view,"_initialized"), "a failed first load must not mark initialization complete");
            window.Content = null; window.Content = view; Dispatcher.UIThread.RunJobs();
            Check(Field<ListBox>(view,"_library").ItemCount == 2, "reattaching after a failed load must retry");
            Check(Field<bool>(view,"_initialized"), "successful retry must complete initialization");
        }
        finally { Close(window, model); }
    }
    private static void RecordingUsesCurrentTaskController()
    {
        WindowFixture.Windows = new[] { new WindowFixture(7,"Alternate game client","OtherWindowClass"), new WindowFixture(3,"Second client","OtherWindowClass") };
        var before = WindowFixture.ListCalls;
        var windows = StudioWindows.List();
        Check(WindowFixture.ListCalls == before+1, "recording must query the current task page instead of enumerating with its own filters");
        Check(windows.Select(x=>x.Handle).SequenceEqual(new nint[] { 7,3 }), "recording must keep the host-selected ordering and support arbitrary titles/classes");
        Check(windows[0].Title == "Alternate game client", "recording must use the host-provided labels");
    }
    private static void NoImplicitRecordingTarget()
    {
        MaaProcessorManager.Instance.Current.Config.DesktopWindow.HWnd = 0;
        var (window, view, model) = Open(false);
        try
        {
            Check(Field<ComboBox>(view,"_windows").SelectedItem == null,
                "a new recorder must not silently choose the first unrelated desktop window");
            MaaProcessorManager.Instance.Current.Config.DesktopWindow.HWnd = WindowFixture.Windows[0].Handle;
            view.GetType().GetMethod("RefreshWindows",BindingFlags.Instance|BindingFlags.NonPublic)!.Invoke(view,null);
            Check((Field<ComboBox>(view,"_windows").SelectedItem as StudioWindow)?.Handle == WindowFixture.Windows[0].Handle,
                "recorder must reuse an explicitly selected task window");
        }
        finally { Close(window,model); MaaProcessorManager.Instance.Current.Config.DesktopWindow.HWnd = 0; }
    }
}

internal sealed class TestApplication : Application
{
    public override void Initialize()
    {
        Styles.Add(new FluentTheme());
        // Optionally load the same theme assemblies as the built host, without starting its app or services.
        if (Environment.GetEnvironmentVariable("STUDIO_MFA_THEME_DIR") is { } directory)
        {
            System.Runtime.Loader.AssemblyLoadContext.Default.Resolving += (_, name) =>
            {
                var path = Path.Combine(directory, name.Name + ".dll");
                return File.Exists(path) ? Assembly.LoadFrom(path) : null;
            };
            foreach (var entry in new[] { ("FluentAvalonia", "FluentAvalonia.Styling.FluentAvaloniaTheme"), ("SukiUI", "SukiUI.SukiTheme") })
                Styles.Add((Avalonia.Styling.IStyle)Activator.CreateInstance(Assembly.LoadFrom(Path.Combine(directory, entry.Item1 + ".dll")).GetType(entry.Item2)!)!);
        }
        else
        {
            Resources.ThemeDictionaries[Avalonia.Styling.ThemeVariant.Light] = new ResourceDictionary
                { ["SukiCardBackground"] = Colors.White, ["SukiText"] = Color.Parse("#222222") };
            Resources.ThemeDictionaries[Avalonia.Styling.ThemeVariant.Dark] = new ResourceDictionary
                { ["SukiCardBackground"] = Color.Parse("#323232"), ["SukiText"] = Color.Parse("#edffffff") };
        }
    }
}

// Only the host/task state, OS discovery and service transport are faked. The production
// view, event handlers, layout, model and timeline above are compiled without alteration.
namespace MFAAvalonia.ViewModels.Pages { public class ViewModelBase { } }
namespace MFAAvalonia.Extensions.MaaFW
{
    public sealed class MaaProcessor
    {
        public static List<MaaProcessor> Processors { get; } = new();
        public bool IsTaskRunActive { get; set; }
        public string InstanceId => "fixture";
        public ControllerConfigFixture Config { get; } = new();
    }
    public sealed class ControllerConfigFixture { public DesktopFixture DesktopWindow { get; } = new(); }
    public sealed class DesktopFixture { public nint HWnd { get; set; } }
    public sealed class MaaProcessorManager
    {
        public static bool IsInstanceCreated => true;
        public static MaaProcessorManager Instance { get; } = new();
        public MaaProcessor Current { get; } = new();
        public WindowFixture GetViewModel(string instanceId) => new(0,"","");
    }
}
namespace MFAAvalonia.Services
{
    public sealed record WindowFixture(nint Handle, string Name, string ClassName)
    {
        public static int ListCalls;
        public static IReadOnlyList<WindowFixture> Windows = new[] { new WindowFixture(1,"Fixture window","Fixture") };
        public IReadOnlyList<WindowFixture> GetScriptRecordingWindows() { ListCalls++; return Windows; }
    }
    public static class ScriptStudioGuard
    {
        public static bool Capturing { get; private set; }
        public static void BeginCapture() { if (Capturing || MaaProcessor.Processors.Any(p=>p.IsTaskRunActive)) throw new InvalidOperationException("Task or capture is active"); Capturing = true; }
        public static void EndCapture() { Capturing = false; }
    }
    public sealed class ScriptStudioClient : IDisposable
    {
        public List<string> Calls { get; } = new();
        public int FailLists;
        public long? DurationRequest;
        public string? LastBatchMethod;
        public JsonObject? LastBatchParameters;
        public JsonNode? BatchSnapshot;
        public Func<string, JsonObject?, Task<JsonNode>?>? Handler;
        private string? _renamed, _renamedFile;
        public bool IsDisconnected => false;
        public Task<JsonNode> CallAsync(string method, JsonObject? parameters = null)
        {
            Calls.Add(method);
            if (Handler?.Invoke(method, parameters) is { } handled) return handled;
            if (method is "move_many" or "delete_many")
            {
                LastBatchMethod=method; LastBatchParameters=parameters?.DeepClone().AsObject();
                return Task.FromResult(BatchSnapshot!.DeepClone());
            }
            if (method=="set_duration")
            {
                DurationRequest=parameters!["duration_ms"]!.GetValue<long>();
                return Task.FromResult<JsonNode>(new JsonObject { ["file"]="user.json", ["name"]="User recording", ["duration_ms"]=(double)DurationRequest.Value, ["operations"]=new JsonArray(), ["dirty"]=true });
            }
            if (method == "list" && FailLists-- > 0) throw new IOException("Fixture transient list failure");
            if (method == "rename_file")
            {
                _renamed=parameters!["name"]!.GetValue<string>();
                _renamedFile=parameters["file"]!.GetValue<string>();
                return Task.FromResult<JsonNode>(new JsonObject { ["document"]=new JsonObject
                { ["file"]=_renamed+".json", ["name"]=_renamed, ["duration_ms"]=1000.0, ["operations"]=new JsonArray(), ["dirty"]=false } });
            }
            JsonNode result = method == "list"
                ? JsonNode.Parse("""{"items":[{"name":"User recording","file":"user.json","revision":"fixture","duration_ms":1000,"event_count":2},{"name":"Default recording","file":"default.json","revision":"fixture","duration_ms":1000,"event_count":2,"is_default":true}],"tasks":[],"binding_revision":"fixture"}""")!
                : JsonNode.Parse("""{"name":"New recording","duration_ms":0,"operations":[],"dirty":false}""")!;
            if (method=="list" && _renamed!=null)
            {
                var item=result["items"]!.AsArray().First(value=>value!["file"]!.GetValue<string>()==_renamedFile)!;
                item["name"]=_renamed; item["file"]=_renamed+".json";
            }
            return Task.FromResult(result);
        }
        public void Dispose() { }
    }
}
