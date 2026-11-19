using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input.Platform;
using Avalonia.Media;

namespace MFAAvalonia.Views.Pages;

public sealed partial class ScriptStudioView
{
    private readonly HashSet<string> _selection = new(StringComparer.Ordinal);
    private readonly TextBlock _selectionSummary = new() { FontSize = 13, FontWeight = FontWeight.SemiBold };
    private readonly StackPanel _batchInspector = new() { Spacing = 12, IsVisible = false, Margin = new Thickness(0,0,10,0) };
    private readonly ScrollViewer _batchInspectorHost = new();
    private Control? _singleApplyRow;
    private readonly NumericUpDown _batchOffset = Number(0, -604800, 604800, "0.000");
    private Button? _duplicateOperationButton;

    private static JsonArray SelectionJson(IEnumerable<string> ids) => new(ids.Select(id => (JsonNode?)JsonValue.Create(id)).ToArray());

    private void SelectOperations(IEnumerable<string> ids)
    {
        var wanted = ids.ToHashSet(StringComparer.Ordinal);
        var selected = _vm.Operations.Where(operation => wanted.Contains(operation.Id)).ToArray();
        _selection.Clear(); _selection.UnionWith(selected.Select(operation => operation.Id));
        if (selected.Length == 1) SelectOperation(selected[0]);
        else { _selected = null; _adding = false; _inspectorDirty = false; }
        _sync = true;
        try
        {
            _events.SelectedItems?.Clear();
            foreach (var operation in selected) _events.SelectedItems?.Add(operation);
            _timeline.SetSelection(_selection);
        }
        finally { _sync = false; }
        UpdateControls();
    }

    private async Task MoveSelectionAsync(IReadOnlyList<string> ids, double delta)
    {
        if (ids.Count == 0 || delta == 0 || !await EnsureInspectorAsync()) return;
        await _vm.ChangeAsync("move_many", new JsonObject { ["ids"] = SelectionJson(ids), ["delta_ms"] = delta });
        SelectOperations(ids);
        _status.Text = $"已移动 {ids.Count} 个操作，保留相对间隔和按住时长；Ctrl+Z 可撤销。";
    }

    private async Task CopySelectionAsync()
    {
        if (_selection.Count == 0 || !await EnsureInspectorAsync()) return;
        var clipboard = TopLevel.GetTopLevel(this)?.Clipboard
            ?? throw new InvalidOperationException("当前窗口无法访问剪贴板。");
        var fragment = await _vm.Client.CallAsync("copy_many", new JsonObject { ["ids"] = SelectionJson(_selection) });
        await clipboard.SetTextAsync(fragment.ToJsonString());
        _status.Text = $"已复制 {_selection.Count} 个操作；移动播放头后按 Ctrl+V，保留原轨道与相对间隔。";
    }

    private async Task PasteSelectionAsync()
    {
        if (_vm.Document == null || !await EnsureInspectorAsync()) return;
        var at = Milliseconds(_cursor);
        var clipboard = TopLevel.GetTopLevel(this)?.Clipboard
            ?? throw new InvalidOperationException("当前窗口无法访问剪贴板。");
        var text = await clipboard.TryGetTextAsync();
        if (string.IsNullOrWhiteSpace(text) || text.Length > 16 * 1024 * 1024)
            throw new InvalidOperationException("剪贴板没有有效的时间轴片段，请先选择操作并复制。");
        JsonNode? fragment;
        try { fragment = JsonNode.Parse(text); }
        catch (System.Text.Json.JsonException) { throw new InvalidOperationException("剪贴板不是时间轴片段，请先选择操作并复制。"); }
        if (fragment is not JsonObject payload || payload["format"]?.ToString() != "maazmxy4.timeline")
            throw new InvalidOperationException("剪贴板不是时间轴片段，请先选择操作并复制。");
        var result = await _vm.Client.CallAsync("paste_many", new JsonObject { ["clipboard"] = payload, ["at_ms"] = at });
        _vm.Apply(result);
        SelectOperations(result["selected_ids"]?.AsArray().Select(id => id!.GetValue<string>()) ?? []);
        _status.Text = $"已在 {at / 1000.0:0.000} 秒粘贴 {_selection.Count} 个操作；Ctrl+Z 可一次撤销。";
    }

    private void BuildBatchInspector()
    {
        _batchInspector.Children.Add(new TextBlock { Text = "批量编辑", FontSize = 17, FontWeight = FontWeight.SemiBold });
        _batchInspector.Children.Add(new TextBlock { Text = "拖动任一选中块可整体移动。所有操作保持原有间隔，按下与释放会一起移动。", TextWrapping = TextWrapping.Wrap, Opacity = .75 });
        _batchInspector.Children.Add(Field("移动距离（秒，负数向前）", _batchOffset));
        _batchInspector.Children.Add(Button("移动选中操作", () => MoveSelectionAsync(_selection.ToArray(), Milliseconds(_batchOffset))));
        _batchInspector.Children.Add(Button("删除选中操作", DeleteAsync));
        _batchInspector.Children.Add(new TextBlock { Text = "Ctrl + 单击：增减选择\n空白处拖动：框选\nCtrl+C / Ctrl+V：复制 / 粘贴到播放头\n跨行保持原轨道与相对间隔\nCtrl+A：全选 · Esc：取消\n← / →：移动 10 毫秒\nShift + ← / →：移动 100 毫秒", TextWrapping = TextWrapping.Wrap, Opacity = .75 });
    }
}
