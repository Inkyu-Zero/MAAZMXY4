using System;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Automation;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using MFAAvalonia.Controls;
using MFAAvalonia.ViewModels.Pages;

namespace MFAAvalonia.Views.Pages;

public sealed partial class ScriptStudioView
{
    private Grid? _editorShell;
    private Control? _editorLibraryHost;
    private GridSplitter? _librarySplitter;
    private double _editorWidth;
    private readonly TextBlock _emptyInspector = new();

    private Button IconButton(int icon, string name, Func<Task> action, bool documentAction = true)
    {
        var button = Button(name, action);
        button.Content = new StudioToolbarIcon(icon); button.Width = button.Height = 36;
        button.MinHeight = 32; button.Padding = new Thickness(5);
        button.Background = new SolidColorBrush(Color.Parse("#202733"));
        ToolTip.SetTip(button, name); ToolTip.SetShowDelay(button, 350);
        AutomationProperties.SetName(button, name);
        if (documentAction) _documentActions.Add(button);
        return button;
    }

    private Control BuildEditorTab()
    {
        var layout = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,*"), RowSpacing = 7, Margin = new Thickness(0, 8, 0, 0) };
        var pickerRow = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto"), ColumnSpacing = 10 };
        _editorPicker.ItemTemplate = new FuncDataTemplate<JsonNode>((item, _) => new TextBlock { Text = item?["name"]?.GetValue<string>(), TextTrimming = TextTrimming.CharacterEllipsis });
        pickerRow.Children.Add(_editorPicker);
        var help = new TextBlock { Text = "空白处框选 · Ctrl+单击多选 · 拖动整体移动 · Delete 删除", FontSize = 12, Opacity = .75, VerticalAlignment = VerticalAlignment.Center, TextWrapping = TextWrapping.Wrap };
        Grid.SetColumn(help, 1); pickerRow.Children.Add(help);
        var export = Button("导出…", ExportAsync); _documentActions.Add(export); Grid.SetColumn(export, 2); pickerRow.Children.Add(export);
        layout.Children.Add(pickerRow);
        _focusTimelineButton = IconButton(15, "最大化时间轴", async () => { ToggleTimelineFocus(); await Task.CompletedTask; }, false);
        _cursor.Width = 105; _cursor.MinWidth = 95; _cursor.Height = 32; _cursor.MinHeight = 32;
        var remove = IconButton(4, "删除操作（Delete）", DeleteAsync); _operationActions.Add(remove);
        var duplicate = IconButton(5, "复制操作（Ctrl+D）", DuplicateOperationAsync); _operationActions.Add(duplicate);
        _duplicateOperationButton = duplicate;
        var toolbar = Wrap(IconButton(0, "保存（Ctrl+S）", SaveAsync), _undo, _redo,
            IconButton(3, "添加操作", async () => { if (await EnsureInspectorAsync()) BeginInsert(); }), remove, duplicate,
            IconButton(6, "添加标记", async () => { if (await EnsureInspectorAsync()) await _vm.ChangeAsync("marker", new JsonObject { ["at_ms"] = Milliseconds(_cursor), ["label"] = "阶段标记" }); }),
            IconButton(7, "分割脚本：在当前位置生成两个文件", SplitAsync),
            IconButton(8, "缩小时间轴", async () => { _timeline.Zoom(.8); await Task.CompletedTask; }, false),
            IconButton(9, "放大时间轴", async () => { _timeline.Zoom(1.25); await Task.CompletedTask; }, false),
            IconButton(10, "适合窗口", async () => { _timeline.Fit(_timelineScroll.Viewport.Width); await Task.CompletedTask; }, false),
            _focusTimelineButton,
            ToolbarGroup(new TextBlock { Text = "位置（秒）", Foreground = Brushes.LightGray, FontSize = 11, VerticalAlignment = VerticalAlignment.Center }, _cursor));
        var extras = new Button { Content = "更多", Padding = new Thickness(9, 5), MinHeight = 32, Foreground = Brushes.White, Background = new SolidColorBrush(Color.Parse("#202733")) };
        _scriptDuration.Width = 145; _scriptDuration.Height = 32; _scriptDuration.MinHeight = 32;
        ToolTip.SetTip(_scriptDuration, "输入总时长，点击应用时长或保存；停止标记同步移动，现有动作不会被截断。");
        var applyDuration = Button("应用时长", async () =>
        {
            var duration = Milliseconds(_scriptDuration);
            if (await EnsureInspectorAsync())
            {
                await _vm.ChangeAsync("set_duration", new JsonObject { ["duration_ms"] = duration });
                _status.Text = $"脚本总时长已应用：{_vm.Duration / 1000:0.000} 秒；保存后用于回放。";
            }
        });
        applyDuration.Foreground = Brushes.White;
        applyDuration.Background = new SolidColorBrush(Color.Parse("#202733"));
        applyDuration.Padding = new Thickness(9, 5); applyDuration.MinHeight = 32;
        _documentActions.Add(applyDuration);
        toolbar.Children.Add(ToolbarGroup(new TextBlock { Text = "总时长（秒）", Foreground = Brushes.LightGray,
            FontSize = 11, VerticalAlignment = VerticalAlignment.Center }, _scriptDuration, applyDuration));
        var menu = new MenuFlyout();
        foreach (var option in new (string Label, Func<Task> Run)[] {
            ("检查脚本", ValidateAsync),
            ("修复未释放的按键或鼠标", async () => { if (await EnsureInspectorAsync()) await _vm.ChangeAsync("repair"); }),
            ("增大轨道高度", async () => { _timeline.AdjustTrackHeight(8); await Task.CompletedTask; }),
            ("减小轨道高度", async () => { _timeline.AdjustTrackHeight(-8); await Task.CompletedTask; }) })
        {
            var item = new MenuItem { Header = option.Label }; item.Click += async (_, _) => await Run(option.Run); menu.Items.Add(item);
        }
        extras.Flyout = menu; toolbar.Children.Add(extras);
        foreach (var control in toolbar.Children) control.Margin = new Thickness(0, 0, 4, 0);
        var bar = new Border { Child = toolbar, Background = new SolidColorBrush(Color.Parse("#202733")), CornerRadius = new CornerRadius(7), Padding = new Thickness(6) };
        Grid.SetRow(bar, 1); layout.Children.Add(bar);
        _editorShell = new Grid { ColumnDefinitions = new ColumnDefinitions("200,8,*") };
        _editorLibraryHost = Card(BuildLibraryPane(_editorLibrary, _editorSearch, true));
        _editorLibraryHost.MinWidth = 150; _editorShell.Children.Add(_editorLibraryHost);
        _librarySplitter = new GridSplitter { ResizeDirection = GridResizeDirection.Columns, ResizeBehavior = GridResizeBehavior.PreviousAndNext,
            HorizontalAlignment = HorizontalAlignment.Stretch, VerticalAlignment = VerticalAlignment.Stretch, Background = new SolidColorBrush(Color.Parse("#188497B0")) };
        Grid.SetColumn(_librarySplitter, 1); _editorShell.Children.Add(_librarySplitter);
        _editorMain = new Grid { ColumnDefinitions = new ColumnDefinitions("*,8,260") };
        _editorMain.ColumnDefinitions[0].MinWidth = 240;
        _editorMain.ColumnDefinitions[2].MinWidth = 230;
        var timelineArea = new Grid { RowDefinitions = new RowDefinitions("Auto,*,Auto"), RowSpacing = 8 };
        timelineArea.Children.Add(Wrap(_selectionSummary,
            Button("全选", async () => { if (await EnsureInspectorAsync()) SelectOperations(_vm.Operations.Select(operation => operation.Id)); }),
            Button("取消选择", async () => { if (await EnsureInspectorAsync()) SelectOperations([]); })));
        _timelineScroll.Content = _timeline; _timelineScroll.MinHeight = 180;
        _timelineScroll.VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto;
        _timelineScroll.PropertyChanged += (_, change) =>
        {
            if (change.Property == ScrollViewer.OffsetProperty) { _timeline.InvalidateVisual(); return; }
            if (change.Property != ScrollViewer.ViewportProperty) return;
            _timeline.SetViewport(_timelineScroll.Viewport.Width, _timelineScroll.Viewport.Height);
            FitPendingTimeline();
        };
        var timelineCard = Card(_timelineScroll); timelineCard.Padding = new Thickness(0); Grid.SetRow(timelineCard, 1); timelineArea.Children.Add(timelineCard);

        var list = new Grid { RowDefinitions = new RowDefinitions("*,Auto"), RowSpacing = 6, Height = 160 };
        _events.ItemsSource = _vm.Operations;
        _events.ItemTemplate = new FuncDataTemplate<StudioOperation>((operation, _) =>
        {
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("85,*,90"), Margin = new Thickness(4, 6) };
            if (operation == null) return row;
            row.Children.Add(new TextBlock { Text = operation.TimeText, FontFamily = FontFamily.Parse("Consolas"), Opacity = .7 });
            var label = new TextBlock { Text = operation.Label, TextTrimming = TextTrimming.CharacterEllipsis }; Grid.SetColumn(label, 1); row.Children.Add(label);
            var duration = new TextBlock { Text = operation.DurationText, Opacity = .6, HorizontalAlignment = HorizontalAlignment.Right }; Grid.SetColumn(duration, 2); row.Children.Add(duration); return row;
        });
        list.Children.Add(_events); Grid.SetRow(_diagnostics, 1); list.Children.Add(_diagnostics);
        _operationList = new Expander { Header = "操作列表与检查结果", Content = list, IsExpanded = false,
            HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Stretch,
            Margin = new Thickness(0, 8, 0, 0) };
        Grid.SetRow(_operationList, 2); timelineArea.Children.Add(_operationList); _editorMain.Children.Add(timelineArea);

        _inspector.Children.Add(new TextBlock { Text = "操作参数", FontSize = 17, FontWeight = FontWeight.SemiBold });
        _inspector.Children.Add(Field("操作类型", _kind));
        var timing = new Grid { ColumnDefinitions = new ColumnDefinitions("*,10,*") }; timing.Children.Add(Field("开始（秒）", _start)); var holdField = Field("持续（秒）", _duration); Grid.SetColumn(holdField, 2); timing.Children.Add(holdField); _inspector.Children.Add(timing);
        var keys = new Grid { ColumnDefinitions = new ColumnDefinitions("*,10,*") }; keys.Children.Add(Field("按键", _keyCapture)); var keyCode = Field("按键码", _key); Grid.SetColumn(keyCode, 2); keys.Children.Add(keyCode); _keyFields = keys; _inspector.Children.Add(keys);
        var mouseFields = new StackPanel { Spacing = 8 };
        mouseFields.Children.Add(Field("鼠标按钮", _button));
        var xy = new Grid { ColumnDefinitions = new ColumnDefinitions("*,8,* ") };
        xy.Children.Add(Field("X 坐标", _x)); var yf = Field("Y 坐标", _y); Grid.SetColumn(yf, 2); xy.Children.Add(yf);
        mouseFields.Children.Add(xy); _mouseFields = mouseFields; _inspector.Children.Add(mouseFields);
        _scrollFields = Field("横向 / 纵向滚轮", Wrap(_dx, _dy)); _inspector.Children.Add(_scrollFields); _inspector.Children.Add(Field("文本 / 标记", _text));
        var inspectorBody = new Grid { RowDefinitions = new RowDefinitions("*,Auto"), RowSpacing = 8 };
        inspectorBody.Children.Add(new ScrollViewer { Content = _inspector });
        _emptyInspector.Text = "选择时间轴中的操作后，在这里调整时间、按键或坐标。";
        _emptyInspector.TextWrapping = TextWrapping.Wrap; _emptyInspector.Opacity = .6;
        _emptyInspector.VerticalAlignment = VerticalAlignment.Center;
        inspectorBody.Children.Add(_emptyInspector);
        BuildBatchInspector();
        _batchInspectorHost.Content = _batchInspector;
        inspectorBody.Children.Add(_batchInspectorHost);
        var applyRow = Wrap(_apply, Button("还原参数", async () => { if (_selected != null) SelectOperation(_selected); else BeginInsert(); await Task.CompletedTask; })); Grid.SetRow(applyRow, 1); inspectorBody.Children.Add(applyRow);
        _singleApplyRow = applyRow;
        _inspectorPane = Card(inspectorBody); Grid.SetColumn(_inspectorPane, 2); _editorMain.Children.Add(_inspectorPane);
        _editorSplitter = new GridSplitter { ResizeDirection = GridResizeDirection.Columns, ResizeBehavior = GridResizeBehavior.PreviousAndNext,
            HorizontalAlignment = HorizontalAlignment.Stretch, VerticalAlignment = VerticalAlignment.Stretch,
            Background = new SolidColorBrush(Color.Parse("#188497B0")), KeyboardIncrement = 16 };
        ToolTip.SetTip(_editorSplitter, "拖动调整时间轴与参数区的宽度"); Grid.SetColumn(_editorSplitter, 1); _editorMain.Children.Add(_editorSplitter);
        Grid.SetColumn(_editorMain, 2); _editorShell.Children.Add(_editorMain);
        Grid.SetRow(_editorShell, 2); layout.Children.Add(_editorShell);
        layout.SizeChanged += (_, _) => UpdateEditorLayout(layout.Bounds.Width);
        UpdateInspectorFields(); return layout;
    }

    private void UpdateEditorLayout(double width)
    {
        _editorWidth = width;
        if (_editorShell == null || _editorLibraryHost == null || _librarySplitter == null) return;
        var expanded = width >= 1100 && !_timelineFocused;
        _editorLibraryHost.IsVisible = _librarySplitter.IsVisible = expanded;
        _editorShell.ColumnDefinitions[0].Width = new GridLength(expanded ? 200 : 0);
        _editorShell.ColumnDefinitions[1].Width = new GridLength(expanded ? 8 : 0);
        _editorPicker.IsVisible = !expanded;
    }

    private static StackPanel ToolbarGroup(params Control[] children)
    {
        var group = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
        foreach (var child in children) group.Children.Add(child);
        return group;
    }

    private void ToggleTimelineFocus()
    {
        if (_editorMain == null || _inspectorPane == null || _editorSplitter == null || _operationList == null) return;
        _timelineFocused = !_timelineFocused;
        if (_timelineFocused) _inspectorWidth = _editorMain.ColumnDefinitions[2].Width;
        _editorMain.ColumnDefinitions[2].MinWidth = _timelineFocused ? 0 : 230;
        _editorMain.ColumnDefinitions[2].Width = _timelineFocused ? new GridLength(0) : _inspectorWidth;
        _editorMain.ColumnDefinitions[1].Width = new GridLength(_timelineFocused ? 0 : 8);
        _inspectorPane.IsVisible = _editorSplitter.IsVisible = _operationList.IsVisible = !_timelineFocused;
        var label = _timelineFocused ? "还原工作区" : "最大化时间轴";
        ToolTip.SetTip(_focusTimelineButton!, label); AutomationProperties.SetName(_focusTimelineButton!, label);
        UpdateEditorLayout(_editorWidth);
        Dispatcher.UIThread.Post(() => _timeline.SetViewport(_timelineScroll.Viewport.Width, _timelineScroll.Viewport.Height), DispatcherPriority.Background);
    }

    private async Task DuplicateOperationAsync()
    {
        if (_selected == null || !await EnsureInspectorAsync()) return;
        var changes = _selected.Data.DeepClone().AsObject(); changes.Remove("id");
        var at = Math.Max(Milliseconds(_cursor), _selected.Start + _selected.Duration + 1);
        changes["start_ms"] = at;
        await _vm.ChangeAsync("insert", new JsonObject { ["changes"] = changes });
        var inserted = _vm.Operations.LastOrDefault(operation => Math.Abs(operation.Start - at) < .1);
        if (inserted != null) SelectOperation(inserted);
        _status.Text = "已复制操作，可拖动调整位置。";
    }
}
