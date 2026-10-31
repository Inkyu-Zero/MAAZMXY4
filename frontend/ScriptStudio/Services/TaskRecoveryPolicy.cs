using System;
using System.IO;
using System.Collections.Generic;
using System.Linq;
using System.Globalization;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.Services;

public enum TaskAttemptStatus { Success, Ready, Fatal, Recoverable, Cancelled }

public sealed record TaskAttempt(TaskAttemptStatus Status, string Reason = "")
{
    public static TaskAttempt Fatal(string reason) => new(TaskAttemptStatus.Fatal, reason);
}

public interface ITaskRestartLifecycle
{
    Task StopInputsAndAgentAsync(CancellationToken token);
    Task StopSelectedProcessAsync(CancellationToken token);
    Task StartSelectedProcessAsync(CancellationToken token);
    Task BindUniqueWindowAsync(CancellationToken token);
    Task ReconnectAsync(CancellationToken token);
    Task<TaskAttempt> ResumeAsync(int slot, CancellationToken token);
}

/// <summary>A recovery retry is still the same round and must not adopt a newly edited recording.</summary>
public sealed class TaskRecordingRevision
{
    public string? FileName { get; private set; }
    public string? Sha256 { get; private set; }

    public void Observe(JsonObject checkpoint)
    {
        if (checkpoint["recording_file"] == null && checkpoint["recording_sha256"] == null) return;
        var file = checkpoint["recording_file"]?.GetValue<string>();
        var hash = checkpoint["recording_sha256"]?.GetValue<string>();
        if (string.IsNullOrWhiteSpace(file) || Path.GetFileName(file) != file || file.IndexOfAny(new[] { '/', '\\', ':' }) >= 0
            || string.IsNullOrEmpty(hash) || hash.Length != 64 || !hash.All(Uri.IsHexDigit))
            throw new InvalidDataException("本轮录制版本标识无效。");
        if (FileName != null && (FileName != file || !string.Equals(Sha256, hash, StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("recording_changed：本轮恢复期间脚本或绑定已修改，请重新开始任务。");
        FileName = file;
        Sha256 = hash.ToLowerInvariant();
    }

    public void Apply(JsonObject parameters)
    {
        if (FileName == null) return;
        parameters["expected_recording_file"] = FileName;
        parameters["expected_recording_sha256"] = Sha256;
    }
}

public sealed record TaskRecoveryOptions(bool Enabled, int MaxRetries, int SaveSlot, string ExePath)
{
    public static TaskRecoveryOptions Read(JsonObject parameters)
    {
        var policy = parameters["recovery"] as JsonObject;
        var enabled = policy?["enabled"]?.GetValue<bool>() ?? false;
        var retries = ReadInputInteger(policy?["max_retries"], 3);
        var slot = ReadInputInteger(policy?["save_slot"], 1);
        var path = policy?["exe_path"]?.GetValue<string>()?.Trim() ?? "";
        if (retries is < 1 or > 3 || slot is < 1 or > 99)
            throw new InvalidDataException("任务恢复次数必须为1～3，存档必须为1～99。");
        if (enabled && (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path)
            || !path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("启用任务恢复前，请填写游戏窗口所属程序的完整.EXE路径。");
        return new(enabled, retries, slot, path);
    }

    private static int ReadInputInteger(JsonNode? value, int fallback)
    {
        if (value == null) return fallback;
        if (value is JsonValue item)
        {
            if (item.TryGetValue<int>(out var numeric)) return numeric;
            if (item.TryGetValue<string>(out var text)
                && int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out numeric)) return numeric;
        }
        throw new InvalidDataException("任务恢复次数和存档必须是整数。");
    }
}

public sealed record TaskProcessIdentity(int Pid, long StartedUtcTicks, string ExePath)
{
    public void RequireSame(TaskProcessIdentity current)
    {
        if (Pid != current.Pid || StartedUtcTicks != current.StartedUtcTicks
            || !string.Equals(Path.GetFullPath(ExePath), Path.GetFullPath(current.ExePath), StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("游戏进程身份已变化，拒绝结束或替换其它进程。");
    }

    public void RequireConfiguredPath(string configuredPath)
    {
        if (!string.Equals(Path.GetFullPath(ExePath), Path.GetFullPath(configuredPath), StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("恢复程序路径与所选游戏窗口所属进程不一致，请指定实际游戏EXE。");
    }
}

public sealed record TaskWindowIdentity(string Title, string ClassName)
{
    public bool IsValid => !string.IsNullOrWhiteSpace(Title) && !string.IsNullOrWhiteSpace(ClassName);
    public bool Matches(TaskWindowIdentity other) => IsValid && this == other;
}

public sealed record ManagedGameTarget(TaskProcessIdentity Process, nint Window, TaskWindowSize ClientSize,
    TaskWindowIdentity WindowIdentity);

public sealed record TaskWindowCandidate(nint Handle, int Pid, TaskWindowIdentity Identity,
    TaskWindowSize ClientSize, TaskWindowSize OuterSize, bool Visible, bool GeometryConsistent = true)
{
    public bool HasUsableBounds => GeometryConsistent && ClientSize.IsValid && OuterSize.IsValid
        && OuterSize.Width >= ClientSize.Width && OuterSize.Height >= ClientSize.Height;
}

/// <summary>A process can expose a temporary AIR window before its real game window.</summary>
public sealed class TaskWindowReadiness(int expectedPid, TaskWindowIdentity? expectedIdentity)
{
    private TaskWindowCandidate? _candidate;
    private TimeSpan _since;

    public bool MatchesTarget(TaskWindowCandidate window) => window.Handle != 0 && window.Pid == expectedPid
        && window.Visible && window.Identity.IsValid
        && (expectedIdentity != null ? expectedIdentity.Matches(window.Identity)
            : window.Identity.Title.StartsWith("造梦盒子", StringComparison.Ordinal));

    public TaskWindowCandidate? Observe(IEnumerable<TaskWindowCandidate> windows, TimeSpan elapsed)
    {
        // Keep uniqueness after identity filtering: an AIR splash is not a game
        // candidate, but two real game windows must never be resolved by order.
        var selected = TaskRecoveryPolicy.SelectUniqueWindow(windows.Where(MatchesTarget), w => w.Pid, expectedPid);
        if (!selected.Found || !selected.Window.HasUsableBounds)
        {
            _candidate = null;
            return null;
        }
        if (_candidate != selected.Window)
        {
            _candidate = selected.Window;
            _since = elapsed;
        }
        return elapsed - _since >= TimeSpan.FromSeconds(1) ? _candidate : null;
    }
}

/// <summary>Use one synchronous coordinate context; never carry a thread DPI change across await.</summary>
public static class TaskWindowCoordinates
{
    public static T InPhysicalPixels<T>(Func<nint, nint> setContext, Func<T> action)
    {
        var previous = setContext(new nint(-4)); // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if (previous == 0) throw new InvalidOperationException("无法统一游戏窗口的 DPI 坐标，未调整窗口。");
        try { return action(); }
        finally { setContext(previous); }
    }
}

public readonly record struct TaskWindowSize(int Width, int Height)
{
    public bool IsValid => Width is >= 1 and <= 32768 && Height is >= 1 and <= 32768;
    public bool SameNormalizedFrame(TaskWindowSize other)
    {
        Validate(); other.Validate();
        // Maa normalizes the short side to 720. Uniform AIR/DPI scaling is
        // harmless; changing the aspect ratio would change recorded positions.
        double scale = 720.0 / Math.Min(Width, Height);
        double otherScale = 720.0 / Math.Min(other.Width, other.Height);
        return Math.Abs(Math.Round(Width * scale) - Math.Round(other.Width * otherScale)) <= 1
            && Math.Abs(Math.Round(Height * scale) - Math.Round(other.Height * otherScale)) <= 1;
    }
    public void Validate()
    {
        if (!IsValid)
            throw new InvalidOperationException("游戏窗口尺寸无效，自动恢复已停止。");
    }

    public TaskWindowSize OuterSizeFor(TaskWindowSize currentClient, TaskWindowSize currentOuter)
    {
        Validate(); currentClient.Validate(); currentOuter.Validate();
        if (currentOuter.Width < currentClient.Width || currentOuter.Height < currentClient.Height)
            throw new InvalidOperationException("游戏窗口边框尺寸无效。");
        var target = new TaskWindowSize(checked(currentOuter.Width + Width - currentClient.Width),
            checked(currentOuter.Height + Height - currentClient.Height));
        target.Validate();
        return target;
    }
}

/// <summary>One outer MFA iteration owns its recovery budget; failures never complete an iteration.</summary>
public static class TaskRecoveryPolicy
{
    /// <summary>Wait for the old job to drain before allowing replacement of a lost game window.</summary>
    public static async Task<(T Result, bool TargetLost)> ObserveExecutionAsync<T>(Task<T> execution,
        Func<bool> targetLost, Action stopOldTask, CancellationToken token, TimeSpan? poll = null)
    {
        while (!execution.IsCompleted)
        {
            token.ThrowIfCancellationRequested();
            await Task.WhenAny(execution, Task.Delay(poll ?? TimeSpan.FromMilliseconds(500), token));
            token.ThrowIfCancellationRequested();
            if (execution.IsCompleted || !targetLost()) continue;
            stopOldTask();
            var stopped = await execution.WaitAsync(TimeSpan.FromSeconds(10), token);
            token.ThrowIfCancellationRequested();
            return (stopped, true);
        }
        return (await execution, false);
    }

    /// <summary>MFA serializes ordered option overrides as an array, even when Maa accepts one object.</summary>
    public static JsonObject MergePipelineOverrides(string? json)
    {
        var parsed = JsonNode.Parse(string.IsNullOrWhiteSpace(json) ? "{}" : json);
        if (parsed is JsonObject single) return (JsonObject)single.DeepClone();
        if (parsed is not JsonArray options)
            throw new InvalidDataException("任务任务参数必须是JSON对象或有序选项数组。");
        var result = new JsonObject();
        foreach (var option in options)
        {
            if (option is not JsonObject values)
                throw new InvalidDataException("任务任务的每个选项覆盖必须是JSON对象。");
            MergeObject(result, values);
        }
        return result;
    }

    private static void MergeObject(JsonObject destination, JsonObject source)
    {
        foreach (var item in source)
        {
            if (destination[item.Key] is JsonObject current && item.Value is JsonObject next)
                MergeObject(current, next);
            else
                destination[item.Key] = item.Value?.DeepClone(); // Lists such as next replace, never concatenate.
        }
    }

    public static void CompleteParameters(JsonObject parameters, TaskRecoveryOptions options, bool resume)
    {
        parameters["phase"] = resume ? "recovery_begin" : "begin";
        parameters["recovery"] = new JsonObject
        {
            ["enabled"] = options.Enabled, ["max_retries"] = options.MaxRetries,
            ["save_slot"] = options.SaveSlot, ["exe_path"] = options.ExePath
        };
        if (resume) parameters["save_slot"] = options.SaveSlot;
    }

    public static async Task<TaskAttempt> RestartAsync(ITaskRestartLifecycle lifecycle, int slot, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        await lifecycle.StopInputsAndAgentAsync(token);
        token.ThrowIfCancellationRequested();
        await lifecycle.StopSelectedProcessAsync(token);
        token.ThrowIfCancellationRequested();
        await lifecycle.StartSelectedProcessAsync(token);
        token.ThrowIfCancellationRequested();
        await lifecycle.BindUniqueWindowAsync(token);
        token.ThrowIfCancellationRequested();
        await lifecycle.ReconnectAsync(token);
        token.ThrowIfCancellationRequested();
        var result = await lifecycle.ResumeAsync(slot, token);
        token.ThrowIfCancellationRequested();
        return result;
    }

    public static void RequireExclusivePid(int pid, IEnumerable<int> otherRunningPids)
    {
        if (otherRunningPids.Contains(pid))
            throw new InvalidOperationException("其它运行实例也在使用该游戏进程，自动恢复已停止。");
    }

    public static (bool Found, T Window) SelectUniqueWindow<T>(IEnumerable<T> windows, Func<T, int> pid, int expectedPid)
    {
        var matching = windows.Where(window => pid(window) == expectedPid).Take(2).ToArray();
        if (matching.Length > 1)
            throw new InvalidOperationException("重启后匹配到多个游戏窗口，拒绝猜测目标。");
        return matching.Length == 1 ? (true, matching[0]) : (false, default!);
    }

    public static async Task<TaskAttempt> RunRoundAsync(
        TaskRecoveryOptions options,
        Func<CancellationToken, Task<TaskAttempt>> runRound,
        Func<int, int, CancellationToken, Task<TaskAttempt>> recover,
        CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var outcome = await runRound(token);
        var restarts = 0;
        while (outcome.Status == TaskAttemptStatus.Recoverable && options.Enabled)
        {
            token.ThrowIfCancellationRequested();
            if (outcome.Reason is not ("white_screen" or "window_lost" or "battle_failed" or "battle_retry_exhausted" or "battle_return_failed" or "task_failed"))
                return TaskAttempt.Fatal("不允许自动恢复的故障：" + outcome.Reason);
            // Game defeats retry inside the same running Pipeline. Process
            // recovery has a separate budget even for an unlimited task.
            if (restarts >= options.MaxRetries)
                return TaskAttempt.Fatal("本轮连续重启恢复失败，已达到上限，请检查游戏状态。");
            restarts++;
            outcome = await recover(options.SaveSlot, restarts, token);
            token.ThrowIfCancellationRequested();
            if (outcome.Status == TaskAttemptStatus.Ready)
                outcome = await runRound(token);
        }
        token.ThrowIfCancellationRequested();
        return outcome;
    }

    public static TaskAttempt ReadCheckpoint(JsonObject state, string runId, string instanceId, bool resume)
    {
        if (!CheckpointBelongsTo(state, runId, instanceId))
            return TaskAttempt.Fatal("任务状态标识不匹配，未确认本轮完成。");
        var reason = state["reason"]?.GetValue<string>() ?? "";
        return state["status"]?.GetValue<string>() switch
        {
            "success" when !resume => new(TaskAttemptStatus.Success),
            "ready" when resume => new(TaskAttemptStatus.Ready),
            "recoverable" when reason is "white_screen" or "window_lost" or "battle_failed" or "battle_retry_exhausted" or "battle_return_failed" or "task_failed" => new(TaskAttemptStatus.Recoverable, reason),
            "cancelled" => new(TaskAttemptStatus.Cancelled, reason),
            "fatal" => TaskAttempt.Fatal(reason),
            _ => TaskAttempt.Fatal("任务没有留下可验证的成功状态。")
        };
    }

    public static bool CheckpointBelongsTo(JsonObject state, string runId, string instanceId) =>
        state["version"]?.GetValue<int>() == 1 && state["run_id"]?.GetValue<string>() == runId
        && state["instance_id"]?.GetValue<string>() == instanceId;

    public static bool IsCaptureLossCandidate(JsonObject state, string runId, string instanceId) =>
        CheckpointBelongsTo(state, runId, instanceId)
        && (state["status"]?.GetValue<string>() == "running"
            || (state["status"]?.GetValue<string>() == "fatal" && state["reason"]?.GetValue<string>() == "capture_unavailable"));
}
