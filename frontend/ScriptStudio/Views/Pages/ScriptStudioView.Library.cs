using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Extensions.MaaFW;

namespace MFAAvalonia.Views.Pages;

public sealed partial class ScriptStudioView
{
    private readonly TextBlock _documentTitle = new(), _libraryCount = new();
    private readonly ListBox _editorLibrary = new() { ClipToBounds = true };
    private readonly TextBox _editorSearch = new() { Watermark = "搜索脚本" };
    private readonly ComboBox _editorPicker = new() { MinWidth = 160, MaxWidth = 330, HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly List<Button> _documentActions = new(), _operationActions = new();
    private JsonArray _availableTasks = new();
    private string? _bindingRevision;
    private Button _deleteFile = null!;
    private Expander? _recordingExpander;
    private bool _libraryNarrow;
    private JsonNode? CurrentLibraryItem => _files.FirstOrDefault(item => item?["file"]?.GetValue<string>() == _vm.File);

    private Control BuildLibraryTab()
    {
        var layout = new Grid { ColumnDefinitions = new ColumnDefinitions("*,12,300"), Margin = new Thickness(0, 8, 0, 0) };
        var library = Card(BuildLibraryPane(_library, _search, false));
        library.Padding = new Thickness(12); layout.Children.Add(library);
        var recording = new StackPanel { Spacing = 10 };
        recording.Children.Add(new TextBlock { Text = "倒计时结束前切回游戏，失焦会自动暂停。", TextWrapping = TextWrapping.Wrap, Opacity = .65, FontSize = 12 });
        var target = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 6 };
        _windows.MinWidth = 0; target.Children.Add(_windows);
        _refreshWindows = Button("刷新", async () => { RefreshWindows(); await Task.CompletedTask; }, allowDuringTask: true);
        _refreshWindows.Padding = new Thickness(8, 6); Grid.SetColumn(_refreshWindows, 1); target.Children.Add(_refreshWindows);
        recording.Children.Add(Field("游戏窗口", target));
        var countdown = new Grid { ColumnDefinitions = new ColumnDefinitions("*,85"), ColumnSpacing = 8 };
        countdown.Children.Add(new TextBlock { Text = "开始前倒计时（秒）", VerticalAlignment = VerticalAlignment.Center, Opacity = .75 });
        _countdown.MinWidth = 70; Grid.SetColumn(_countdown, 1); countdown.Children.Add(_countdown); recording.Children.Add(countdown);
        _captureState.FontSize = 19; _captureState.FontWeight = FontWeight.SemiBold;
        _captureState.TextWrapping = TextWrapping.Wrap; _captureState.Text = "准备就绪";
        recording.Children.Add(_captureState);
        var actions = new Grid { ColumnDefinitions = new ColumnDefinitions("*,*"), RowDefinitions = new RowDefinitions("Auto,Auto"),
            ColumnSpacing = 8, RowSpacing = 8 };
        foreach (var button in new[] { _record, _stop, _pause, _discardRecording })
        {
            button.Height = button.MinHeight = 40; button.MinWidth = 0; button.Padding = new Thickness(4, 4);
            button.FontSize = 12; button.HorizontalAlignment = HorizontalAlignment.Stretch;
            button.HorizontalContentAlignment = HorizontalAlignment.Center; button.VerticalContentAlignment = VerticalAlignment.Center;
        }
        _discardRecording.Background = new SolidColorBrush(Color.Parse("#B83232"));
        _discardRecording.Foreground = Brushes.White;
        _discardRecording.BorderBrush = new SolidColorBrush(Color.Parse("#B83232"));
        actions.Children.Add(_record);
        Grid.SetColumn(_stop, 1); actions.Children.Add(_stop);
        Grid.SetRow(_pause, 1); actions.Children.Add(_pause);
        Grid.SetRow(_discardRecording, 1); Grid.SetColumn(_discardRecording, 1); actions.Children.Add(_discardRecording);
        recording.Children.Add(actions);
        ToolTip.SetTip(_doubleClickDiscard, "勾选后，录制区的放弃按钮需要双击；时间轴上的放弃按钮和快捷键不受影响。本选项在当前页面有效。");
        recording.Children.Add(_doubleClickDiscard);
        _mark = Button("添加标记", async () => await _vm.Client.CallAsync("capture_mark", new JsonObject { ["label"] = "阶段标记" }));
        recording.Children.Add(_mark); recording.Children.Add(_captureHint);
        recording.Children.Add(new Separator());
        recording.Children.Add(new TextBlock { Text = "F8  暂停 / 继续\nF9  停止录制\nF10  停止并保存\n\n单击继续后有3秒切回游戏。停止后可在时间轴编辑，首次保存时再命名。放弃本次录制会直接丢弃，不保存草稿。", TextWrapping = TextWrapping.Wrap, FontSize = 12, Opacity = .65 });
        _recordingExpander = new Expander { Header = "录制脚本", Content = recording, IsExpanded = true,
            HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        var captureCard = Card(_recordingExpander); captureCard.Padding = new Thickness(12); captureCard.VerticalAlignment = VerticalAlignment.Top;
        var captureScroll = new ScrollViewer { Content = captureCard, HorizontalContentAlignment = HorizontalAlignment.Stretch,
            HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto, VerticalAlignment = VerticalAlignment.Top };
        Grid.SetColumn(captureScroll, 2); layout.Children.Add(captureScroll);
        var narrow = false;
        layout.SizeChanged += (_, _) =>
        {
            var next = layout.Bounds.Width < 900;
            captureScroll.MaxHeight = next ? Math.Max(120, layout.Bounds.Height - 54) : double.PositiveInfinity;
            if (next == narrow) return;
            narrow = next;
            _libraryNarrow = narrow;
            layout.ColumnDefinitions = new ColumnDefinitions(narrow ? "*" : "*,12,300");
            layout.RowDefinitions = new RowDefinitions(narrow ? "Auto,*" : "*");
            layout.RowSpacing = narrow ? 8 : 0;
            Grid.SetRow(library, narrow ? 1 : 0); Grid.SetColumn(captureScroll, narrow ? 0 : 2);
            _recordingExpander.IsExpanded = !narrow || _vm.Capturing;
        };
        return layout;
    }

    private Control BuildLibraryPane(ListBox list, TextBox search, bool compact)
    {
        var pane = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,*"), RowSpacing = 8 };
        if (compact)
            pane.Children.Add(new TextBlock { Text = "脚本库", FontWeight = FontWeight.SemiBold, FontSize = 15 });
        else
        {
            _deleteFile = Button("删除", async () => { if (CurrentLibraryItem is JsonNode item) await DeleteLibraryFileAsync(item); });
            var rename = Button("重命名…", async () => { if (CurrentLibraryItem is JsonNode item) await RenameLibraryFileAsync(item); });
            _documentActions.Add(rename);
            var tools = Wrap(Button("新建", NewScriptAsync), Button("导入…", ImportAsync), rename, _deleteFile, Button("刷新", RefreshLibraryAsync, allowDuringTask: true), _libraryCount);
            foreach (var button in tools.Children.OfType<Button>()) { button.Padding = new Thickness(10, 6); button.MinHeight = 32; }
            _libraryCount.VerticalAlignment = VerticalAlignment.Center; _libraryCount.Opacity = .6; _libraryCount.FontSize = 12;
            pane.Children.Add(tools);
        }
        Grid.SetRow(search, 1); pane.Children.Add(search);
        list.MinHeight = 0; list.ClipToBounds = true;
        list.ItemTemplate = new FuncDataTemplate<JsonNode>((item, _) => item == null ? new TextBlock() : BuildLibraryRow(item, compact));
        Grid.SetRow(list, 2); pane.Children.Add(list);
        list.DoubleTapped += async (_, _) => { if (list.SelectedItem is JsonNode item) await Run(async () => await OpenLibraryFileAsync(item, true)); };
        return pane;
    }

    private Control BuildLibraryRow(JsonNode item, bool compact)
    {
        var row = new Grid { ColumnDefinitions = new ColumnDefinitions(compact ? "*" : "*,160,Auto,Auto"), ColumnSpacing = 8,
            Margin = new Thickness(5, 6), MinHeight = 42 };
        var info = new StackPanel { Spacing = 3, VerticalAlignment = VerticalAlignment.Center };
        info.Children.Add(new TextBlock { Text = item["name"]?.GetValue<string>(), FontWeight = FontWeight.SemiBold, TextTrimming = TextTrimming.CharacterEllipsis });
        var duration = (item["duration_ms"]?.GetValue<double>() ?? 0) / 1000;
        info.Children.Add(new TextBlock { Text = $"{duration:0.#} 秒 · {item["event_count"]} 个事件" + (item["is_default"]?.GetValue<bool>() == true ? " · 默认脚本" : ""), FontSize = 11, Opacity = .6, TextTrimming = TextTrimming.CharacterEllipsis });
        var error = item["error"]?.GetValue<string>();
        ToolTip.SetTip(info, string.IsNullOrEmpty(error) ? item["description"]?.GetValue<string>() : error);
        row.Children.Add(info);
        if (compact) return row;
        var associations = item["bindings"]?.AsArray() ?? new JsonArray();
        var bind = Button(associations.Count == 0 ? "绑定任务…" : associations.Count == 1 ? associations[0]!["label"]!.GetValue<string>() : $"已绑定 {associations.Count} 个任务", () => Task.CompletedTask);
        bind.Content = new TextBlock { Text = bind.Content?.ToString(), TextTrimming = TextTrimming.CharacterEllipsis };
        bind.Click += async (_, _) => await Run(async () => await ShowBindingFlyoutAsync(item, bind));
        ToolTip.SetTip(bind, associations.Count == 0 ? "选择使用这份脚本的任务" : string.Join("\n", associations.Select(value => value?["label"]?.GetValue<string>())) + "\n单击修改绑定");
        bind.Padding = new Thickness(8, 5); bind.MinHeight = 30; bind.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(bind, 1); row.Children.Add(bind);
        var edit = Button("编辑", async () => await OpenLibraryFileAsync(item, true));
        edit.Padding = new Thickness(8, 5); edit.MinHeight = 30; edit.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(edit, 2); row.Children.Add(edit);
        var more = new Button { Content = "⋯", Padding = new Thickness(7, 4), MinHeight = 30, VerticalAlignment = VerticalAlignment.Center };
        ToolTip.SetTip(more, "更多操作");
        var menu = new MenuFlyout();
        foreach (var action in new (string Label, Func<Task> Run)[] {
            ("重命名…", () => RenameLibraryFileAsync(item)),
            ("复制脚本…", () => DuplicateLibraryFileAsync(item)),
            ("导出…", async () => { if (await OpenLibraryFileAsync(item, false)) await ExportAsync(); }),
            ("删除…", () => DeleteLibraryFileAsync(item)) })
        {
            var menuItem = new MenuItem { Header = action.Label };
            menuItem.Click += async (_, _) => await Run(action.Run); menu.Items.Add(menuItem);
        }
        more.Flyout = menu; Grid.SetColumn(more, 3); row.Children.Add(more);
        return row;
    }

    private void WireLibrarySelection(ListBox list) => list.SelectionChanged += async (_, _) =>
    {
        if (!_sync && list.SelectedItem is JsonNode item)
            await Run(async () => await OpenLibraryFileAsync(item, ReferenceEquals(list, _editorLibrary)));
    };

    private async Task<bool> OpenLibraryFileAsync(JsonNode item, bool edit)
    {
        var file = item["file"]!.GetValue<string>();
        if (file != _vm.File)
        {
            if (!await EnsureSavedAsync()) { FilterLibrary(); return false; }
            await _vm.ChangeAsync("load", new JsonObject { ["file"] = file });
        }
        if (edit) _tabs.SelectedIndex = 1;
        _status.Text = "已打开“" + _vm.Name + "”。修改后按 Ctrl+S 保存。";
        return true;
    }

    private async Task RefreshLibraryAsync()
    {
        var result = await _vm.Client.CallAsync("list");
        _files = result["items"]!.AsArray(); _availableTasks = result["tasks"]?.AsArray() ?? new JsonArray();
        _bindingRevision = result["binding_revision"]?.GetValue<string>();
        if (result["binding_errors"] is JsonObject errors && errors.Count > 0)
            _status.Text = string.Join("；", errors.Select(error => error.Value?.GetValue<string>()).Where(error => !string.IsNullOrEmpty(error)).Distinct());
        else if (MaaProcessor.Processors.Any(p => p.IsTaskRunActive)) _status.Text = RunningHint;
        FilterLibrary();
    }

    private void FilterLibrary()
    {
        _sync = true;
        try
        {
            foreach (var pair in new[] { (_library, _search), (_editorLibrary, _editorSearch) })
            {
                var query = pair.Item2.Text?.Trim() ?? "";
                var files = _files.Where(item => item != null && (query.Length == 0 || (item["name"]?.GetValue<string>() ?? "").Contains(query, StringComparison.CurrentCultureIgnoreCase))).ToList();
                pair.Item1.ItemsSource = files;
                pair.Item1.SelectedItem = files.FirstOrDefault(item => item?["file"]?.GetValue<string>() == _vm.File);
            }
            _editorPicker.ItemsSource = _files.Where(item => item != null).ToList();
            _editorPicker.SelectedItem = _files.FirstOrDefault(item => item?["file"]?.GetValue<string>() == _vm.File);
            _libraryCount.Text = $"{_files.Count} 个脚本";
        }
        finally { _sync = false; }
    }

    private async Task ShowBindingFlyoutAsync(JsonNode item, Button anchor)
    {
        var filename = item["file"]!.GetValue<string>();
        if (_vm.File == filename && !await EnsureSavedAsync()) return;
        var file = _files.FirstOrDefault(value => value?["file"]?.GetValue<string>() == filename);
        if (file == null || _bindingRevision == null) return;
        var revision = _bindingRevision;
        var controls = new List<(CheckBox Check, string Id, string Mode)>();
        var content = new StackPanel { Spacing = 10, Width = 330 };
        content.Children.Add(new TextBlock { Text = "绑定到任务", FontSize = 17, FontWeight = FontWeight.SemiBold });
        content.Children.Add(new TextBlock { Text = "选择一个或多个任务。关卡任务会保留进关和退关流程，进入关卡后回放此脚本。", TextWrapping = TextWrapping.Wrap, FontSize = 12, Opacity = .7 });
        var tasks = new StackPanel { Spacing = 6 };
        var other = new StackPanel { Spacing = 6 };
        foreach (var task in _availableTasks.OfType<JsonObject>())
        {
            var stage = task["modes"]!.AsArray().Any(mode => mode!.GetValue<string>() == "pipeline");
            var bound = task["binding"]?["file"]?.GetValue<string>();
            var mode = bound == filename ? task["binding"]?["mode"]?.GetValue<string>() ?? (stage ? "pipeline" : "replace") : stage ? "pipeline" : "replace";
            var caption = new StackPanel { Spacing = 2 };
            caption.Children.Add(new TextBlock { Text = task["label"]?.GetValue<string>(), TextWrapping = TextWrapping.Wrap });
            if (bound != null && bound != filename) caption.Children.Add(new TextBlock { Text = "将替换：“" + bound + "”", FontSize = 11, Opacity = .6 });
            if (stage && bound == filename && mode == "replace") caption.Children.Add(new TextBlock { Text = "当前方式：仅执行脚本", FontSize = 11, Opacity = .65 });
            var check = new CheckBox { Content = caption, IsChecked = bound == filename,
                IsEnabled = file["can_bind"]?.GetValue<bool>() == true || bound == filename };
            controls.Add((check, task["id"]!.GetValue<string>(), mode));
            (stage ? tasks : other).Children.Add(check);
        }
        if (other.Children.Count > 0)
        {
            other.Children.Insert(0, new TextBlock { Text = "以下任务会仅回放脚本，跳过原有任务流程。", TextWrapping = TextWrapping.Wrap, FontSize = 12, Foreground = Brushes.DarkOrange });
            tasks.Children.Add(new Expander { Header = "其他任务（仅执行脚本）", Content = other, HorizontalAlignment = HorizontalAlignment.Stretch });
        }
        content.Children.Add(new ScrollViewer { Content = tasks, MaxHeight = 280 });
        // Suki does not give a plain Flyout a surface; reuse the page's themed card.
        var popup = new Flyout { Content = Card(content), Placement = PlacementMode.RightEdgeAlignedTop };
        var apply = Button("确定", async () =>
        {
            var chosen = new JsonObject();
            foreach (var (check, id, mode) in controls) if (check.IsChecked == true) chosen[id] = mode;
            await _vm.Client.CallAsync("set_file_bindings", new JsonObject { ["file"] = filename,
                ["revision"] = file["revision"]!.DeepClone(), ["binding_revision"] = revision, ["tasks"] = chosen });
            popup.Hide(); await RefreshLibraryAsync(); _status.Text = "已更新“" + file["name"] + "”的任务绑定。";
        }, true);
        content.Children.Add(Wrap(apply, Button("取消", async () => { popup.Hide(); await Task.CompletedTask; })));
        popup.ShowAt(TopLevel.GetTopLevel(anchor) != null ? anchor : _library);
    }

    private async Task NewScriptAsync()
    {
        if (!await EnsureSavedAsync()) return;
        await _vm.ChangeAsync("new"); _tabs.SelectedIndex = 1; BeginInsert();
        _status.Text = "已新建脚本。添加操作后保存，保存时输入名称。";
    }

    private async Task DuplicateLibraryFileAsync(JsonNode item)
    {
        if (_vm.File == item["file"]!.GetValue<string>() && !await EnsureSavedAsync()) return;
        var name = await AskNameAsync("复制脚本", item["name"]!.GetValue<string>() + "-副本");
        if (name == null) return;
        var fresh = _files.First(value => value?["file"]?.GetValue<string>() == item["file"]!.GetValue<string>())!;
        await _vm.Client.CallAsync("duplicate_file", new JsonObject { ["file"] = fresh["file"]!.DeepClone(), ["revision"] = fresh["revision"]!.DeepClone(), ["name"] = name });
        await RefreshLibraryAsync(); _status.Text = "已创建“" + name + "”的独立副本。";
    }

    private async Task RenameLibraryFileAsync(JsonNode item)
    {
        var filename = item["file"]!.GetValue<string>();
        if (_vm.File == filename && !await EnsureSavedAsync()) return;
        var name = await AskNameAsync("重命名脚本", item["name"]!.GetValue<string>(), filename);
        if (name == null) return;
        var fresh = _files.First(value => value?["file"]?.GetValue<string>() == filename)!;
        var result = await _vm.Client.CallAsync("rename_file", new JsonObject { ["file"] = filename,
            ["revision"] = fresh["revision"]!.DeepClone(), ["name"] = name });
        if (_vm.File == filename) _vm.Apply(result["document"]!);
        await RefreshLibraryAsync(); _status.Text = "已重命名为“" + name + "”，原有任务绑定已保留。";
    }

    private async Task DeleteLibraryFileAsync(JsonNode item)
    {
        var filename = item["file"]!.GetValue<string>();
        var count = item["bindings"]?.AsArray().Count ?? 0;
        var message = "删除“" + item["name"] + "”？" + (count > 0 ? $"同时解除 {count} 个任务的绑定。" : "") + "\n文件会移入脚本库的 .trash 目录。";
        if (_vm.File == filename && HasChanges) message += "当前未保存的修改也会被放弃。";
        if (!await ConfirmDeleteAsync(message)) return;
        var current = _vm.File == filename;
        if (current && HasChanges) await _vm.ChangeAsync("discard");
        var result = await _vm.Client.CallAsync("delete_file", new JsonObject { ["file"] = filename, ["revision"] = item["revision"]!.DeepClone() });
        if (current) _vm.Apply(result["document"]!);
        await RefreshLibraryAsync(); _status.Text = "已删除脚本，恢复副本位于 recordings/.trash。";
    }
}
