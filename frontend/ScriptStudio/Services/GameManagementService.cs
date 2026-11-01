using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using System.Xml.Linq;
using MaaFramework.Binding;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Services;

/// <summary>Owns a verified external game process; native Maa remains the sole input owner.</summary>
public sealed class GameManagementService : IDisposable
{
    private const string SettingsKey = "GameManagement.Options";
    private static readonly ConditionalWeakTable<MaaProcessor, GameManagementService> Instances = new();
    private readonly MaaProcessor _processor;
    private readonly CancellationTokenSource _lifetime = new();
    private readonly SemaphoreSlim _operation = new(1, 1);
    private readonly GameRestartSchedule _schedule = new();
    private readonly object _sync = new();
    private TaskProcessIdentity? _identity;
    private ManagedGameTarget? _verifiedTarget;
    private bool _manualRestart;
    private int _managedRuns;
    private int _reconnecting;
    private bool _disposed;
    private string? _attentionRequired;
    private readonly long _clockOrigin = Stopwatch.GetTimestamp();
    private DateTimeOffset Clock => DateTimeOffset.UnixEpoch + Stopwatch.GetElapsedTime(_clockOrigin);
    public GameManagementOptions Options { get; private set; }
    public bool IsArmed { get { lock (_sync) return _identity != null; } }
    public static GameManagementService For(MaaProcessor processor) => Instances.GetValue(processor, p => new(p));
    public static bool IsReconnecting(MaaProcessor processor) => Instances.TryGetValue(processor, out var service)
        && Volatile.Read(ref service._reconnecting) != 0;
    public static bool HasArmedSession(MaaProcessor processor) => Instances.TryGetValue(processor, out var service) && service.IsArmed;
    public static IDisposable SuppressReconnection(MaaProcessor processor)
    {
        var service = For(processor);
        Interlocked.Increment(ref service._reconnecting);
        lock (ScriptStudioGuard.SyncRoot) ScriptStudioGuard.GameManagementOperations++;
        return new ReconnectionLease(service);
    }
    private sealed class ReconnectionLease(GameManagementService owner) : IDisposable
    {
        private bool _disposed;
        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            Interlocked.Decrement(ref owner._reconnecting);
            lock (ScriptStudioGuard.SyncRoot) ScriptStudioGuard.GameManagementOperations--;
        }
    }
    public static void Release(MaaProcessor processor)
    {
        if (Instances.TryGetValue(processor, out var service)) service.Dispose();
        Instances.Remove(processor);
    }

    private GameManagementService(MaaProcessor processor)
    {
        _processor = processor;
        try { Options = GameManagementOptions.Parse(processor.InstanceConfiguration.GetValue(SettingsKey, "{}")); }
        catch (Exception error) when (error is JsonException or InvalidDataException)
        {
            Options = new();
            Log("设置无效，自动托管暂时关闭；原设置未覆盖，请在游戏管理中重新保存。" + error.Message);
        }
        _ = MonitorAsync();
    }

    public void Save(GameManagementOptions options)
    {
        lock (ScriptStudioGuard.SyncRoot)
        {
            if (_processor.IsTaskRunActive || _operation.CurrentCount == 0)
                throw new InvalidOperationException("请先停止当前任务或等待盒子操作完成，再修改游戏管理设置。");
            options.Validate();
            _processor.InstanceConfiguration.SetValue(SettingsKey, JsonSerializer.Serialize(options));
            Options = options;
            lock (_sync) { _schedule.Reset(); _identity = null; _verifiedTarget = null; _manualRestart = false; _attentionRequired = null; }
        }
        Log("设置已保存到当前实例；开启选项后从任务或“启动并连接”开始托管。");
    }

    public string SelectedExePath()
    {
        if (_processor.ViewModel?.CurrentDevice is not DesktopWindowInfo window || window.Handle == IntPtr.Zero)
            throw new InvalidOperationException("请先在连接设置中明确选择造梦盒子窗口。");
        GetWindowThreadProcessId(window.Handle, out var pid);
        using var process = Process.GetProcessById(checked((int)pid));
        return process.MainModule?.FileName ?? throw new InvalidOperationException("无法读取所选窗口的程序路径。");
    }

    public void Arm()
    {
        if (_attentionRequired != null) throw new InvalidOperationException(_attentionRequired);
        var options = Options;
        options.Validate(requireTarget: true);
        VerifyBox(options.ExePath);
        var verified = TaskRecovery.CaptureManagedTarget(_processor, options.ExePath);
        var identity = verified.Process;
        lock (_sync)
        {
            var changed = _identity?.StartedUtcTicks != identity.StartedUtcTicks;
            _identity = identity;
            _verifiedTarget = verified;
            if (changed) _schedule.Observe(options, 0, identity.StartedUtcTicks, Clock);
        }
    }

    public async Task StartAsync()
    {
        _attentionRequired = null;
        Options.Validate(requireTarget: true); VerifyBox(Options.ExePath);
        if (_processor.ViewModel?.CurrentDevice is DesktopWindowInfo window && WindowPid(window.Handle) != 0)
        {
            Arm();
            await OperateAsync("connect", false, _lifetime.Token);
            return;
        }
        // An unselected running copy must not be silently adopted or duplicated.
        foreach (var process in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(Options.ExePath)))
        {
            using (process)
            {
                try
                {
                    if (string.Equals(process.MainModule?.FileName, Options.ExePath, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidOperationException("造梦盒子已运行，请先明确选择它的窗口再连接。");
                }
                catch (System.ComponentModel.Win32Exception) { }
            }
        }
        await OperateAsync("start", false, _lifetime.Token);
    }

    public async Task RequestRestartAsync()
    {
        if (_processor.IsTaskRunActive)
        {
            if (Volatile.Read(ref _managedRuns) == 0)
                throw new InvalidOperationException("当前任务未进入托管执行，请停止后从游戏管理配置重新开始。");
            lock (_sync) _manualRestart = true;
            Log("已安排重启，将在当前轮结束后执行，已完成次数保留。");
            return;
        }
        _attentionRequired = null;
        Arm();
        await OperateAsync("restart", false, _lifetime.Token);
    }

    public async Task CloseAsync()
    {
        await OperateAsync("close", false, _lifetime.Token);
        lock (_sync) { _identity = null; _verifiedTarget = null; _manualRestart = false; _schedule.Reset(); }
    }

    private async Task<MaaJobStatus> ExecuteAsync(string? entry, string? patch, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var tasker = await _processor.GetTaskerAsync(token) ?? throw new IOException("盒子连接失败。");
        var job = tasker.AppendTask(entry!, patch ?? "{}");
        using var cancellation = token.Register(() =>
        {
            try { tasker.Stop(); }
            catch (ObjectDisposedException) { }
        });
        return await Task.Run(() => job.Wait(), token);
    }

    private async Task OperateAsync(string operation, bool insideTask, CancellationToken token,
        Func<string?, string?, CancellationToken, Task<MaaJobStatus>>? execute = null)
    {
        await _operation.WaitAsync(token);
        var ownsGuard = false;
        try
        {
            lock (ScriptStudioGuard.SyncRoot)
            {
                ScriptStudioGuard.ThrowIfCapturing();
                if (!insideTask && (_processor.IsTaskRunActive || _processor.IsConnecting))
                    throw new InvalidOperationException("请先停止任务并等待连接完成，再关闭或启动盒子。");
                ScriptStudioGuard.GameManagementOperations++;
                ownsGuard = true;
            }
            Options.Validate(requireTarget: true, requireLogin: operation != "close"); VerifyBox(Options.ExePath);
            Log(operation == "close" ? "正在关闭已核验的造梦盒子。" : "正在重建盒子会话，停止旧输入并重新核验窗口。");
            var result = await TaskRecovery.ManageAsync(_processor, Options, operation, execute ?? ExecuteAsync, token);
            if (result.Status != TaskAttemptStatus.Ready)
                throw new IOException("恢复未完成：" + result.Reason + "；请处理登录或存档问题后重试。");
            lock (_sync) { _schedule.CompleteRestart(Clock); _manualRestart = false; }
            if (operation != "close") Arm();
            Log(operation == "close" ? "盒子已关闭，自动监测已停止。" : "盒子已就绪，账号、存档及地图检查完成。");
        }
        catch
        {
            lock (_sync) { _identity = null; _manualRestart = false; _schedule.Reset(); }
            throw;
        }
        finally
        {
            if (ownsGuard) lock (ScriptStudioGuard.SyncRoot) ScriptStudioGuard.GameManagementOperations--;
            _operation.Release();
        }
    }

    private async Task MonitorAsync()
    {
        try
        {
            using var timer = new PeriodicTimer(TimeSpan.FromSeconds(5));
            while (await timer.WaitForNextTickAsync(_lifetime.Token))
            {
                TaskProcessIdentity? identity;
                lock (_sync) identity = _identity;
                if (identity == null || _operation.CurrentCount == 0 || IsReconnecting(_processor) || !Options.Enabled) continue;
                try
                {
                    using var process = Process.GetProcessById(identity.Pid);
                    identity.RequireSame(ReadIdentity(process));
                    process.Refresh();
                    bool requested;
                    lock (_sync) requested = _schedule.Observe(Options, process.PrivateMemorySize64,
                        identity.StartedUtcTicks, Clock);
                    if (_schedule.MemoryRecoveryExhausted)
                    {
                        _attentionRequired = "十分钟内三次重启后内存仍超限，自动托管已暂停；检查盒子后重新保存设置或手动启动。";
                        throw new InvalidOperationException(_attentionRequired);
                    }
                    if (requested) Log("已达到内存或定时重启条件；任务运行时将在本轮结束后重启。");
                    bool pending;
                    lock (_sync) pending = _schedule.PendingReason != null || _manualRestart;
                    if (pending && !_processor.IsTaskRunActive)
                        await OperateAsync("restart", false, _lifetime.Token);
                }
                catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { break; }
                catch (Exception error)
                {
                    // A disappearing user window is not permission to relaunch
                    // it while idle. Active tasks use the bounded recovery path.
                    lock (_sync) { _identity = null; _schedule.Reset(); }
                    Log("监测暂停：" + error.Message);
                }
            }
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { }
    }

    public async Task<MaaJobStatus> RunCompletionOnlyAsync(string entry, string? overrides,
        Func<string?, string?, CancellationToken, Task<MaaJobStatus>> execute, CancellationToken token)
    {
        Arm();
        Interlocked.Increment(ref _managedRuns);
        var confirmed = false;
        try
        {
            Log("答题任务只在确认整局结算后维护；异常中断不会重启或重新开局。");
            var run = Guid.NewGuid().ToString();
            var path = Path.Combine(AppPaths.DataRoot, "debug", "task-runs", run + ".json");
            var pipeline = TaskRecoveryPolicy.MergePipelineOverrides(overrides);
            TaskScriptPlan.Parameters(pipeline, entry)["management_checkpoint"] = new JsonObject
                { ["run_id"] = run, ["instance_id"] = _processor.InstanceId, ["status_path"] = path };
            TaskProcessIdentity identity;
            lock (_sync) identity = _identity!;
            var window = _processor.Config.DesktopWindow.HWnd;
            bool Interrupted()
            {
                try
                {
                    using var process = Process.GetProcessById(identity.Pid);
                    identity.RequireSame(ReadIdentity(process));
                    if (WindowPid(window) != identity.Pid) return true;
                    if (!File.Exists(path) || new FileInfo(path).Length > 65536) return false;
                    var state = JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
                    return state != null && TaskRecoveryPolicy.CheckpointBelongsTo(state, run, _processor.InstanceId)
                        && state["status"]?.GetValue<string>() == "fatal";
                }
                catch (ArgumentException) { return true; }
                catch (InvalidOperationException) { return true; }
                catch (IOException) { return false; }
            }
            var observed = await TaskRecoveryPolicy.ObserveExecutionAsync(execute(entry, pipeline.ToJsonString(), token),
                Interrupted, () => _processor.MaaTasker?.Stop(), token);
            var status = observed.TargetLost ? MaaJobStatus.Failed : observed.Result;
            token.ThrowIfCancellationRequested();
            var checkpoint = File.Exists(path) && new FileInfo(path).Length <= 65536
                ? JsonNode.Parse(await File.ReadAllTextAsync(path, token)) as JsonObject : null;
            if (status != MaaJobStatus.Succeeded || checkpoint == null
                || !TaskRecoveryPolicy.CheckpointBelongsTo(checkpoint, run, _processor.InstanceId)
                || checkpoint["status"]?.GetValue<string>() != "success"
                || checkpoint["reason"]?.GetValue<string>() != "adventure_finished")
            {
                Log("未确认答题整局结算，已保留日志并停止；不会重启或消耗新的冒险次数。");
                return MaaJobStatus.Failed;
            }
            bool pending;
            lock (_sync) pending = _schedule.PendingReason != null || _manualRestart;
            if (pending) await OperateAsync("restart", true, token, execute);
            confirmed = true;
            return MaaJobStatus.Succeeded;
        }
        finally
        {
            Interlocked.Decrement(ref _managedRuns);
            if (!confirmed) lock (_sync) { _identity = null; _manualRestart = false; _schedule.Reset(); }
        }
    }

    public async Task<MaaJobStatus> RunAsync(JsonObject plan, string originalEntry, string? overrides,
        Func<string?, string?, CancellationToken, Task<MaaJobStatus>> execute, CancellationToken token)
    {
        var profile = plan["management_profile"]?.DeepClone() as JsonObject;
        if (profile == null)
        {
            // 任务没声明轮次边界不是错误，只是不能交给游戏管理托管：
            // 降级成普通执行，不要在用户点「开始任务」时直接失败。
            Log("该任务没有声明轮次边界，本次不启用游戏管理，任务照常执行。");
            return await execute(originalEntry, overrides, token);
        }
        try { Arm(); }
        catch (ArgumentException) when (Options.RestartOnFailure && _verifiedTarget != null)
        { Log("先前核验的盒子已退出，将从保存的程序身份恢复连接。"); }
        Interlocked.Increment(ref _managedRuns);
        var completedNormally = false;
        try
        {
            var prepared = TaskScriptPlan.PrepareManaged(plan, originalEntry, overrides, Options);
            var (entry, pipeline, preparedProfile, limit) = prepared;
            profile = preparedProfile;
            var run = Guid.NewGuid().ToString();
            for (long completed = 0; completed < limit; completed++)
            {
                token.ThrowIfCancellationRequested();
                if (_attentionRequired != null) throw new InvalidOperationException(_attentionRequired);
                bool pending;
                lock (_sync) pending = _schedule.PendingReason != null || _manualRestart;
                if (pending) await OperateAsync("restart", true, token, execute);
                WriteProgress(run, plan, completed, limit, "running");
                var status = await TaskRecovery.RunAsync(_processor, entry, pipeline.ToJsonString(), profile, execute, token, Options, _verifiedTarget);
                token.ThrowIfCancellationRequested();
                if (status != MaaJobStatus.Succeeded)
                {
                    WriteProgress(run, plan, completed, limit, "stopped");
                    return status;
                }
                WriteProgress(run, plan, completed + 1, limit, "confirmed");
                Log($"本任务已确认 {completed + 1}/{(limit == long.MaxValue ? "持续运行" : limit)} 轮。");
                try { Arm(); }
                catch (ArgumentException) when (completed + 1 == limit || Options.RestartOnFailure)
                { lock (_sync) _identity = null; }
            }
            completedNormally = true;
            return MaaJobStatus.Succeeded;
        }
        finally
        {
            Interlocked.Decrement(ref _managedRuns);
            if (!completedNormally) lock (_sync) { _identity = null; _manualRestart = false; _schedule.Reset(); }
        }
    }

    private void WriteProgress(string run, JsonObject plan, long completed, long limit, string status)
    {
        var directory = Path.Combine(AppPaths.DataRoot, "debug", "game-management"); Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, run + ".json");
        var state = new JsonObject { ["version"] = 1, ["instance_id"] = _processor.InstanceId,
            ["run_id"] = run, ["task_id"] = plan["id"]?.DeepClone(), ["completed"] = completed,
            ["total"] = limit == long.MaxValue ? -1 : limit, ["status"] = status,
            ["recording_sha256"] = plan["recording"]?["sha256"]?.DeepClone() };
        File.WriteAllText(path + ".tmp", state.ToJsonString()); File.Move(path + ".tmp", path, true);
    }

    private void Log(string message) => _processor.AddLog("游戏管理：" + message);
    private static TaskProcessIdentity ReadIdentity(Process process) => new(process.Id,
        process.StartTime.ToUniversalTime().Ticks, process.MainModule?.FileName ?? throw new IOException("无法核验进程路径。"));
    private static int WindowPid(nint handle) { GetWindowThreadProcessId(handle, out var pid); return unchecked((int)pid); }
    private static void VerifyBox(string path)
    {
        if (!File.Exists(path)) throw new FileNotFoundException("造梦盒子程序不存在。", path);
        var descriptor = Path.Combine(Path.GetDirectoryName(path)!, "META-INF", "AIR", "application.xml");
        if (!File.Exists(descriptor)) throw new InvalidDataException("缺少造梦盒子 AIR 描述文件。");
        var app = XDocument.Load(descriptor);
        if (!app.Descendants().Any(e => e.Name.LocalName == "id" && e.Value == "dusk.zmBox"))
            throw new InvalidDataException("所选程序不是已适配的造梦盒子 AIR 客户端。");
        // 版本号不设限：造梦盒子 1.9 / 1.10 及后续版本都能直接用，
        // 只校验它确实是造梦盒子（id = dusk.zmBox），不再锁死具体版本。
    }
    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true; _lifetime.Cancel();
        lock (_sync) _identity = null;
    }
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint window, out uint processId);
}
