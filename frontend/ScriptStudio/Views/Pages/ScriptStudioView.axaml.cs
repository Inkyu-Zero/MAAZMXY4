using System;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Controls;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Services;
using MFAAvalonia.ViewModels.Pages;

namespace MFAAvalonia.Views.Pages;

/// <summary>Native MFA page, retaining the existing app navigation and themes.</summary>
public sealed partial class ScriptStudioView : UserControl
{
    private const string RunningHint = "任务正在运行，可查看和刷新脚本列表；编辑、绑定或录制请先停止任务。";
    private readonly ScriptStudioViewModel _vm;
    private readonly TabControl _tabs = new();
    private readonly ListBox _library = new(), _events = new();
    private readonly TextBox _search = new() { Watermark = "搜索脚本名称" };
    private readonly TextBlock _summary = new(), _status = new(), _captureState = new();
    private readonly TextBlock _captureHint = new() { TextWrapping = TextWrapping.Wrap, Opacity = .7 };
    private readonly TextBlock _diagnostics = new() { TextWrapping = TextWrapping.Wrap };
    private readonly ComboBox _windows = new() { HorizontalAlignment = HorizontalAlignment.Stretch, MinWidth = 200 };
    private readonly NumericUpDown _countdown = Number(3, 0, 30, "0");
    private readonly ScriptTimeline _timeline = new();
    private readonly ScrollViewer _timelineScroll = new() { HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto, HorizontalContentAlignment = HorizontalAlignment.Left, VerticalContentAlignment = VerticalAlignment.Top };
    private readonly NumericUpDown _cursor = Number(0, 0, 604800, "0.000");
    private readonly NumericUpDown _scriptDuration = Number(0, 0, 604800, "0.000");
    private readonly ComboBox _kind = new() { ItemsSource = new[] { "按键长按", "鼠标点击 / 拖动", "单次按键", "单次点击", "鼠标移动", "滚轮", "输入文本", "阶段标记" }, SelectedIndex = 1 };
    private readonly NumericUpDown _start = Number(0), _duration = Number(.05), _key = Number(74, 1, 255, "0"), _x = Number(0, 0, 32767, "0"), _y = Number(0, 0, 32767, "0");
    private readonly NumericUpDown _dx = Number(0, -100000, 100000, "0"), _dy = Number(1, -100000, 100000, "0");
    private readonly ComboBox _button = new() { ItemsSource = new[] { "左键", "右键", "中键" }, SelectedIndex = 0 };
    private readonly TextBox _text = new() { Watermark = "文本或标记名称", AcceptsReturn = true, MinHeight = 70 };
    private readonly TextBox _keyCapture = new() { Watermark = "点此后按一个键", IsReadOnly = true };
    private readonly StackPanel _inspector = new() { Spacing = 8 };
    private Control? _keyFields, _mouseFields, _scrollFields;
    private readonly Button _save, _record, _pause, _stop, _apply, _undo, _redo;
    private Button? _refreshWindows, _mark;
    private readonly DispatcherTimer _poll = new() { Interval = TimeSpan.FromMilliseconds(250) };
    private JsonArray _files = new();
    private StudioOperation? _selected;
    private bool _sync, _busy, _polling, _initialized, _inspectorDirty, _adding, _allowClose;
    private Window? _owner;
    private string _recordingName = "";
    private string _state = "idle";
    private string _renderedName = "";
    private string? _renderedFile;
    private bool _fitNextTimeline = true;
    private Grid? _editorMain;
    private GridSplitter? _editorSplitter;
    private Control? _inspectorPane;
    private Expander? _operationList;
    private Button? _focusTimelineButton;
    private bool _timelineFocused;
    private GridLength _inspectorWidth = new(320);
    private string _inspectorBaseline = "";

    public ScriptStudioView(ScriptStudioViewModel viewModel)
    {
        _vm = viewModel; DataContext = viewModel; Focusable = true; PlayerUi.Apply(this);
        _save = Button("保存", SaveAsync, true);
        _record = Button("开始录制", StartRecordingAsync, true);
        _pause = Button("暂停", async () => RenderCapture(await _vm.Client.CallAsync(_state == "paused" ? "capture_resume" : "capture_pause")));
        _stop = Button("停止并编辑", FinishRecordingAsync);
        _apply = Button("应用更改", ApplyInspectorAsync, true);
        _undo = IconButton(1, "撤销（Ctrl+Z）", async () => { if (await EnsureInspectorAsync()) await _vm.ChangeAsync("undo"); }, false);
        _redo = IconButton(2, "重做（Ctrl+Shift+Z）", async () => { if (await EnsureInspectorAsync()) await _vm.ChangeAsync("redo"); }, false);
        BuildPage();
        WireLibrarySelection(_library);
        WireLibrarySelection(_editorLibrary);
        _editorSearch.TextChanged += (_, _) => FilterLibrary();
        _editorPicker.SelectionChanged += async (_, _) =>
        {
            if (!_sync && _editorPicker.SelectedItem is JsonNode item)
                await Run(async () => await OpenLibraryFileAsync(item, true));
        };
        _vm.Changed += RenderDocument;
        _poll.Tick += async (_, _) => await PollCaptureAsync();
        _search.TextChanged += (_, _) => FilterLibrary();
        _events.SelectionMode = Avalonia.Controls.SelectionMode.Multiple | Avalonia.Controls.SelectionMode.Toggle;
        _events.SelectionChanged += async (_, _) =>
        {
            if (!_sync)
            {
                var ids = _events.SelectedItems?.OfType<StudioOperation>().Select(operation => operation.Id).ToArray() ?? [];
                await Run(async () => { if (await EnsureInspectorAsync()) SelectOperations(ids); else RestoreSelectionVisual(); });
            }
        };
        _timeline.SelectionChanged += ids => _ = Run(async () =>
        {
            if (await EnsureInspectorAsync()) SelectOperations(ids); else RestoreSelectionVisual();
        });
        _timeline.Moved += (ids, delta) => _ = Run(() => MoveSelectionAsync(ids, delta));
        _timeline.Edited += (operation, start, duration) => _ = Run(async () =>
        {
            if (!await EnsureInspectorAsync()) return;
            await _vm.ChangeAsync("edit", new JsonObject { ["id"] = operation.Id,
                ["changes"] = new JsonObject { ["start_ms"] = start, ["duration_ms"] = duration } });
            SelectById(operation.Id);
        });
        _timeline.CursorChanged += time => _cursor.Value = (decimal)(time / 1000);
        _tabs.SelectionChanged += (_, e) =>
        {
            if (ReferenceEquals(e.Source, _tabs) && _tabs.SelectedIndex == 1)
                Dispatcher.UIThread.Post(FitPendingTimeline, DispatcherPriority.Background);
        };
        _cursor.ValueChanged += (_, _) => _timeline.SetCursor(Milliseconds(_cursor));
        _scriptDuration.ValueChanged += (_, _) =>
        {
            if (_sync || _vm.Document == null) return;
            _status.Text = DurationPending
                ? $"总时长待应用：{_scriptDuration.Value:0.000} 秒；点击应用时长或保存。"
                : $"当前脚本总时长：{_vm.Duration / 1000:0.000} 秒。";
        };
        foreach (var number in new[] { _start, _duration, _key, _x, _y, _dx, _dy })
            number.ValueChanged += (_, _) => { if (!_sync) RefreshInspectorDirty(); };
        _button.SelectionChanged += (_, _) => { if (!_sync) RefreshInspectorDirty(); };
        _kind.SelectionChanged += (_, _) =>
        {
            if (_sync) return;
            UpdateInspectorFields();
            if (_kind.SelectedIndex is not (0 or 1)) _duration.Value = 0;
            RefreshInspectorDirty();
        };
        _text.TextChanged += (_, _) => { if (!_sync) RefreshInspectorDirty(); };
        _keyCapture.KeyDown += (_, e) =>
        {
            var key = StudioWindows.VirtualKeyFromKey(e.Key);
            if (key > 0) { _key.Value = key; _keyCapture.Text = StudioOperation.KeyName(key); }
            else _keyCapture.Text = "此按键暂不支持，请手动填写键码。";
            e.Handled = true;
        };
        AttachedToVisualTree += async (_, _) =>
        {
            var owner = TopLevel.GetTopLevel(this) as Window;
            if (!ReferenceEquals(owner, _owner))
            {
                if (_owner != null) _owner.Closing -= OnClosing;
                _owner = owner;
                if (_owner != null) _owner.Closing += OnClosing;
            }
            if (!_initialized)
            {
                await Run(async () =>
                {
                    RefreshWindows(); await RefreshLibraryAsync();
                    _initialized = true;
                }, allowDuringTask: true);
            }
            _poll.Start();
        };
        AddHandler(KeyDownEvent, OnShortcut, Avalonia.Interactivity.RoutingStrategies.Tunnel);
    }

    private void BuildPage()
    {
        var page = new Grid { RowDefinitions = new RowDefinitions("Auto,*,Auto"), Margin = new Thickness(16, 10), RowSpacing = 8 };
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 12 };
        var title = new StackPanel { Spacing = 2 };
        title.Children.Add(new TextBlock { Text = "录制与脚本", FontSize = 23, FontWeight = FontWeight.SemiBold });
        _documentTitle.Text = "选择脚本，或开始录制";
        _documentTitle.FontSize = 12; _documentTitle.Opacity = .7;
        _documentTitle.TextTrimming = TextTrimming.CharacterEllipsis;
        title.Children.Add(_documentTitle); header.Children.Add(title);
        var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, VerticalAlignment = VerticalAlignment.Center };
        _summary.FontSize = 12; _summary.VerticalAlignment = VerticalAlignment.Center; actions.Children.Add(_summary);
        actions.Children.Add(_save);
        var saveAs = Button("另存为…", SaveAsAsync); _documentActions.Add(saveAs); actions.Children.Add(saveAs);
        Grid.SetColumn(actions, 1); header.Children.Add(actions); page.Children.Add(header);
        _tabs.Items.Add(new TabItem { Header = "脚本库与录制", Content = BuildLibraryTab() });
        _tabs.Items.Add(new TabItem { Header = "时间轴编辑", Content = BuildEditorTab() });
        Grid.SetRow(_tabs, 1); page.Children.Add(_tabs);
        _status.Text = "双击脚本进入编辑；在脚本行中设置任务绑定。";
        _status.FontSize = 12; _status.TextWrapping = TextWrapping.Wrap; _status.Opacity = .75;
        Grid.SetRow(_status, 2); page.Children.Add(_status); Content = page;
    }

    private static NumericUpDown Number(double value, double minimum = 0, double maximum = 604800, string format = "0.000") => new()
    { Value = (decimal)value, Minimum = (decimal)minimum, Maximum = (decimal)maximum, Increment = format == "0" ? 1 : .01m, FormatString = format, MinWidth = 90, HorizontalAlignment = HorizontalAlignment.Stretch };
    private Button Button(string label, Func<Task> action, bool primary = false, bool allowDuringTask = false)
    {
        var button = new Button { Content = label, Padding = new Thickness(14, 9), MinHeight = 38 };
        if (primary) button.Classes.Add("Flat");
        button.Click += async (_, _) => await Run(action, allowDuringTask); return button;
    }
    private static WrapPanel Wrap(params Control[] controls)
    {
        var panel = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var control in controls) { control.Margin = new Thickness(0, 0, 8, 6); panel.Children.Add(control); } return panel;
    }
    private static StackPanel Field(string label, Control control)
    {
        var panel = new StackPanel { Spacing = 5 }; panel.Children.Add(new TextBlock { Text = label, Opacity = .65, FontSize = 12 }); panel.Children.Add(control); return panel;
    }
    private static Border Card(Control content)
    {
        var border = new Border { Child = content, Padding = new Thickness(16), CornerRadius = new CornerRadius(12), BorderThickness = new Thickness(1), BorderBrush = new SolidColorBrush(Color.Parse("#258497B0")) };
        border.Bind(Border.BackgroundProperty, new DynamicResourceExtension("SukiCardBackground")); return border;
    }
    private static long Milliseconds(NumericUpDown field) => (long)Math.Round((field.Value ?? 0) * 1000);
    private async Task Run(Func<Task> action, bool allowDuringTask = false)
    {
        if (_busy) return;
        if (!allowDuringTask && MaaProcessor.Processors.Any(p => p.IsTaskRunActive))
        { _status.Text = RunningHint; return; }
        _busy = true; UpdateControls();
        try { await action(); }
        catch (Exception error)
        {
            _status.Text = error.Message;
            if (_vm.Client.IsDisconnected) { _vm.Capturing = false; _state = "failed"; ScriptStudioGuard.EndCapture(); }
        }
        finally { _busy = false; UpdateControls(); }
    }
    private void RefreshWindows()
    {
        var old = (_windows.SelectedItem as StudioWindow)?.Handle;
        var windows = StudioWindows.List(); _windows.ItemsSource = windows;
        _windows.SelectedItem = windows.FirstOrDefault(w => w.Handle == old)
            ?? windows.FirstOrDefault(w => w.Handle != 0 && w.Handle == StudioWindows.SelectedHandle);
        _windows.PlaceholderText = "请选择游戏窗口";
        if (windows.Count == 0) _status.Text = "没有找到游戏窗口，请先启动造梦盒子，再刷新窗口。";
    }
    private void RenderDocument()
    {
        var selectedIds = _selection.ToArray(); _sync = true;
        if (_vm.File != _renderedFile || _vm.Name != _renderedName) { _fitNextTimeline = true; _adding = false; }
        _renderedName = _vm.Name; _renderedFile = _vm.File;
        _documentTitle.Text = _vm.Document == null ? "选择脚本，或开始录制" : _vm.Name + (_vm.Dirty ? " · 未保存" : "");
        _summary.Text = _vm.Document == null ? "" : $"{_vm.Duration / 1000:0.000} 秒 · {_vm.Operations.Count} 个操作" + (_vm.Dirty ? " · 未保存" : " · 已保存");
        _diagnostics.Text = string.Join("\n", _vm.Diagnostics.Take(3));
        _timeline.Update(_vm.Operations.ToList(), _vm.Duration);
        _scriptDuration.Value = (decimal)(_vm.Duration / 1000);
        _cursor.Maximum = (decimal)(_vm.Duration / 1000);
        _cursor.Value = (decimal)(_timeline.CursorMs / 1000);
        _sync = false;
        _inspectorDirty = false;
        SelectOperations(selectedIds); FilterLibrary(); UpdateControls();
        if (_tabs.SelectedIndex == 1) Dispatcher.UIThread.Post(FitPendingTimeline, DispatcherPriority.Background);
    }
    private void FitPendingTimeline()
    {
        if (!_fitNextTimeline || _timelineScroll.Viewport.Width < 150) return;
        _timeline.Fit(_timelineScroll.Viewport.Width); _fitNextTimeline = false;
    }
    private void SelectById(string? id)
    {
        var operation = _vm.Operations.FirstOrDefault(o => o.Id == id);
        if (operation != null) SelectOperation(operation); else { _selected = null; _inspector.IsEnabled = false; }
    }
    private void RestoreSelectionVisual()
    {
        SelectOperations(_selection.ToArray());
    }
    private void SelectOperation(StudioOperation operation)
    {
        _sync = true;
        try
        {
        _selection.Clear(); _selection.Add(operation.Id);
        _selected = operation; _adding = false; _inspector.IsEnabled = true;
        _events.SelectedItem = operation; _timeline.SelectedId = operation.Id; _timeline.InvalidateVisual();
        _kind.SelectedIndex = operation.Kind switch { "key_hold" => 0, "mouse_hold" => 1, "key" => 2, "click" => 3, "move" => 4, "scroll" => 5, "text" => 6, "marker" => 7, _ => 1 };
        _kind.IsEnabled = false;
        _start.Value = (decimal)(operation.Start / 1000); _duration.Value = (decimal)(operation.Duration / 1000);
        _key.Value = StudioOperation.Integer(operation.Data["key"], 74);
        _keyCapture.Text = StudioOperation.KeyName((int)(_key.Value ?? 74));
        _x.Value = StudioOperation.Integer(operation.Data["x"]); _y.Value = StudioOperation.Integer(operation.Data["y"]);
        _dx.Value = StudioOperation.Integer(operation.Data["dx"]); _dy.Value = StudioOperation.Integer(operation.Data["dy"]);
        _button.SelectedIndex = StudioOperation.String(operation.Data["button"]) switch { "right" => 1, "middle" => 2, _ => 0 };
        _text.Text = operation.Kind == "marker" ? StudioOperation.String(operation.Data["label"]) : StudioOperation.String(operation.Data["text"]);
        UpdateInspectorFields();
        RememberInspectorBaseline();
        }
        finally { _sync = false; }
    }
    private void BeginInsert()
    {
        if (_vm.Document == null) throw new InvalidOperationException("请先选择脚本或新建空白脚本。");
        _selection.Clear(); _timeline.SetSelection([]);
        _sync = true; _selected = null; _adding = true; _inspector.IsEnabled = true; _kind.IsEnabled = true;
        _kind.SelectedIndex = 1; _start.Value = _cursor.Value; _duration.Value = .05m; _text.Text = "";
        UpdateInspectorFields();
        RememberInspectorBaseline(); _sync = false;
    }
    private JsonObject ReadInspectorValues()
    {
        var kind = new[] { "key_hold", "mouse_hold", "key", "click", "move", "scroll", "text", "marker" }[Math.Clamp(_kind.SelectedIndex, 0, 7)];
        var changes = new JsonObject { ["kind"] = kind, ["start_ms"] = Milliseconds(_start), ["duration_ms"] = kind is "key_hold" or "mouse_hold" ? Milliseconds(_duration) : 0 };
        if (kind is "key_hold" or "key") changes["key"] = (int)(_key.Value ?? 74);
        if (kind is "mouse_hold" or "click" or "move") { changes["x"] = (int)(_x.Value ?? 0); changes["y"] = (int)(_y.Value ?? 0); }
        if (kind == "mouse_hold") changes["button"] = new[] { "left", "right", "middle" }[Math.Max(0, _button.SelectedIndex)];
        if (kind == "scroll") { changes["dx"] = (int)(_dx.Value ?? 0); changes["dy"] = (int)(_dy.Value ?? 0); }
        if (kind == "text") changes["text"] = _text.Text ?? "";
        if (kind == "marker") changes["label"] = _text.Text ?? "阶段标记";
        return changes;
    }
    private void RememberInspectorBaseline()
    {
        _inspectorBaseline = ReadInspectorValues().ToJsonString();
        _inspectorDirty = false;
    }
    private bool RefreshInspectorDirty()
    {
        if (_sync) return _inspectorDirty;
        // Compare the same normalized, kind-specific values that an apply would send.
        // Deferred control events and equivalent decimal formatting are not edits.
        _inspectorDirty = (_selected != null || _adding) &&
            !string.Equals(ReadInspectorValues().ToJsonString(), _inspectorBaseline, StringComparison.Ordinal);
        return _inspectorDirty;
    }
    private async Task ApplyInspectorAsync()
    {
        var changes = ReadInspectorValues();
        var kind = changes["kind"]!.GetValue<string>();
        changes.Remove("kind");
        if (_adding)
        {
            var previousIds = _vm.Operations.Select(operation => operation.Id).ToHashSet();
            changes["kind"] = kind;
            await _vm.ChangeAsync("insert", new JsonObject { ["changes"] = changes });
            var added = _vm.Operations.FirstOrDefault(operation => !previousIds.Contains(operation.Id));
            if (added != null) SelectOperation(added);
        }
        else if (_selected != null) await _vm.ChangeAsync("edit", new JsonObject { ["id"] = _selected.Id, ["changes"] = changes });
        RememberInspectorBaseline(); _status.Text = "操作已应用，保存脚本后即可用于任务。";
    }
    private void UpdateInspectorFields()
    {
        var kind = _kind.SelectedIndex;
        if (_kind.Parent is Control typeField) typeField.IsVisible = _adding;
        if (_keyFields != null) _keyFields.IsVisible = kind is 0 or 2;
        if (_mouseFields != null) _mouseFields.IsVisible = kind is 1 or 3 or 4;
        if (_scrollFields != null) _scrollFields.IsVisible = kind == 5;
        if (_text.Parent is Control textField) textField.IsVisible = kind is 6 or 7;
        _text.MinHeight = kind == 7 ? 38 : 70;
        _duration.IsEnabled = kind is 0 or 1;
        _key.IsEnabled = _keyCapture.IsEnabled = kind is 0 or 2;
        _button.IsEnabled = kind == 1;
        _x.IsEnabled = _y.IsEnabled = kind is 1 or 3 or 4;
        _dx.IsEnabled = _dy.IsEnabled = kind == 5;
        _text.IsEnabled = kind is 6 or 7;
    }
    private async Task<bool> EnsureInspectorAsync()
    {
        if (!RefreshInspectorDirty()) return true;
        var choice = await AskAsync("操作参数尚未应用", "先应用填写的参数，或还原填写后继续。", "应用", "还原");
        if (choice == 0) return false;
        if (choice == 1) await ApplyInspectorAsync();
        else if (_selected != null) SelectOperation(_selected);
        else BeginInsert();
        return true;
    }
    private bool DurationPending => _vm.Document != null && Milliseconds(_scriptDuration) != (long)Math.Round(_vm.Duration);
    private async Task ApplyPendingEditsAsync()
    {
        var pendingDuration = DurationPending;
        var requestedDuration = Milliseconds(_scriptDuration);
        if (RefreshInspectorDirty()) await ApplyInspectorAsync();
        if (pendingDuration)
        {
            // Applying operation parameters refreshes the document and controls.
            // Preserve the separately entered total duration across that refresh.
            _scriptDuration.Value = requestedDuration / 1000m;
            await _vm.ChangeAsync("set_duration", new JsonObject { ["duration_ms"] = requestedDuration });
        }
    }
    private bool HasChanges => _vm.Dirty || RefreshInspectorDirty() || DurationPending;
    private async Task<bool> EnsureSavedAsync()
    {
        if (!HasChanges) return true;
        var choice = await AskAsync("保留当前脚本修改", "当前修改尚未保存。", "保存", "不保存");
        if (choice == 0) return false;
        if (choice == 1) return await SaveDocumentAsync(false);
        else
        {
            if (_vm.Client.IsDisconnected) _vm.Apply(new JsonObject { ["empty"] = true });
            else await _vm.ChangeAsync("discard");
            _inspectorDirty = false;
        }
        return true;
    }
    private async Task ImportAsync()
    {
        if (!await EnsureSavedAsync()) return;
        var storage = TopLevel.GetTopLevel(this)?.StorageProvider; if (storage == null) return;
        var files = await storage.OpenFilePickerAsync(new FilePickerOpenOptions { Title = "导入录制脚本", AllowMultiple = false, FileTypeFilter = new[] { JsonFiles } });
        var path = files.FirstOrDefault()?.TryGetLocalPath(); if (path == null) return;
        await _vm.ChangeAsync("import", new JsonObject { ["path"] = path }); _status.Text = "脚本已导入为草稿，保存后进入脚本库。";
    }
    private static FilePickerFileType JsonFiles => new("录制脚本 JSON") { Patterns = new[] { "*.json" } };
    private async Task ExportAsync()
    {
        await ApplyPendingEditsAsync();
        var storage = TopLevel.GetTopLevel(this)?.StorageProvider; if (storage == null || _vm.Document == null) return;
        var file = await storage.SaveFilePickerAsync(new FilePickerSaveOptions { Title = "导出录制脚本", SuggestedFileName = _vm.Name + ".json", DefaultExtension = "json", FileTypeChoices = new[] { JsonFiles }, ShowOverwritePrompt = true });
        var path = file?.TryGetLocalPath(); if (path != null) { await _vm.Client.CallAsync("export", new JsonObject { ["path"] = path }); _status.Text = "脚本已导出。"; }
    }
    private async Task StartRecordingAsync()
    {
        if (!await EnsureSavedAsync()) return;
        if (_windows.SelectedItem is not StudioWindow window) throw new InvalidOperationException("请先选择游戏窗口。");
        _recordingName = "录制-" + DateTime.Now.ToString("MMdd-HHmmss", CultureInfo.InvariantCulture);
        ScriptStudioGuard.BeginCapture();
        try
        {
            var snapshot = await _vm.Client.CallAsync("capture_start", new JsonObject { ["hwnd"] = (long)window.Handle, ["countdown_seconds"] = (int)(_countdown.Value ?? 3) });
            _vm.Capturing = true; RenderCapture(snapshot);
            _status.Text = "请切回游戏。F8 暂停 / 继续，F9 停止，F10 停止并保存。"; _poll.Start();
        }
        catch { ScriptStudioGuard.EndCapture(); throw; }
    }
    private void RenderCapture(JsonNode snapshot)
    {
        _state = snapshot["state"]?.GetValue<string>() ?? "idle";
        _captureState.Text = StudioCapturePresentation.Title(snapshot.AsObject());
        _captureHint.Text = snapshot["message"]?.GetValue<string>() ?? "";
        UpdateControls();
    }
    private async Task PollCaptureAsync()
    {
        UpdateControls();
        if (_polling || _busy || !_vm.Capturing) return;
        _polling = true;
        try
        {
            var snapshot = await _vm.Client.CallAsync("capture_status"); RenderCapture(snapshot);
            if (_state is "stopped" or "failed" or "closed")
            {
                await FinishRecordingAsync();
                if (!string.IsNullOrEmpty(snapshot["error"]?.GetValue<string>())) _status.Text = snapshot["error"]!.GetValue<string>();
                if (snapshot["save_requested"]?.GetValue<bool>() == true) await SaveAsync();
            }
        }
        catch (Exception error)
        {
            _status.Text = error.Message;
            if (_vm.Client.IsDisconnected) { _vm.Capturing = false; _state = "failed"; ScriptStudioGuard.EndCapture(); }
        }
        finally { _polling = false; UpdateControls(); }
    }
    private async Task FinishRecordingAsync()
    {
        if (!_vm.Capturing) return;
        if (_vm.Client.IsDisconnected) { _vm.Capturing = false; _state = "failed"; ScriptStudioGuard.EndCapture(); return; }
        if (_state == "countdown")
        {
            await _vm.Client.CallAsync("capture_cancel"); _vm.Capturing = false; _state = "idle";
            ScriptStudioGuard.EndCapture(); _captureState.Text = "准备就绪"; _captureHint.Text = "已取消倒计时，当前脚本保持原样。"; return;
        }
        await _vm.ChangeAsync("capture_stop"); _vm.Capturing = false; ScriptStudioGuard.EndCapture();
        _state = "stopped"; _captureState.Text = "录制完成"; _captureHint.Text = "草稿已送到时间轴，可检查后保存。";
        if (_libraryNarrow && _recordingExpander != null) _recordingExpander.IsExpanded = false;
        await _vm.ChangeAsync("rename", new JsonObject { ["name"] = _recordingName });
        _tabs.SelectedIndex = 1; _timeline.Fit(_timelineScroll.Bounds.Width);
        _status.Text = "录制已停止，内容保留为草稿。可以编辑、检查后保存。";
    }
    private async Task DeleteAsync()
    {
        if (!await EnsureInspectorAsync() || _selection.Count == 0) return;
        var count = _selection.Count;
        await _vm.ChangeAsync("delete_many", new JsonObject { ["ids"] = SelectionJson(_selection) });
        _status.Text = $"已删除 {count} 个操作，可用 Ctrl+Z 一次撤销。";
    }
    private async Task SplitAsync()
    {
        if (!await EnsureInspectorAsync()) return;
        var result = await _vm.Client.CallAsync("split", new JsonObject { ["at_ms"] = Milliseconds(_cursor) });
        await RefreshLibraryAsync(); _status.Text = "已在键鼠全部释放的位置拆分，生成：" + string.Join("、", result["files"]!.AsArray().Select(n => n!.GetValue<string>()));
    }
    private Task ValidateAsync()
    {
        _status.Text = _vm.Document == null ? "请先选择脚本。" : _vm.Diagnostics.Count == 0 ? $"离线检查通过：{_vm.Operations.Count} 个操作，总时长 {_vm.Duration / 1000:0.000} 秒。" : string.Join("；", _vm.Diagnostics);
        return Task.CompletedTask;
    }
    private void UpdateControls()
    {
        var tasks = MaaProcessor.Processors.Any(p => p.IsTaskRunActive);
        var editable = !_busy && !_vm.Capturing && !tasks;
        _save.IsEnabled = editable && _vm.Document != null;
        _record.IsEnabled = !_busy && !_vm.Capturing && !tasks;
        _pause.IsEnabled = !_busy && _vm.Capturing && _state is "recording" or "paused" or "resume_countdown";
        _pause.Content = _state == "paused" ? "继续（3秒准备）" : _state == "resume_countdown" ? "取消继续" : "暂停";
        _stop.IsEnabled = !_busy && _vm.Capturing;
        _stop.Content = _state == "countdown" ? "取消倒计时" : "停止并编辑";
        _windows.IsEnabled = _countdown.IsEnabled = editable;
        if (_refreshWindows != null) _refreshWindows.IsEnabled = !_busy && !_vm.Capturing;
        if (_mark != null) _mark.IsEnabled = !_busy && _vm.Capturing && _state is "recording" or "paused";
        _library.IsEnabled = !_busy && !_vm.Capturing;
        _editorLibrary.IsEnabled = _editorPicker.IsEnabled = !_busy && !_vm.Capturing;
        _timeline.Editable = editable; _events.IsEnabled = editable;
        _inspector.IsEnabled = editable && (_selected != null || _adding);
        _apply.IsEnabled = editable && (_selected != null || _adding);
        _undo.IsEnabled = editable && _vm.Document?["can_undo"]?.GetValue<bool>() == true;
        _redo.IsEnabled = editable && _vm.Document?["can_redo"]?.GetValue<bool>() == true;
        foreach (var action in _documentActions) action.IsEnabled = editable && _vm.Document != null;
        _scriptDuration.IsEnabled = editable && _vm.Document != null;
        foreach (var action in _operationActions) action.IsEnabled = editable && _selection.Count > 0;
        if (_duplicateOperationButton != null) _duplicateOperationButton.IsEnabled = editable && _selected != null;
        _batchInspector.IsVisible = _selection.Count > 1;
        _batchInspectorHost.IsVisible = _batchInspector.IsVisible;
        if (_singleApplyRow != null) _singleApplyRow.IsVisible = _selected != null || _adding;
        _batchInspector.IsEnabled = editable;
        _selectionSummary.Text = _selection.Count == 0 ? "未选择操作" : $"已选择 {_selection.Count} 个操作";
        _inspector.IsVisible = _selected != null || _adding;
        _emptyInspector.IsVisible = !_inspector.IsVisible && !_batchInspector.IsVisible;
        _deleteFile.IsEnabled = editable && CurrentLibraryItem != null;
        if (!tasks && _status.Text == RunningHint) _status.Text = "任务已停止，可以编辑、绑定或录制脚本。";
    }
    private async void OnShortcut(object? sender, KeyEventArgs e)
    {
        if (_vm.Capturing) return;
        var typing = e.Source is TextBox || e.Source is Control field && field.GetVisualAncestors().OfType<TextBox>().Any();
        if (!typing && _tabs.SelectedIndex == 1 && e.Key == Key.Delete) { e.Handled = true; await Run(DeleteAsync); return; }
        if (!typing && _tabs.SelectedIndex == 1 && e.Key == Key.A && e.KeyModifiers.HasFlag(KeyModifiers.Control))
        { e.Handled = true; await Run(async () => { if (await EnsureInspectorAsync()) SelectOperations(_vm.Operations.Select(operation => operation.Id)); }); return; }
        if (!typing && _tabs.SelectedIndex == 1 && e.Key is Key.Left or Key.Right && _selection.Count > 0
            && (e.Source is ScriptTimeline || e.Source is Control item && item.GetVisualAncestors().Contains(_events)))
        {
            e.Handled = true;
            var delta = (e.Key == Key.Left ? -1 : 1) * (e.KeyModifiers.HasFlag(KeyModifiers.Shift) ? 100 : 10);
            var earliest = _vm.Operations.Where(operation => _selection.Contains(operation.Id)).Min(operation => operation.Start);
            await Run(() => MoveSelectionAsync(_selection.ToArray(), Math.Max(delta, -earliest))); return;
        }
        if (!e.KeyModifiers.HasFlag(KeyModifiers.Control)) return;
        if (e.Key == Key.N) { e.Handled = true; await Run(NewScriptAsync); return; }
        if (!typing && _tabs.SelectedIndex == 1 && e.Key == Key.D) { e.Handled = true; await Run(DuplicateOperationAsync); return; }
        if (e.Key == Key.S) { e.Handled = true; await Run(e.KeyModifiers.HasFlag(KeyModifiers.Shift) ? SaveAsAsync : SaveAsync); }
        else if (e.Key is Key.Z or Key.Y && e.Source is not TextBox && !(e.Source is Control input && input.GetVisualAncestors().OfType<TextBox>().Any()))
        { e.Handled = true; await Run(async () => { if (await EnsureInspectorAsync()) await _vm.ChangeAsync(e.Key == Key.Y || e.KeyModifiers.HasFlag(KeyModifiers.Shift) ? "redo" : "undo"); }); }
    }
    private async Task<int> AskAsync(string title, string message, string primary, string secondary)
    {
        if (TopLevel.GetTopLevel(this) is not Window owner) return 0;
        var dialog = new Window { Title = title, Width = 440, SizeToContent = SizeToContent.Height, CanResize = false,
            ShowInTaskbar = false, WindowStartupLocation = WindowStartupLocation.CenterOwner };
        var content = new StackPanel { Spacing = 20, Margin = new Thickness(24) };
        content.Children.Add(new TextBlock { Text = message, TextWrapping = TextWrapping.Wrap });
        var buttons = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Right };
        foreach (var item in new[] { (primary, 1), (secondary, 2), ("取消", 0) })
        { var button = new Button { Content = item.Item1, Margin = new Thickness(4), Padding = new Thickness(12, 8) }; var result = item.Item2; button.Click += (_, _) => dialog.Close(result); buttons.Children.Add(button); }
        content.Children.Add(buttons); dialog.Content = content; return await ShowStudioDialogAsync<int>(dialog, owner);
    }
    private async void OnClosing(object? sender, WindowClosingEventArgs e)
    {
        if (_allowClose) return;
        if (_vm.Capturing || HasChanges)
        {
            e.Cancel = true;
            await Run(async () =>
            {
                if (_vm.Capturing) await FinishRecordingAsync();
                if (!await EnsureSavedAsync()) return;
                _allowClose = true; _poll.Stop(); _vm.Dispose(); _owner?.Close();
            });
        }
        else { _allowClose = true; _poll.Stop(); _vm.Dispose(); }
    }
}
