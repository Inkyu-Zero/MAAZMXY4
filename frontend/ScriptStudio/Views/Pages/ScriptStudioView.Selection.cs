using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
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

    private void BuildBatchInspector()
    {
        _batchInspector.Children.Add(new TextBlock { Text = "批量编辑", FontSize = 17, FontWeight = FontWeight.SemiBold });
        _batchInspector.Children.Add(new TextBlock { Text = "拖动任一选中块可整体移动。所有操作保持原有间隔，按下与释放会一起移动。", TextWrapping = TextWrapping.Wrap, Opacity = .75 });
        _batchInspector.Children.Add(Field("移动距离（秒，负数向前）", _batchOffset));
        _batchInspector.Children.Add(Button("移动选中操作", () => MoveSelectionAsync(_selection.ToArray(), Milliseconds(_batchOffset))));
        _batchInspector.Children.Add(Button("删除选中操作", DeleteAsync));
        _batchInspector.Children.Add(new TextBlock { Text = "Ctrl + 单击：增减选择\n空白处拖动：框选\nCtrl+A：全选 · Esc：取消\n← / →：移动 10 毫秒\nShift + ← / →：移动 100 毫秒", TextWrapping = TextWrapping.Wrap, Opacity = .75 });
    }
}
