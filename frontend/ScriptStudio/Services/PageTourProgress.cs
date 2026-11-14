using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;

namespace MFAAvalonia.Services;

/// <summary>Only tutorial state, with stable page/step IDs. No business operations.</summary>
public sealed class PageTourProgress
{
    public const string Key = "PageTours.Progress";
    private readonly Action<string> _save;
    private readonly Action<string> _log;
    private readonly JsonObject _state;
    private readonly bool _writable;
    private readonly bool _needsInitialization;
    public bool Automatic { get; }

    public PageTourProgress(string? json, bool newInstallation, bool legacySeen, Action<string> save, Action<string> log)
    {
        _save = save; _log = log;
        _needsInitialization = string.IsNullOrWhiteSpace(json);
        try
        {
            _state = string.IsNullOrWhiteSpace(json)
                ? new JsonObject { ["version"] = 1, ["automatic"] = newInstallation && !legacySeen, ["pages"] = new JsonObject() }
                : JsonNode.Parse(json) as JsonObject ?? throw new FormatException("引导记录不是对象。");
            _writable = _state["version"]?.GetValue<int>() == 1 && _state["pages"] is JsonObject;
            Automatic = _writable && _state["automatic"]?.GetValue<bool>() == true;
        }
        catch (Exception error)
        {
            _state = new JsonObject { ["pages"] = new JsonObject() };
            _writable = false; Automatic = false;
            _log("引导记录未读取，原配置保留：" + error.Message);
        }
    }

    public void Initialize() { if (_writable && _needsInitialization) Persist(); }

    public int? StartIndex(string page, IReadOnlyList<string> steps, bool replay)
    {
        if (steps.Count == 0) return null;
        if (replay) return 0;
        if (!Automatic) return null;
        try
        {
            var node = _state["pages"]?[page];
            if (node == null) return 0;
            if (node is not JsonObject record) return null;
            if (record["version"]?.GetValue<int>() != 1 || record["status"]?.GetValue<string>() != "reading") return null;
            var id = record["step"]?.GetValue<string>();
            for (var i = 0; i < steps.Count; i++) if (steps[i] == id) return i;
            return 0; // A removed step resumes at the start, never at an unrelated array offset.
        }
        catch (Exception error) { _log("本页引导记录未读取：" + error.Message); return null; }
    }

    public void Record(string page, string step, string status)
    {
        if (!_writable) return;
        var pages = _state["pages"]!.AsObject();
        if (pages[page] != null && pages[page] is not JsonObject) return;
        if (pages[page] is not JsonObject record) { record = new JsonObject(); pages[page] = record; }
        record["version"] = 1; record["status"] = status; record["step"] = step;
        Persist();
    }

    private void Persist()
    {
        try { _save(_state.ToJsonString()); }
        catch (Exception error) { _log("引导进度未保存：" + error.Message); }
    }

    public static string ResetPages(string? json, IEnumerable<string> pages)
    {
        JsonObject state;
        try { state = JsonNode.Parse(json ?? "{}") as JsonObject ?? new JsonObject(); }
        catch { state = new JsonObject(); }
        state["version"] = 1; state["automatic"] = true;
        if (state["pages"] is not JsonObject records) { records = new JsonObject(); state["pages"] = records; }
        foreach (var page in pages)
        {
            if (records[page] is not JsonObject record) { records.Remove(page); continue; }
            record["version"] = 1; record["status"] = "reading"; record["step"] = "";
        }
        return state.ToJsonString();
    }

    public static bool IsFreshConfiguration(string directory, string globalFile)
    {
        try { return !File.Exists(globalFile) && (!Directory.Exists(directory) || !Directory.EnumerateFiles(directory, "*.json", SearchOption.AllDirectories).Any()); }
        catch { return false; } // Uncertain installations must not be forced through a new tour.
    }
}
