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
        Func<string?, string?, CancellationToken, Task<MaaJobStatus>> execute, Action stopOnFailure, CancellationToken token)
    {
        if (string.IsNullOrEmpty(entry) || string.IsNullOrEmpty(name)) return await execute(entry, overrides, token);
        try
        {
            overrides = TaskScriptPlan.NormalizeRepeatLimits(overrides ?? "{}", entry);
            using var library = new ScriptStudioClient(AppPaths.DataRoot, releaseCaptureOnDisconnect: false);
            var plan = (await library.CallAsync("execution_plan", new JsonObject
            {
                ["name"] = name, ["entry"] = entry,
                ["overrides"] = TaskRecoveryPolicy.MergePipelineOverrides(overrides),
                ["report_unbound"] = true
            }, token)).AsObject();
            token.ThrowIfCancellationRequested();
            TaskScriptPlan.StopIfUnbound(plan, message => processor.AddLog(message), token);
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
            if (plan["handled"]?.GetValue<bool>() != true) return await execute(entry, overrides, token);
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
            return await execute(prepared.Entry, prepared.Pipeline.ToJsonString(), token);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception error)
        {
            stopOnFailure();
            processor.AddLog("任务脚本未执行：" + error.Message);
            return MaaJobStatus.Failed;
        }
    }
}
