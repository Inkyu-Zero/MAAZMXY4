using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Services;

/// <summary>One queue task owns native speed across repetitions and verified restarts.</summary>
public sealed class NativeBoxSpeedSession
{
    private ScriptStudioClient? _client;
    private ManagedGameTarget? _target;
    private JsonObject? _proof;
    private string? _file, _revision, _exe;
    private readonly int _speed;

    public NativeBoxSpeedSession(int speed)
    {
        if (speed is not (1 or 2)) throw new ArgumentException("盒子倍速只支持 1 倍或 2 倍。");
        _speed = speed;
    }

    public static int Read(string? parameters, string? entry)
    {
        var pipeline = TaskRecoveryPolicy.MergePipelineOverrides(parameters);
        var value = entry == GenericScriptPlayback.Entry
            ? pipeline[entry]?["attach"]?["box_speed"]
            : pipeline["脚本任务设置"]?["action"]?["param"]?["custom_action_param"]?["box_speed"];
        var speed = value?.GetValue<int>() ?? 1;
        if (speed is not (1 or 2)) throw new InvalidDataException("盒子倍速只支持 1 倍或 2 倍。");
        return speed;
    }

    public async Task<JsonObject?> EnsureAsync(MaaProcessor processor, JsonNode? recording, CancellationToken token)
    {
        if (_speed == 1) return null;
        if (recording == null) throw new InvalidDataException("倍速需要已绑定并校验的脚本。");
        var file = recording["file"]!.GetValue<string>();
        var revision = recording["sha256"]!.GetValue<string>();
        if (_file != null && (_file != file || _revision != revision))
            throw new InvalidOperationException("本次倍速任务的脚本或绑定已改变，请重新开始任务。");
        _file = file; _revision = revision;
        if (!OperatingSystem.IsWindows() || processor.ViewModel?.IsCurrentWin32WindowValid() != true)
            throw new InvalidOperationException("原生倍速需要明确选择造梦盒子的 Win32 窗口。");
        var hwnd = processor.Config.DesktopWindow.HWnd;
        GetWindowThreadProcessId(hwnd, out var pid);
        using var process = Process.GetProcessById(checked((int)pid));
        var path = process.MainModule?.FileName ?? throw new InvalidOperationException("无法读取盒子程序路径。");
        var selected = TaskRecovery.CaptureManagedTarget(processor, path);
        if (selected.Window != hwnd) throw new InvalidOperationException("任务控制器与所选窗口不一致。");
        if (_exe != null && !string.Equals(Path.GetFullPath(path), _exe, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("恢复后的盒子程序与本任务不一致。");
        _exe = Path.GetFullPath(path);
        token.ThrowIfCancellationRequested();
        if (_client != null)
        {
            // Keep the owner channel alive when a user cancels, so cleanup can
            // still request/confirm 1x instead of disposing it mid-response.
            var status = await _client.CallAsync("box_speed_status");
            token.ThrowIfCancellationRequested();
            if (status["state"]?.GetValue<string>() == "ready")
            {
                _target!.Process.RequireSame(selected.Process);
                if (_target.Window != selected.Window) throw new InvalidOperationException("倍速任务期间窗口发生变化。");
                return _proof?.DeepClone().AsObject();
            }
            if (status["state"]?.GetValue<string>() != "lost")
                throw new InvalidOperationException(status["error"]?.GetValue<string>() is { Length: > 0 } message ? message : "盒子倍速会话已中断。");
            // The old process exited. Only the newly selected, verified same
            // executable may acquire a fresh lease after normal login recovery.
            await CloseAsync();
        }
        _target = selected;
        _client = new ScriptStudioClient(AppPaths.DataRoot, releaseCaptureOnDisconnect: false, shutdownTimeoutMs: 10000);
        await _client.CallAsync("box_speed_start", new JsonObject
        {
            ["speed"] = _speed, ["hwnd"] = (long)selected.Window,
            ["pid"] = selected.Process.Pid, ["started_utc_ticks"] = selected.Process.StartedUtcTicks,
            ["exe_path"] = selected.Process.ExePath, ["file"] = file, ["sha256"] = revision
        });
        var deadline = DateTime.UtcNow.AddSeconds(15);
        while (DateTime.UtcNow < deadline)
        {
            token.ThrowIfCancellationRequested();
            var status = await _client.CallAsync("box_speed_status");
            token.ThrowIfCancellationRequested();
            var state = status["state"]?.GetValue<string>();
            if (state == "ready")
            {
                if (status["hwnd"]?.GetValue<long>() != (long)selected.Window
                    || status["pid"]?.GetValue<int>() != selected.Process.Pid
                    || status["started_utc_ticks"]?.GetValue<long>() != selected.Process.StartedUtcTicks
                    || status["speed"]?.GetValue<int>() != _speed || status["sha256"]?.GetValue<string>() != revision)
                    throw new InvalidDataException("盒子倍速确认与当前任务不一致。");
                _proof = status.DeepClone().AsObject();
                processor.AddLog("盒子 2 倍速已确认，脚本时序同步为 2 倍；任务结束后恢复 1 倍。");
                return _proof.DeepClone().AsObject();
            }
            if (state != "starting")
                throw new InvalidOperationException(status["error"]?.GetValue<string>() is { Length: > 0 } error ? error : "盒子未确认进入倍速。");
            await Task.Delay(50, token);
        }
        throw new TimeoutException("盒子倍速准备超时，本次未播放脚本。");
    }

    public static string WithProof(string? parameters, JsonObject? proof)
    {
        var pipeline = TaskRecoveryPolicy.MergePipelineOverrides(parameters);
        var settings = TaskScriptPlan.Parameters(pipeline, "脚本任务设置");
        settings["box_speed"] = proof == null ? 1 : 2;
        settings["box_speed_proof"] = proof?.DeepClone();
        // Whole-task replacement has no shared round initializer.
        foreach (var item in pipeline)
            if (item.Value?["action"]?["param"]?["custom_action"]?.GetValue<string>() == "battle_play")
            {
                var playback = TaskScriptPlan.Parameters(pipeline, item.Key);
                playback["box_speed_proof"] = proof?.DeepClone();
                if (proof != null) playback["speed"] = 2;
            }
        return pipeline.ToJsonString();
    }

    public async Task CloseAsync()
    {
        var client = _client;
        if (client == null) return;
        try
        {
            if (client.IsDisconnected) throw new IOException("倍速服务已断开，无法确认恢复 1 倍速；请在盒子按数字行 1。");
            var status = await client.CallAsync("box_speed_stop");
            var deadline = DateTime.UtcNow.AddSeconds(8);
            while (status["state"]?.GetValue<string>() == "stopping" && DateTime.UtcNow < deadline)
            {
                await Task.Delay(50);
                status = await client.CallAsync("box_speed_status");
            }
            if (status["state"]?.GetValue<string>() is not ("stopped" or "lost" or "idle")
                && !(status["state"]?.GetValue<string>() == "failed" && status["restored"]?.GetValue<bool>() == true))
                throw new InvalidOperationException(status["error"]?.GetValue<string>() is { Length: > 0 } error
                    ? error : "尚未确认盒子恢复 1 倍速，已停止后续任务。");
        }
        finally { client.Dispose(); _client = null; _proof = null; _target = null; }
    }

    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint window, out uint pid);
}
