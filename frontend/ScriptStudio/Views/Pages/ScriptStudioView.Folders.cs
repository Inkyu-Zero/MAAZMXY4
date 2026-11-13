using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;

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

    private List<JsonNode?> LibraryRows(string query)
    {
        bool Matches(JsonNode item) => query.Length == 0 || (item["name"]?.GetValue<string>() ?? "")
            .Contains(query, StringComparison.CurrentCultureIgnoreCase);
        var result = new List<JsonNode?>();
        if (_folders.Count > 0) result.Add(new JsonObject { ["kind"] = "root", ["name"] = "根目录 · 拖到此处移出文件夹" });
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
        var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), MinHeight = 38, Margin = new Thickness(4), Tag = id ?? "" };
        var toggle = Button(id == null ? item["name"]!.GetValue<string>() :
            (item["expanded"]?.GetValue<bool>() == true ? "▾  " : "▸  ") + item["name"] + $"  ({item["count"]})", () =>
            {
                _selectedLibraryGroup = id ?? "";
                if (id != null) { if (!_expandedFolders.Add(id)) _expandedFolders.Remove(id); FilterLibrary(); }
                else FilterLibrary();
                return Task.CompletedTask;
            }, allowDuringTask: true);
        toggle.HorizontalContentAlignment = HorizontalAlignment.Left;
        ToolTip.SetTip(toggle, toggle.Content);
        toggle.Content = new TextBlock { Text = toggle.Content?.ToString(), TextTrimming = TextTrimming.CharacterEllipsis };
        toggle.HorizontalAlignment = HorizontalAlignment.Stretch;
        toggle.Padding = new Thickness(6); toggle.MinHeight = 34;
        row.Children.Add(toggle);
        if (id != null && !compact)
        {
            var more = new Button { Content = "⋯", Padding = new Thickness(8, 4) };
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
            e.DragEffects = DraggedFile(e) != null ? DragDropEffects.Move : DragDropEffects.None;
            row.Background = e.DragEffects == DragDropEffects.Move ? new SolidColorBrush(Color.Parse("#308497B0")) : null;
            e.Handled = true;
        });
        row.AddHandler(DragDrop.DragLeaveEvent, (_, _) => row.Background = null);
        row.AddHandler(DragDrop.DropEvent, async (_, e) =>
        {
            row.Background = null; e.Handled = true;
            if (DraggedFile(e) is { } file) await Run(() => MoveToFolderAsync(file, id), allowDuringTask: true);
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
        PointerPressedEventArgs? pressed = null;
        Point origin = default;
        source.PointerPressed += (_, e) =>
        {
            if (e.GetCurrentPoint(source).Properties.IsLeftButtonPressed)
            {
                pressed = e; origin = e.GetPosition(source); e.Pointer.Capture(source); e.Handled = true;
            }
        };
        source.PointerReleased += async (_, e) =>
        {
            var clicked = pressed != null; pressed = null; e.Pointer.Capture(null); e.Handled = true;
            if (clicked) await Run(async () => await OpenLibraryFileAsync(item, compact));
        };
        source.PointerMoved += async (_, e) =>
        {
            var delta = e.GetPosition(source) - origin;
            if (pressed == null || !e.GetCurrentPoint(source).Properties.IsLeftButtonPressed || Math.Abs(delta.X) + Math.Abs(delta.Y) < 6) return;
            var trigger = pressed; pressed = null;
            e.Pointer.Capture(null);
            var data = new DataTransfer();
            data.Add(DataTransferItem.Create(ScriptDragFormat, _dragOwner + "\n" + item["file"]!.GetValue<string>()));
            try { await DragDrop.DoDragDropAsync(trigger, data, DragDropEffects.Move); }
            catch (Exception error) { _status.Text = "拖动未完成：" + error.Message; }
        };
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
