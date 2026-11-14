using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Avalonia.VisualTree;

namespace MFAAvalonia.Views.Pages;

public sealed partial class ScriptStudioView
{
    private JsonArray _folders = new();
    private string? _folderRevision;
    private string? _selectedLibraryGroup;
    private readonly HashSet<string> _expandedFolders = new();
    private readonly string _dragOwner = Guid.NewGuid().ToString("N");
    private static readonly DataFormat<string> ScriptDragFormat = DataFormat.CreateStringApplicationFormat("maazmxy4-script-folder");
    private static bool IsLibraryGroup(JsonNode item) => item["kind"] != null;
    private static readonly IBrush FolderFill = new SolidColorBrush(Color.FromArgb(16, 128, 128, 128));
    private static readonly IBrush FolderLine = new SolidColorBrush(Color.FromArgb(30, 128, 128, 128));
    private static readonly IBrush DropFill = new SolidColorBrush(Color.FromArgb(38, 23, 101, 230));
    private static readonly IBrush DropLine = new SolidColorBrush(Color.Parse("#1765E6"));
    private Canvas? _scriptDragVisual;
    private Border? _scriptDragCard, _dropFrame;
    private TextBlock? _scriptDragHint;
    private AdornerLayer? _scriptDragLayer;
    private Control? _scriptDragSource;
    private bool _scriptDragTracking;
    private int _scriptDragGeneration;

    private List<JsonNode?> LibraryRows(string query)
    {
        bool Matches(JsonNode item) => query.Length == 0 || (item["name"]?.GetValue<string>() ?? "")
            .Contains(query, StringComparison.CurrentCultureIgnoreCase);
        var result = new List<JsonNode?>();
        if (_folders.Count > 0) result.Add(new JsonObject { ["kind"] = "root", ["name"] = "根目录",
            ["count"] = _files.Count(item => item?["folder_id"] == null) });
        result.AddRange(_files.Where(item => item != null && item["folder_id"] == null && Matches(item)));
        foreach (var folder in _folders.OfType<JsonObject>())
        {
            var id = folder["id"]!.GetValue<string>();
            var contents = _files.Where(item => item?["folder_id"]?.GetValue<string>() == id).ToList();
            var matching = Matches(folder) ? contents : contents.Where(item => Matches(item!)).ToList();
            if (query.Length > 0 && !Matches(folder) && matching.Count == 0) continue;
            var header = folder.DeepClone(); header["kind"] = "folder";
            header["expanded"] = _expandedFolders.Contains(id) || query.Length > 0;
            result.Add(header);
            if (header["expanded"]!.GetValue<bool>()) result.AddRange(matching);
        }
        return result;
    }

    private Control BuildFolderRow(JsonNode item, bool compact)
    {
        var id = item["id"]?.GetValue<string>();
        var row = new Grid { Name = "LibraryFolderRow", Background = Brushes.Transparent,
            ColumnDefinitions = new ColumnDefinitions("*,Auto"), MinHeight = 40, Margin = new Thickness(2, 4), Tag = id ?? "" };
        var frame = new Border { Name = "FolderDropFrame", CornerRadius = new CornerRadius(7),
            Background = FolderFill, BorderBrush = FolderLine, BorderThickness = new Thickness(1), IsHitTestVisible = false };
        Grid.SetColumnSpan(frame, 2); row.Children.Add(frame);
        var toggle = Button(item["name"]!.GetValue<string>(), () =>
            {
                _selectedLibraryGroup = id ?? "";
                if (id != null) { if (!_expandedFolders.Add(id)) _expandedFolders.Remove(id); FilterLibrary(); }
                else FilterLibrary();
                return Task.CompletedTask;
            }, allowDuringTask: true);
        toggle.HorizontalContentAlignment = HorizontalAlignment.Left;
        var label = item["name"]!.GetValue<string>();
        ToolTip.SetTip(toggle, label);
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("12,20,*,Auto"), ColumnSpacing = compact ? 5 : 8 };
        var chevron = new PathIcon { Width = 9, Height = 9, Opacity = .65, IsVisible = id != null,
            Data = Geometry.Parse(item["expanded"]?.GetValue<bool>() == true ? "M0,0 L8,0 L4,5 Z" : "M0,0 L5,4 L0,8 Z") };
        header.Children.Add(chevron);
        var icon = new PathIcon { Width = 18, Height = 16, Opacity = .75,
            Data = Geometry.Parse("M1,3 L8,3 L10,5 L19,5 L19,17 L1,17 Z M3,7 L3,15 L17,15 L17,7 Z") };
        Grid.SetColumn(icon, 1); header.Children.Add(icon);
        var name = new TextBlock { Text = label, FontWeight = FontWeight.SemiBold, TextTrimming = TextTrimming.CharacterEllipsis,
            VerticalAlignment = VerticalAlignment.Center };
        Grid.SetColumn(name, 2); header.Children.Add(name);
        var count = new TextBlock { Text = item["count"]?.ToString(), FontSize = 12, Opacity = .55, VerticalAlignment = VerticalAlignment.Center };
        Grid.SetColumn(count, 3); header.Children.Add(count);
        toggle.Content = header;
        toggle.HorizontalContentAlignment = HorizontalAlignment.Stretch;
        toggle.HorizontalAlignment = HorizontalAlignment.Stretch;
        toggle.Background = Brushes.Transparent; toggle.BorderThickness = new Thickness(0); toggle.Margin = new Thickness(0);
        toggle.RenderTransform = new ScaleTransform(1, 1);
        toggle.Bind(TemplatedControl.ForegroundProperty, new DynamicResourceExtension("SukiText"));
        toggle.Padding = new Thickness(10, 7); toggle.MinHeight = 38;
        toggle.PointerEntered += (_, _) => toggle.Background = FolderFill;
        toggle.PointerExited += (_, _) => toggle.Background = Brushes.Transparent;
        row.Children.Add(toggle);
        if (id != null && !compact)
        {
            var more = new Button { Content = "⋯", Padding = new Thickness(8, 4), Background = Brushes.Transparent,
                BorderThickness = new Thickness(0), Margin = new Thickness(0, 2, 4, 2), RenderTransform = new ScaleTransform(1, 1) };
            more.Bind(TemplatedControl.ForegroundProperty, new DynamicResourceExtension("SukiText"));
            var menu = new MenuFlyout();
            var rename = new MenuItem { Header = "重命名文件夹…" };
            rename.Click += async (_, _) => await Run(() => RenameFolderAsync(item), allowDuringTask: true);
            var delete = new MenuItem { Header = "删除文件夹及全部脚本…" };
            delete.Click += async (_, _) => await Run(() => DeleteFolderAsync(item));
            menu.Items.Add(rename); menu.Items.Add(delete); more.Flyout = menu;
            Grid.SetColumn(more, 1); row.Children.Add(more);
        }
        DragDrop.SetAllowDrop(row, true);
        row.AddHandler(DragDrop.DragOverEvent, (_, e) =>
        {
            e.DragEffects = CanDropFile(DraggedFile(e), id) ? DragDropEffects.Move : DragDropEffects.None;
            SetFolderDropFrame(e.DragEffects == DragDropEffects.Move ? frame : null);
            e.Handled = true;
        });
        row.AddHandler(DragDrop.DragLeaveEvent, (_, _) => { if (ReferenceEquals(_dropFrame, frame)) SetFolderDropFrame(null); });
        row.AddHandler(DragDrop.DropEvent, async (_, e) =>
        {
            SetFolderDropFrame(null); e.Handled = true;
            if (DraggedFile(e) is { } file && CanDropFile(file, id)) await Run(() => MoveToFolderAsync(file, id), allowDuringTask: true);
        });
        return row;
    }

    private string? DraggedFile(DragEventArgs e)
    {
        var value = e.DataTransfer.TryGetValue(ScriptDragFormat);
        var prefix = _dragOwner + "\n";
        return value != null && value.StartsWith(prefix, StringComparison.Ordinal) ? value[prefix.Length..] : null;
    }

    private void WireScriptDrag(Control source, JsonNode item, bool compact)
    {
        EnsureScriptDragTracking();
        PointerPressedEventArgs? pressed = null;
        Point origin = default;
        source.AddHandler(InputElement.PointerPressedEvent, (_, e) =>
        {
            if (e.Source is Visual visual && (visual is Button || visual.GetVisualAncestors().TakeWhile(v => v != source).Any(v => v is Button))) return;
            if (e.GetCurrentPoint(source).Properties.IsLeftButtonPressed)
            {
                pressed = e; origin = e.GetPosition(source); e.Pointer.Capture(source); e.Handled = true;
            }
        }, RoutingStrategies.Tunnel);
        source.PointerReleased += async (_, e) =>
        {
            if (pressed == null) return;
            pressed = null;
            if (ReferenceEquals(e.Pointer.Captured, source)) e.Pointer.Capture(null);
            e.Handled = true;
            if (new Rect(source.Bounds.Size).Contains(e.GetPosition(source))) await Run(async () => await OpenLibraryFileAsync(item, compact));
        };
        source.PointerCaptureLost += (_, _) => pressed = null;
        source.DetachedFromVisualTree += (_, _) =>
        {
            var pointer = pressed?.Pointer; pressed = null;
            if (pointer != null && ReferenceEquals(pointer.Captured, source)) pointer.Capture(null);
        };
        source.PointerMoved += async (_, e) =>
        {
            var delta = e.GetPosition(source) - origin;
            if (pressed == null || !e.GetCurrentPoint(source).Properties.IsLeftButtonPressed || Math.Abs(delta.X) + Math.Abs(delta.Y) < 6) return;
            var trigger = pressed; pressed = null;
            e.Pointer.Capture(null);
            var data = new DataTransfer();
            data.Add(DataTransferItem.Create(ScriptDragFormat, _dragOwner + "\n" + item["file"]!.GetValue<string>()));
            var generation = BeginScriptDragVisual(source, item, e.GetPosition(this));
            try { await DragDrop.DoDragDropAsync(trigger, data, DragDropEffects.Move); }
            catch (Exception error) { _status.Text = "拖动未完成：" + error.Message; }
            finally { if (generation == _scriptDragGeneration) ClearScriptDragVisual(); }
        };
    }

    private bool CanDropFile(string? file, string? folder)
    {
        var item = _files.FirstOrDefault(i => i?["file"]?.GetValue<string>() == file);
        return item != null && item["folder_id"]?.GetValue<string>() != folder;
    }

    private void SetFolderDropFrame(Border? frame)
    {
        if (ReferenceEquals(_dropFrame, frame)) return;
        if (_dropFrame != null) { _dropFrame.Background = FolderFill; _dropFrame.BorderBrush = FolderLine; }
        _dropFrame = frame;
        if (frame != null) { frame.Background = DropFill; frame.BorderBrush = DropLine; }
    }

    private void EnsureScriptDragTracking()
    {
        if (_scriptDragTracking) return;
        _scriptDragTracking = true;
        DragDrop.SetAllowDrop(this, true);
        AddHandler(DragDrop.DragOverEvent, (_, e) =>
        {
            if (DraggedFile(e) is not { } file) return;
            var target = e.Source is Visual visual ? visual.GetSelfAndVisualAncestors().OfType<Grid>()
                .FirstOrDefault(c => c.Name == "LibraryFolderRow") : null;
            var folder = target?.Tag as string;
            var valid = target != null && CanDropFile(file, string.IsNullOrEmpty(folder) ? null : folder);
            e.DragEffects = valid ? DragDropEffects.Move : DragDropEffects.None;
            if (_scriptDragVisual != null)
            {
                _scriptDragVisual.IsVisible = true;
                var name = _folders.FirstOrDefault(f => f?["id"]?.GetValue<string>() == folder)?["name"]?.GetValue<string>();
                _scriptDragHint!.Text = valid ? string.IsNullOrEmpty(folder) ? "移出文件夹" : "移入「" + name + "」" : "拖到目标文件夹";
                PositionScriptDragVisual(e.GetPosition(this));
            }
        }, RoutingStrategies.Bubble, true);
        AddHandler(DragDrop.DragLeaveEvent, (_, e) =>
        {
            if (_scriptDragVisual != null && !new Rect(Bounds.Size).Contains(e.GetPosition(this))) _scriptDragVisual.IsVisible = false;
        }, RoutingStrategies.Bubble, true);
        DetachedFromVisualTree += (_, _) => ClearScriptDragVisual();
    }

    private int BeginScriptDragVisual(Control source, JsonNode item, Point position)
    {
        ClearScriptDragVisual();
        _scriptDragSource = source; source.Opacity = .45;
        _scriptDragLayer = AdornerLayer.GetAdornerLayer(this);
        if (_scriptDragLayer == null) return _scriptDragGeneration;
        _scriptDragHint = new TextBlock { Text = "拖到目标文件夹", FontSize = 11, Opacity = .7 };
        var label = new StackPanel { Spacing = 4 };
        label.Children.Add(new TextBlock { Text = item["name"]?.GetValue<string>(), FontSize = 13,
            FontWeight = FontWeight.SemiBold, TextTrimming = TextTrimming.CharacterEllipsis });
        label.Children.Add(_scriptDragHint);
        label.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiText"));
        _scriptDragCard = new Border { Name = "ScriptDragCard", Width = 220, Padding = new Thickness(12, 9),
            Child = label, BorderBrush = DropLine, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8),
            BoxShadow = new BoxShadows(new BoxShadow { Blur = 12, OffsetY = 4, Color = Color.FromArgb(55, 0, 0, 0) }) };
        _scriptDragCard.Bind(Border.BackgroundProperty, new DynamicResourceExtension("SukiCardBackground"));
        _scriptDragVisual = new Canvas { Name = "ScriptDragPreview", IsHitTestVisible = false,
            Width = _scriptDragLayer.Bounds.Width, Height = _scriptDragLayer.Bounds.Height };
        _scriptDragVisual.Children.Add(_scriptDragCard);
        _scriptDragLayer.Children.Add(_scriptDragVisual);
        PositionScriptDragVisual(position);
        return _scriptDragGeneration;
    }

    private void PositionScriptDragVisual(Point position)
    {
        if (_scriptDragLayer == null || _scriptDragCard == null) return;
        var point = this.TranslatePoint(position, _scriptDragLayer) ?? position;
        var fallback = TopLevel.GetTopLevel(this)?.ClientSize ?? Bounds.Size;
        var width = _scriptDragLayer.Bounds.Width > 0 ? _scriptDragLayer.Bounds.Width : fallback.Width;
        var height = _scriptDragLayer.Bounds.Height > 0 ? _scriptDragLayer.Bounds.Height : fallback.Height;
        _scriptDragVisual!.Width = width; _scriptDragVisual.Height = height;
        Canvas.SetLeft(_scriptDragCard, Math.Clamp(point.X + 16, 0, Math.Max(0, width - 224)));
        Canvas.SetTop(_scriptDragCard, Math.Clamp(point.Y + 18, 0, Math.Max(0, height - 66)));
    }

    private void ClearScriptDragVisual()
    {
        ++_scriptDragGeneration;
        if (_scriptDragVisual != null) _scriptDragLayer?.Children.Remove(_scriptDragVisual);
        if (_scriptDragSource != null) _scriptDragSource.Opacity = 1;
        _scriptDragVisual = null; _scriptDragCard = null; _scriptDragHint = null; _scriptDragLayer = null; _scriptDragSource = null;
        SetFolderDropFrame(null);
    }

    private async Task MoveToFolderAsync(string file, string? folder)
    {
        await _vm.Client.CallAsync("move_to_folder", new JsonObject { ["file"] = file, ["folder_id"] = folder, ["folder_revision"] = _folderRevision });
        if (folder != null) _expandedFolders.Add(folder);
        await RefreshLibraryAsync(); _status.Text = "已移动脚本，原有任务绑定保持不变。";
    }

    private async Task CreateFolderAsync()
    {
        var name = await AskLibraryNameAsync("新建文件夹", "新建文件夹", null, true);
        if (name == null) return;
        var result = await _vm.Client.CallAsync("create_folder", new JsonObject { ["name"] = name, ["folder_revision"] = _folderRevision });
        _expandedFolders.Add(result["folder_id"]!.GetValue<string>());
        await RefreshLibraryAsync(); _status.Text = "已创建文件夹“" + name + "”，可拖入脚本。";
    }

    private async Task RenameFolderAsync(JsonNode item)
    {
        var id = item["id"]!.GetValue<string>();
        var name = await AskLibraryNameAsync("重命名文件夹", item["name"]!.GetValue<string>(), id, true);
        if (name == null) return;
        await _vm.Client.CallAsync("rename_folder", new JsonObject { ["folder_id"] = id, ["name"] = name, ["folder_revision"] = _folderRevision });
        await RefreshLibraryAsync(); _status.Text = "文件夹已重命名，脚本和任务绑定保持不变。";
    }

    private async Task DeleteFolderAsync(JsonNode item)
    {
        var id = item["id"]!.GetValue<string>();
        var files = _files.Where(file => file?["folder_id"]?.GetValue<string>() == id).ToList();
        var revisions = new JsonObject();
        foreach (var file in files) revisions[file!["file"]!.GetValue<string>()] = file["revision"]!.DeepClone();
        var revision = _folderRevision;
        var current = _vm.File != null && revisions.ContainsKey(_vm.File);
        var bindings = files.Sum(file => file?["bindings"]?.AsArray().Count ?? 0);
        var message = $"删除文件夹“{item["name"]}”及其中全部 {files.Count} 个脚本？\n将解除 {bindings} 个任务绑定，不会自动改绑其他脚本。";
        if (current && HasChanges) message += "\n当前未保存的脚本修改也会被放弃。";
        if (!await ConfirmDeleteAsync(message)) return;
        if (current && HasChanges) await _vm.ChangeAsync("discard");
        var result = await _vm.Client.CallAsync("delete_folder", new JsonObject { ["folder_id"] = id, ["folder_revision"] = revision, ["revisions"] = revisions });
        if (current) _vm.Apply(result["document"]!);
        _expandedFolders.Remove(id);
        if (_selectedLibraryGroup == id) _selectedLibraryGroup = null;
        await RefreshLibraryAsync(); _status.Text = $"已删除文件夹及 {files.Count} 个脚本；受影响任务已解除绑定。";
    }
}
