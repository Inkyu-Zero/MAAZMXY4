using System;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using MaaFramework.Binding;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Services;

/// <summary>Reuse the studio catalog and Maa execution for every configured task.</summary>
public static class ScriptTaskExecution
{
    public static async Task<MaaJobStatus> RunAsync(MaaProcessor processor, string? name, string? entry, string? overrides,
        Func<string?, string?, CancellationToken, Task<MaaJobStatus>> execute, Action stopOnFailure, CancellationToken token,
        NativeBoxSpeedSession? speedSession = null)
    {
        async Task<MaaJobStatus> RunNativeAsync(string? nativeEntry, string? parameters)
        {
            var status = await execute(nativeEntry, parameters, token);
            if (status == MaaJobStatus.Failed && !token.IsCancellationRequested)
                processor.AddLog($"error: {name ?? nativeEntry ?? "当前任务"}执行失败。请查看上一条节点日志，确认预览中显示的是任务起始界面后重试。{TaskAttempt.ScreenshotHint}");
            return status;
        }
        if (string.IsNullOrEmpty(entry) || string.IsNullOrEmpty(name)) return await RunNativeAsync(entry, overrides);
        try
        {
            overrides = TaskScriptPlan.NormalizeRepeatLimits(overrides ?? "{}", entry);
            JsonObject plan;
            // The returned plan owns managed JSON; its read-only worker has no
            // role during navigation, playback, retry, or recovery.
            using (var library = new ScriptStudioClient(AppPaths.DataRoot, releaseCaptureOnDisconnect: false))
            {
                plan = (await library.CallAsync("execution_plan", new JsonObject
                {
                    ["name"] = name, ["entry"] = entry,
                    ["overrides"] = TaskRecoveryPolicy.MergePipelineOverrides(overrides),
                    ["report_unbound"] = true
                }, token)).AsObject();
            }
            token.ThrowIfCancellationRequested();
            TaskScriptPlan.StopIfUnbound(plan, message => processor.AddLog(message), token);
            if (speedSession != null)
            {
                var nativeExecute = execute;
                execute = async (targetEntry, parameters, cancellation) =>
                {
                    if (targetEntry == "GameManagement_Resume")
                        return await nativeExecute(targetEntry, parameters, cancellation);
                    var proof = await speedSession.EnsureAsync(processor, plan["recording"], cancellation);
                    return await nativeExecute(targetEntry, NativeBoxSpeedSession.WithProof(parameters, proof), cancellation);
                };
            }
            if (TaskScriptPlan.IsAfkMode(overrides) && plan["binding"]?["mode"]?.GetValue<string>() == "replace")
                throw new InvalidOperationException("挂机模式需要原任务流程的战败与返回确认，请改为原流程绑定。");
            var management = GameManagementService.For(processor);
            if ((management.Options.Enabled || management.IsArmed) && plan["management_completion_only"]?.GetValue<bool>() == true)
            {
                stopOnFailure();
                if (plan["binding"]?["mode"]?.GetValue<string>() == "replace")
                    throw new InvalidOperationException("替代答题任务的录制没有结算检查点，不能自动重启。");
                return await management.RunCompletionOnlyAsync(entry, overrides, execute, token);
            }
            if (plan["handled"]?.GetValue<bool>() != true) return await RunNativeAsync(entry, overrides);
            stopOnFailure();
            if (management.Options.Enabled || management.IsArmed)
            {
                if (plan["binding"]?["mode"]?.GetValue<string>() == "replace")
                    throw new InvalidOperationException("替代任务脚本没有地图恢复边界，请使用原任务流程绑定或关闭游戏管理。");
                return await management.RunAsync(plan, entry, overrides, execute, token);
            }
            var prepared = TaskScriptPlan.Prepare(plan, entry, overrides);
            if (prepared.Profile != null)
            {
                // The visible game-management switches replace the old hidden
                // per-task restart option; retain its saved data for migration.
                TaskScriptPlan.Parameters(prepared.Pipeline, prepared.Entry)["recovery"] =
                    new JsonObject { ["enabled"] = false, ["max_retries"] = 3, ["save_slot"] = 1, ["exe_path"] = "" };
                return await TaskRecovery.RunAsync(processor, prepared.Entry, prepared.Pipeline.ToJsonString(), prepared.Profile, execute, token);
            }
            return await RunNativeAsync(prepared.Entry, prepared.Pipeline.ToJsonString());
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception error)
        {
            stopOnFailure();
            processor.AddLog($"error: 任务脚本未执行。{error.Message} {TaskAttempt.ScreenshotHint}");
            return MaaJobStatus.Failed;
        }
    }
}
