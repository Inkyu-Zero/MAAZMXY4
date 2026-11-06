using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Collections.Specialized;
using System.ComponentModel;
using System.Globalization;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using MFAAvalonia.Services;

namespace MFAAvalonia.ViewModels.Pages;

public sealed class StudioOperation
{
    private string? _label;
    private string? _lane;
    public required JsonNode Data { get; init; }
    public static int Integer(JsonNode? node, int fallback = 0) => node is JsonValue value && value.TryGetValue<int>(out var number) ? number : fallback;
    public static string String(JsonNode? node, string fallback = "") => node is JsonValue value && value.TryGetValue<string>(out var text) ? text : fallback;
    public string Id => String(Data["id"]);
    public string Kind => String(Data["kind"]);
    public double Start => Data["start_ms"]?.GetValue<double>() ?? 0;
    public double Duration => Data["duration_ms"]?.GetValue<double>() ?? 0;
    public string Label => _label ??= Kind switch
    {
        "key_hold" => KeyName(Integer(Data["key"])),
        "mouse_hold" => (String(Data["button"]) switch { "right" => "右键", "middle" => "中键", _ => "左键" }) + "点击 / 拖动",
        "marker" => "标记 · " + String(Data["label"]),
        "move" => "鼠标移动", "scroll" => "滚轮", "text" => "输入文本", "click" => "单击",
        "key" => "按键 · " + KeyName(Integer(Data["key"])),
        _ => String(Data["label"], Kind)
    };
    public string Lane => _lane ??= Kind == "key_hold" ? Label : Kind == "marker" ? "阶段标记" : Kind.Contains("mouse") || Kind is "move" or "click" or "scroll" ? "鼠标" : "其他操作";
    public string TimeText => (Start / 1000).ToString("0.000", CultureInfo.InvariantCulture) + " s";
    public string DurationText => (Duration / 1000).ToString("0.000", CultureInfo.InvariantCulture) + " s";
    public static string KeyName(int key) => key switch
    {
        32 => "空格", 13 => "Enter", 16 => "Shift", 17 => "Ctrl", 18 => "Alt", 27 => "Esc",
        37 => "←", 38 => "↑", 39 => "→", 40 => "↓", 9 => "Tab", 8 => "退格",
        >= 65 and <= 90 => ((char)key).ToString(), >= 48 and <= 57 => ((char)key).ToString(),
        >= 112 and <= 135 => "F" + (key - 111), _ => "键 " + key
    };
}

public sealed class StudioOperationCollection : ObservableCollection<StudioOperation>
{
    public void ReplaceAll(IEnumerable<StudioOperation> operations)
    {
        Items.Clear();
        foreach (var operation in operations) Items.Add(operation);
        OnPropertyChanged(new PropertyChangedEventArgs(nameof(Count)));
        OnPropertyChanged(new PropertyChangedEventArgs("Item[]"));
        OnCollectionChanged(new NotifyCollectionChangedEventArgs(NotifyCollectionChangedAction.Reset));
    }
}

public sealed class ScriptStudioViewModel : ViewModelBase, IDisposable
{
    public ScriptStudioClient Client { get; } = new();
    public StudioOperationCollection Operations { get; } = new();
    public JsonNode? Document { get; private set; }
    public bool Dirty => Document?["dirty"]?.GetValue<bool>() == true;
    public string? File => Document?["file"]?.GetValue<string>();
    public string Name => Document?["name"]?.GetValue<string>() ?? "";
    public double Duration => Document?["duration_ms"]?.GetValue<double>() ?? 0;
    public bool Capturing { get; set; }
    public bool CaptureDraft => Document?["capture_draft"]?.GetValue<bool>() == true;
    public event Action? Changed;

    public void Apply(JsonNode snapshot)
    {
        if (snapshot["empty"]?.GetValue<bool>() == true) { Document = null; Operations.Clear(); }
        else
        {
            Document = snapshot;
            Operations.ReplaceAll((snapshot["operations"]?.AsArray() ?? new JsonArray())
                .Where(item => item != null).Select(item => new StudioOperation { Data = item!.DeepClone() }));
        }
        Changed?.Invoke();
    }
    public async Task ChangeAsync(string method, JsonObject? parameters = null) => Apply(await Client.CallAsync(method, parameters));
    public IReadOnlyList<string> Diagnostics => Document?["diagnostics"]?.AsArray().Select(n => n?.GetValue<string>() ?? "").ToList() ?? new List<string>();
    public void Dispose() => Client.Dispose();
}
