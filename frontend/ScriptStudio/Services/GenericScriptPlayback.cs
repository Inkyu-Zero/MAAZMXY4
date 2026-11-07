using System;
using System.IO;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using MaaFramework.Binding;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Services;

/// <summary>One host task instance: queue owns repetition, studio owns input.</summary>
public sealed class GenericScriptPlayback
{
    public const string Entry = "GenericScriptPlayback";
    private string? _revision;
    private string? _file;

    public static bool IsContinuous(string? parameters) =>
        TaskRecoveryPolicy.MergePipelineOverrides(parameters)[Entry]?["attach"]?["continuous"]?.GetValue<bool>() == true;

    public async Task<MaaJobStatus> RunAsync(MaaProcessor processor, string name, CancellationToken token,
        NativeBoxSpeedSession? speedSession = null)
    {
        using var client = new ScriptStudioClient(AppPaths.DataRoot, releaseCaptureOnDisconnect: false);
        var started = false;
        try
        {
            if (!OperatingSystem.IsWindows() || processor.ViewModel?.IsCurrentWin32WindowValid() != true)
                throw new InvalidOperationException("请先明确选择一个有效的游戏窗口。");
            var plan = (await client.CallAsync("execution_plan", new JsonObject
            { ["name"] = name, ["entry"] = Entry, ["report_unbound"] = true }, token)).AsObject();
            TaskScriptPlan.StopIfUnbound(plan, message => processor.AddLog(message), token);
            var recording = plan["recording"] ?? throw new InvalidDataException("任务未绑定有效脚本。");
            var revision = recording["sha256"]!.GetValue<string>();
            var file = recording["file"]!.GetValue<string>();
            if (_revision != null && (_revision != revision || _file != file))
                throw new InvalidOperationException("本次连续播放的绑定或脚本已改变，请重新开始任务。");
            _revision = revision; _file = file;
            var speedProof = speedSession == null ? null : await speedSession.EnsureAsync(processor, recording, token);
            token.ThrowIfCancellationRequested();
            started = true; // Also clean up a request cancelled before its reply arrives.
            await client.CallAsync("playback_start", new JsonObject
            {
                ["file"] = file, ["sha256"] = revision,
                ["hwnd"] = (long)processor.Config.DesktopWindow.HWnd,
                ["mouse"] = processor.Config.DesktopWindow.Mouse.ToString(),
                ["keyboard"] = processor.Config.DesktopWindow.KeyBoard.ToString(),
                ["speed"] = speedProof == null ? 1 : 2, ["box_speed_proof"] = speedProof
            }, token);
            while (true)
            {
                token.ThrowIfCancellationRequested();
                var result = await client.CallAsync("playback_status", cancellationToken: token);
                var state = result["state"]?.GetValue<string>();
                if (state == "succeeded") return MaaJobStatus.Succeeded;
                if (state != "running")
                    throw new InvalidOperationException(result["error"]?.GetValue<string>() is { Length: > 0 } error
                        ? error : "脚本播放已中断，本次未计为完成。");
                await Task.Delay(100, token);
            }
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception error)
        {
            processor.AddLog("通用脚本播放已停止：" + error.Message);
            return MaaJobStatus.Failed;
        }
        finally
        {
            if (started && !client.IsDisconnected)
                await client.CallAsync("playback_stop");
        }
    }
}
