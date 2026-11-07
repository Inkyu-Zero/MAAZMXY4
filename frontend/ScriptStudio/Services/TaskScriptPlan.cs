using System;
using System.IO;
using System.Text.Json.Nodes;
using System.Threading;

namespace MFAAvalonia.Services;

/// <summary>Turn catalog capabilities and saved bindings into native Pipeline overrides.</summary>
public static class TaskScriptPlan
{
    public static bool IsAfkMode(string? overrides) =>
        TaskRecoveryPolicy.MergePipelineOverrides(overrides)["脚本任务设置"]?["action"]?["param"]?["custom_action_param"]?["afk_mode"]?.GetValue<bool>() == true;

    public static string NormalizeRepeatLimits(string overrides, string? repeatEntry = null, bool unlimitedHostRepeat = false)
    {
        var root = JsonNode.Parse(overrides);
        var merged = TaskRecoveryPolicy.MergePipelineOverrides(overrides);
        var nativeLimit = repeatEntry == null ? null : merged[repeatEntry]?["max_hit"];
        var nativeUnlimited = nativeLimit != null && long.TryParse(nativeLimit.ToString(), out var count)
            && count is -1 or uint.MaxValue;
        var afk = IsAfkMode(overrides) || nativeUnlimited || unlimitedHostRepeat;
        var changed = afk;
        if (afk)
        {
            root = merged;
            Parameters(root.AsObject(), "脚本任务设置")["afk_mode"] = true;
            Parameters(root.AsObject(), "脚本任务设置")["failure_retry"] =
                new JsonObject { ["enabled"] = true, ["max_retries"] = -1 };
            if (repeatEntry != null && root[repeatEntry] is JsonObject repeated && repeated.ContainsKey("max_hit"))
                repeated["max_hit"] = uint.MaxValue;
        }
        void Normalize(JsonNode? value)
        {
            if (value is JsonArray array)
            {
                foreach (var item in array) Normalize(item);
                return;
            }
            if (value is not JsonObject pipeline) return;
            foreach (var item in pipeline)
            {
                if (item.Value is not JsonObject node || node["max_hit"] is not JsonValue limit
                    || !limit.TryGetValue<long>(out var count)) continue;
                if (count < -1) throw new ArgumentException("刷取次数只能是正整数或 -1（无限）。");
                if (count == -1)
                {
                    // Maa's documented unlimited max_hit sentinel is UINT_MAX.
                    node["max_hit"] = uint.MaxValue;
                    changed = true;
                }
            }
        }
        Normalize(root);
        return changed ? root!.ToJsonString() : overrides;
    }

    public static void StopIfUnbound(JsonObject plan, Action<string> log, CancellationToken token)
    {
        if (plan["stop_reason"]?.GetValue<string>() != "unbound_script") return;
        token.ThrowIfCancellationRequested();
        log(plan["message"]?.GetValue<string>() ?? "任务尚未绑定脚本，本次任务已结束，请绑定后再开始。");
        // MFATask already treats cancellation as STOPPED: no failed-task toast,
        // no completed iteration, and no execution of later queued tasks.
        throw new OperationCanceledException(token);
    }

    public static JsonObject Parameters(JsonObject pipeline, string nodeName)
    {
        if (pipeline[nodeName] is not JsonObject node) pipeline[nodeName] = node = new();
        if (node["action"] is not JsonObject action) node["action"] = action = new();
        if (action["param"] is not JsonObject param) action["param"] = param = new();
        if (param["custom_action_param"] is not JsonObject values) param["custom_action_param"] = values = new();
        return values;
    }

    public static (string Entry, JsonObject Pipeline, JsonObject? Profile) Prepare(JsonObject plan, string entry, string? overrides)
    {
        var pipeline = TaskRecoveryPolicy.MergePipelineOverrides(overrides);
        var binding = plan["binding"] as JsonObject;
        var recording = plan["recording"] as JsonObject;
        var profile = plan["profile"] as JsonObject;
        if (binding?["mode"]?.GetValue<string>() == "replace")
        {
            var replacement = "ScriptPlayback_" + Guid.NewGuid().ToString("N");
            var playback = Playback(recording);
            return (replacement, new JsonObject { [replacement] = new JsonObject
            {
                ["action"] = new JsonObject { ["type"] = "Custom", ["param"] = new JsonObject
                { ["custom_action"] = "battle_play", ["custom_action_param"] = playback } },
                ["next"] = new JsonArray(), ["post_delay"] = 0
            } }, null);
        }
        if (profile != null)
        {
            var defaults = profile["parameters"]?.DeepClone() ?? new JsonObject();
            var settingNode = defaults["execution"]?["settings_node"]?.GetValue<string>();
            var current = Parameters(pipeline, entry).DeepClone();
            var settings = settingNode == null ? new JsonObject() : Parameters(pipeline, settingNode).DeepClone();
            var complete = TaskRecoveryPolicy.MergePipelineOverrides(new JsonArray(defaults, current, settings).ToJsonString());
            complete["phase"] = "begin";
            complete["task_binding_id"] = plan["id"]!.DeepClone();
            if (binding != null)
            {
                complete["file"] = recording!["path"]!.DeepClone();
                complete["expected_recording_file"] = recording["file"]!.DeepClone();
                complete["expected_recording_sha256"] = recording["sha256"]!.DeepClone();
            }
            var parameters = Parameters(pipeline, entry);
            parameters.Clear();
            foreach (var item in complete) parameters[item.Key] = item.Value?.DeepClone();
        }
        else if (binding != null)
        {
            var nodes = plan["playback_nodes"]?.AsArray() ?? throw new InvalidDataException("任务缺少回放节点。");
            if (nodes.Count == 0) throw new InvalidDataException("任务没有回放节点，请使用替代任务方式。");
            foreach (var node in nodes)
            {
                var name = node!.GetValue<string>();
                var parameters = Parameters(pipeline, name);
                var defaults = plan["playback_parameters"]?[name]?.DeepClone() ?? new JsonObject();
                var combined = TaskRecoveryPolicy.MergePipelineOverrides(new JsonArray(defaults, parameters.DeepClone()).ToJsonString());
                foreach (var value in combined) parameters[value.Key] = value.Value?.DeepClone();
                foreach (var value in Playback(recording)) parameters[value.Key] = value.Value?.DeepClone();
                var action = pipeline[name]!["action"]!.AsObject();
                action["type"] = "Custom";
                action["param"]!["custom_action"] = "battle_play";
                var includesReturn = pipeline["脚本任务设置"]?["action"]?["param"]?["custom_action_param"]?["exit_mode"]?.GetValue<string>() == "recorded_return";
                if (includesReturn)
                {
                    var next = plan["playback_routes"]?[name]?["recorded_return_next"] as JsonArray;
                    if (next == null || next.Count == 0)
                        throw new InvalidDataException("任务资源缺少录制返回确认流程，请更新本项目完整客户端；自定义任务需配置 script_binding.recorded_return_next。");
                    pipeline[name]!["next"] = next.DeepClone();
                }
            }
        }
        return (entry, pipeline, profile);
    }

    public static (string Entry, JsonObject Pipeline, JsonObject Profile, long Limit) PrepareManaged(
        JsonObject plan, string originalEntry, string? overrides, GameManagementOptions options)
    {
        if (plan["binding"]?["mode"]?.GetValue<string>() != "pipeline")
            throw new InvalidDataException("托管续跑需要绑定原任务流程中的脚本。");
        var profile = plan["management_profile"]?.DeepClone() as JsonObject
            ?? throw new InvalidDataException("该任务没有可确认的单轮边界。");
        var nativeRepeat = profile["native_repeat"]?.GetValue<bool>() == true;
        var limit = nativeRepeat
            ? long.Parse(profile["repeat_limit"]!.ToString(), System.Globalization.CultureInfo.InvariantCulture) : 1;
        overrides = NormalizeRepeatLimits(overrides ?? "{}", originalEntry, nativeRepeat && limit is -1 or uint.MaxValue);
        var prepared = Prepare(plan, originalEntry, overrides);
        var entry = profile["entry"]!.GetValue<string>();
        var pipeline = prepared.Pipeline;
        var shared = pipeline["脚本任务设置"]?["action"]?["param"]?["custom_action_param"]?.DeepClone() ?? new JsonObject();
        var parameters = TaskRecoveryPolicy.MergePipelineOverrides(new JsonArray(
            profile["parameters"]!.DeepClone(), Parameters(pipeline, entry).DeepClone(), shared).ToJsonString());
        parameters["phase"] = "begin";
        parameters["game_management"] = new JsonObject { ["restart_on_failure"] = options.RestartOnFailure };
        parameters["file"] = plan["recording"]!["path"]!.DeepClone();
        parameters["expected_recording_file"] = plan["recording"]!["file"]!.DeepClone();
        parameters["expected_recording_sha256"] = plan["recording"]!["sha256"]!.DeepClone();
        var destination = Parameters(pipeline, entry); destination.Clear();
        foreach (var pair in parameters) destination[pair.Key] = pair.Value?.DeepClone();
        var success = profile["success_node"]!.GetValue<string>();
        if (pipeline[success] is not JsonObject) pipeline[success] = new JsonObject();
        pipeline[success]!["next"] = new JsonArray();
        profile["resume_parameters"] = new JsonObject
        {
            ["account_name"] = options.AccountName, ["role_name"] = options.RoleName, ["save_slot"] = options.SaveSlot
        };
        if (nativeRepeat && IsAfkMode(overrides)) limit = -1;
        if (limit == -1 || limit == uint.MaxValue) limit = long.MaxValue;
        if (limit < 1) throw new InvalidDataException("托管任务的刷取次数无效。");
        pipeline = JsonNode.Parse(NormalizeRepeatLimits(pipeline.ToJsonString()))!.AsObject();
        return (entry, pipeline, profile, limit);
    }

    private static JsonObject Playback(JsonObject? recording)
    {
        if (recording == null) throw new InvalidDataException("缺少已校验的任务脚本。");
        return new JsonObject { ["file"] = recording["path"]!.DeepClone(),
            ["expected_sha256"] = recording["sha256"]!.DeepClone(), ["strict_task"] = true,
            ["expected_short_side"] = 720, ["speed"] = 1.0 };
    }
}
