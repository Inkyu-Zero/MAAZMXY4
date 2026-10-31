using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using MaaFramework.Binding;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Services;

/// <summary>Recover a single script task round without replacing the MFA queue or its progress.</summary>
public static class TaskRecovery
{
    public static async Task<MaaJobStatus> RunAsync(MaaProcessor processor, string entry, string? overrides, JsonObject profile,
        Func<string?, string?, CancellationToken, Task<MaaJobStatus>> execute, CancellationToken token,
        GameManagementOptions? management = null, ManagedGameTarget? verifiedTarget = null)
    {
        try
        {
            var pipeline = TaskRecoveryPolicy.MergePipelineOverrides(overrides);
            var parameters = InitializerParameters(pipeline, entry);
            var options = management == null ? TaskRecoveryOptions.Read(parameters)
                : new TaskRecoveryOptions(management.RestartOnFailure, 3, management.SaveSlot, management.ExePath);
            var resumeEntry = profile["resume_entry"]?.GetValue<string>();
            if (options.Enabled && string.IsNullOrWhiteSpace(resumeEntry))
                throw new InvalidDataException("该任务没有配置恢复入口。");
            var slots = profile["resume_parameters"]?["resume_slots"] as JsonObject;
            if (options.Enabled && slots != null && !slots.ContainsKey(options.SaveSlot.ToString()))
                throw new InvalidDataException("该任务不支持所选存档。");
            TaskRecoveryPolicy.CompleteParameters(parameters, options, false);
            var session = new RecoverySession(processor, pipeline, options, execute, entry, resumeEntry, profile);
            if (options.Enabled)
            {
                if (verifiedTarget != null) session.UseVerifiedTarget(verifiedTarget);
                else session.CaptureTarget();
            }
            var outcome = await TaskRecoveryPolicy.RunRoundAsync(options,
                ct => session.RunEntryAsync(entry, false, ct), session.RecoverAsync, token);
            if (outcome.Status == TaskAttemptStatus.Cancelled)
                throw new OperationCanceledException(outcome.Reason, token);
            if (outcome.Status == TaskAttemptStatus.Success) return MaaJobStatus.Succeeded;
            processor.AddLog("任务已停止：" + outcome.Reason);
            return MaaJobStatus.Failed;
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception error)
        {
            processor.AddLog("任务恢复未执行：" + error.Message);
            LoggerHelper.Warning("任务原生恢复停止：" + error.Message);
            return MaaJobStatus.Failed;
        }
    }

    public static ManagedGameTarget CaptureManagedTarget(MaaProcessor processor, string exePath)
    {
        if (!OperatingSystem.IsWindows() || processor.ViewModel?.CurrentController != MaaControllerTypes.Win32
            || processor.ViewModel.CurrentDevice is not DesktopWindowInfo selected || selected.Handle == IntPtr.Zero)
            throw new ArgumentException("目标窗口已消失，请先启动并连接造梦盒子。");
        if (!File.Exists(exePath)) throw new FileNotFoundException("游戏EXE不存在。", exePath);
        var pid = WindowPid(selected.Handle);
        if (pid == 0) throw new ArgumentException("目标窗口已消失。");
        var identity = Identity(pid); identity.RequireConfiguredPath(exePath);
        RequireUnshared(processor, pid);
        var window = ReadWindow(selected.Handle);
        if (window.Pid != pid || !window.Identity.IsValid || !window.Visible || !window.HasUsableBounds)
            throw new ArgumentException("所选游戏窗口尚未就绪，请等待加载完成后再开始任务。");
        return new(identity, selected.Handle, window.ClientSize, window.Identity);
    }

    private static void RequireUnshared(MaaProcessor processor, int pid) =>
        TaskRecoveryPolicy.RequireExclusivePid(pid, MaaProcessor.Processors
            .Where(other => other != processor && (other.IsTaskRunActive || GameManagementService.HasArmedSession(other)))
            .Select(other => other.ViewModel?.CurrentDevice is DesktopWindowInfo device ? WindowPid(device.Handle) : 0));

    public static async Task<TaskAttempt> ManageAsync(MaaProcessor processor, GameManagementOptions options,
        string operation, Func<string?, string?, CancellationToken, Task<MaaJobStatus>> execute, CancellationToken token)
    {
        options.Validate(requireTarget: true, requireLogin: operation != "close");
        if (!OperatingSystem.IsWindows() || processor.ViewModel?.CurrentController != MaaControllerTypes.Win32)
            throw new InvalidOperationException("游戏管理需要先选择 Win32 造梦盒子控制器。");
        using var reconnection = GameManagementService.SuppressReconnection(processor);
        var profile = new JsonObject
        {
            ["resume_parameters"] = new JsonObject
            {
                ["account_name"] = options.AccountName, ["role_name"] = options.RoleName,
                ["save_slot"] = options.SaveSlot, ["allow_existing_map"] = operation == "connect"
            }
        };
        var session = new RecoverySession(processor, new JsonObject(),
            new TaskRecoveryOptions(true, 3, options.SaveSlot, options.ExePath), execute,
            "GameManagement_Resume", "GameManagement_Resume", profile);
        if (operation == "start")
        {
            await session.StopInputsAndAgentAsync(token);
            await session.StartSelectedProcessAsync(token);
            await session.BindUniqueWindowAsync(token);
            await session.ReconnectAsync(token);
            return await session.ResumeAsync(options.SaveSlot, token);
        }
        session.CaptureTarget();
        if (operation == "connect")
        {
            await session.ReconnectAsync(token);
            return await session.ResumeAsync(options.SaveSlot, token);
        }
        if (operation == "close")
        {
            await session.StopInputsAndAgentAsync(token);
            await session.StopSelectedProcessAsync(token);
            return new(TaskAttemptStatus.Ready);
        }
        if (operation != "restart") throw new InvalidOperationException("未知的盒子管理操作。");
        return await TaskRecoveryPolicy.RestartAsync(session, options.SaveSlot, token);
    }

    private static JsonObject InitializerParameters(JsonObject pipeline, string nodeName)
    {
        if (pipeline[nodeName] is not JsonObject node) pipeline[nodeName] = node = new();
        if (node["action"] is not JsonObject action) node["action"] = action = new();
        if (action["param"] is not JsonObject param) action["param"] = param = new();
        if (param["custom_action_param"] is not JsonObject values) param["custom_action_param"] = values = new();
        return values;
    }

    private sealed class RecoverySession(MaaProcessor processor, JsonObject pipeline,
        TaskRecoveryOptions options, Func<string?, string?, CancellationToken, Task<MaaJobStatus>> execute, string entryName, string? resumeEntry, JsonObject profile) : ITaskRestartLifecycle
    {
        private TaskProcessIdentity? _target;
        private IntPtr _window;
        private readonly TaskRecordingRevision _revision = new();
        private TaskWindowSize _clientSize = new(1147, 720);
        private TaskWindowIdentity? _windowIdentity;

        public void CaptureTarget()
        {
            UseVerifiedTarget(CaptureManagedTarget(processor, options.ExePath));
        }

        public void UseVerifiedTarget(ManagedGameTarget snapshot)
        {
            snapshot.Process.RequireConfiguredPath(options.ExePath);
            snapshot.ClientSize.Validate();
            if (!snapshot.WindowIdentity.IsValid) throw new InvalidOperationException("缺少原游戏窗口的标题与窗口类。");
            var selected = processor.ViewModel?.CurrentDevice as DesktopWindowInfo;
            var selectedPid = selected == null ? 0 : WindowPid(selected.Handle);
            if (selectedPid != 0 && selectedPid != snapshot.Process.Pid)
                throw new InvalidOperationException("所选窗口已改变，未恢复之前的游戏进程。");
            try { snapshot.Process.RequireSame(Identity(snapshot.Process.Pid)); }
            catch (ArgumentException) { } // The verified process may already have exited.
            EnsureUnshared(snapshot.Process.Pid);
            _target = snapshot.Process; _window = snapshot.Window; _clientSize = snapshot.ClientSize;
            _windowIdentity = snapshot.WindowIdentity;
        }

        public async Task<TaskAttempt> RunEntryAsync(string entry, bool resume, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            if (!resume && options.Enabled && IsLostTarget())
                return new(TaskAttemptStatus.Recoverable, "window_lost");
            var runId = Guid.NewGuid().ToString();
            var directory = Path.Combine(AppPaths.DataRoot, "debug", "task-runs");
            Directory.CreateDirectory(directory);
            var statusPath = Path.Combine(directory, runId + ".json");
            var copy = (JsonObject)pipeline.DeepClone();
            var parameters = InitializerParameters(copy, entry);
            // Maa replaces custom_action_param as a complete value. Explicitly
            // carry the full phase configuration rather than relying on the
            // resource node's parameters being merged by the native runtime.
            if (resume && profile["resume_parameters"] is JsonObject defaults)
            {
                var combined = TaskRecoveryPolicy.MergePipelineOverrides(new JsonArray(defaults.DeepClone(), parameters.DeepClone()).ToJsonString());
                foreach (var item in combined) parameters[item.Key] = item.Value?.DeepClone();
            }
            TaskRecoveryPolicy.CompleteParameters(parameters, options, resume);
            parameters["run_id"] = runId;
            parameters["instance_id"] = processor.InstanceId;
            parameters["status_path"] = statusPath;
            _revision.Apply(parameters);
            MaaJobStatus status;
            try
            {
                var execution = execute(entry, copy.ToJsonString(), token);
                if (options.Enabled)
                {
                    var observed = await TaskRecoveryPolicy.ObserveExecutionAsync(execution, IsLostTarget,
                        () => { processor.AddLog("游戏管理：目标窗口已丢失，停止旧任务后重新建立连接。"); processor.MaaTasker?.Stop(); }, token);
                    token.ThrowIfCancellationRequested();
                    if (observed.TargetLost) return new(TaskAttemptStatus.Recoverable, "window_lost");
                    status = observed.Result;
                }
                else status = await execution;
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception error)
            {
                token.ThrowIfCancellationRequested();
                return IsLostTarget() ? new(TaskAttemptStatus.Recoverable, "window_lost") : TaskAttempt.Fatal(error.Message);
            }
            token.ThrowIfCancellationRequested();
            if (!File.Exists(statusPath))
                return IsLostTarget() ? new(TaskAttemptStatus.Recoverable, "window_lost") : TaskAttempt.Fatal("缺少本轮确认状态。");
            if (new FileInfo(statusPath).Length > 65536) return TaskAttempt.Fatal("本轮状态文件无效。");
            var checkpoint = JsonNode.Parse(await File.ReadAllTextAsync(statusPath, token)) as JsonObject;
            if (checkpoint == null) return TaskAttempt.Fatal("本轮状态格式无效。");
            var result = TaskRecoveryPolicy.ReadCheckpoint(checkpoint, runId, processor.InstanceId, resume);
            if (TaskRecoveryPolicy.CheckpointBelongsTo(checkpoint, runId, processor.InstanceId))
                _revision.Observe(checkpoint);
            if ((result.Status is TaskAttemptStatus.Success or TaskAttemptStatus.Ready) && status != MaaJobStatus.Succeeded)
                return TaskAttempt.Fatal("原生任务未成功结束，未计入完成次数。");
            if (result.Status == TaskAttemptStatus.Recoverable && result.Reason == "window_lost" && !IsLostTarget())
                return TaskAttempt.Fatal("目标窗口仍然有效，未确认窗口丢失。");
            if (result.Status == TaskAttemptStatus.Fatal
                && TaskRecoveryPolicy.IsCaptureLossCandidate(checkpoint, runId, processor.InstanceId)
                && IsLostTarget())
                return new(TaskAttemptStatus.Recoverable, "window_lost");
            return result;
        }

        public async Task<TaskAttempt> RecoverAsync(int slot, int attempt, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            if (_target == null) return TaskAttempt.Fatal("没有经过校验的恢复目标。");
            processor.AddLog($"任务：本轮自动恢复 {attempt}/{options.MaxRetries}，已完成轮数保持不变。");
            using var reconnection = GameManagementService.SuppressReconnection(processor);
            return await TaskRecoveryPolicy.RestartAsync(this, slot, token);
        }

        public async Task StopInputsAndAgentAsync(CancellationToken token)
        {
            await Task.Run(() => processor.SetTasker(requireAllStopped: true), token);
            token.ThrowIfCancellationRequested();
            await DispatcherHelper.RunOnMainThreadAsync(() => processor.ViewModel?.InvalidateCurrentWin32Window());
        }

        public Task StopSelectedProcessAsync(CancellationToken token) =>
            StopSelectedProcessAsync(_target ?? throw new InvalidOperationException("没有经过校验的恢复目标。"), token);

        public async Task StartSelectedProcessAsync(CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            using var started = Process.Start(new ProcessStartInfo(options.ExePath)
            {
                WorkingDirectory = Path.GetDirectoryName(options.ExePath)!, UseShellExecute = false,
                CreateNoWindow = false, WindowStyle = ProcessWindowStyle.Normal
            }) ?? throw new InvalidOperationException("游戏进程未启动。");
            var until = DateTime.UtcNow + TimeSpan.FromSeconds(5);
            while (DateTime.UtcNow < until)
            {
                token.ThrowIfCancellationRequested();
                if (started.HasExited) throw new InvalidOperationException("游戏进程启动后立即结束。");
                try
                {
                    _target = Identity(started.Id);
                    _target.RequireConfiguredPath(options.ExePath);
                    return;
                }
                catch (System.ComponentModel.Win32Exception)
                {
                    // Windows may not expose MainModule until loader initialization completes.
                    await Task.Delay(100, token);
                }
            }
            throw new InvalidOperationException("无法核验新启动的游戏进程身份。");
        }

        public async Task BindUniqueWindowAsync(CancellationToken token)
        {
            var pid = _target?.Pid ?? throw new InvalidOperationException("没有重启的目标进程。");
            var clock = Stopwatch.StartNew();
            processor.AddLog("游戏管理：等待原游戏窗口就绪，临时启动窗口不会连接或接收输入。");
            while (clock.Elapsed < TimeSpan.FromSeconds(60))
            {
                var window = await FindNewWindowAsync(pid, clock, token);
                if (!await RestoreClientSizeAsync(window, pid, token)) continue;
                token.ThrowIfCancellationRequested();
                _target!.RequireSame(Identity(pid));
                EnsureUnshared(pid);
                var current = ReadWindow(window.Handle);
                if (!MatchesWindow(current, window, pid) || !current.HasUsableBounds
                    || !_clientSize.SameNormalizedFrame(current.ClientSize)) continue;
                _window = window.Handle;
                _windowIdentity = current.Identity;
                await DispatcherHelper.RunOnMainThreadAsync(() =>
                {
                    token.ThrowIfCancellationRequested();
                    processor.ViewModel!.BindWin32RecoveryWindow(window);
                });
                processor.AddLog($"游戏管理：已绑定稳定游戏窗口 PID={pid}，HWND=0x{window.Handle.ToInt64():X}，标题={window.Name}。");
                return;
            }
            throw new TimeoutException("游戏窗口在启动期间持续变化，未连接临时窗口。");
        }

        private static bool MatchesWindow(TaskWindowCandidate current, DesktopWindowInfo window, int pid) =>
            current.Pid == pid && current.Visible && current.Identity.Matches(new(window.Name, window.ClassName));

        private async Task<bool> RestoreClientSizeAsync(DesktopWindowInfo window, int pid, CancellationToken token)
        {
            var clock = Stopwatch.StartNew();
            var nextCorrection = TimeSpan.Zero;
            var readiness = new TaskWindowReadiness(pid, new(window.Name, window.ClassName));
            TaskWindowCandidate? last = null;
            while (clock.Elapsed < TimeSpan.FromSeconds(5))
            {
                token.ThrowIfCancellationRequested();
                _target!.RequireSame(Identity(pid));
                var current = ReadWindow(window.Handle);
                if (!MatchesWindow(current, window, pid)) return false; // Re-enumerate a replacement HWND.
                if (current != last)
                {
                    LoggerHelper.Info($"游戏管理尺寸采样：原客户区={_clientSize}，窗口={current}。");
                    last = current;
                }
                var stable = readiness.Observe(new[] { current }, clock.Elapsed);
                if (current.HasUsableBounds && _clientSize.SameNormalizedFrame(current.ClientSize))
                {
                    if (stable != null) return true;
                }
                else if (stable != null && clock.Elapsed >= nextCorrection)
                {
                    token.ThrowIfCancellationRequested();
                    // Read and resize synchronously in the same physical-pixel
                    // context. Never resize an AIR placeholder or a changed HWND.
                    TaskWindowCoordinates.InPhysicalPixels(SetThreadDpiAwarenessContext, () =>
                    {
                        var measured = ReadWindowPhysical(window.Handle);
                        if (!MatchesWindow(measured, window, pid) || !measured.HasUsableBounds) return false;
                        var target = _clientSize.OuterSizeFor(measured.ClientSize, measured.OuterSize);
                        if (!SetWindowPos(window.Handle, IntPtr.Zero, 0, 0, target.Width, target.Height, 0x0002 | 0x0004 | 0x0010))
                            throw new InvalidOperationException("无法调整新游戏窗口尺寸。");
                        return true;
                    });
                    nextCorrection = clock.Elapsed + TimeSpan.FromSeconds(1);
                }
                await Task.Delay(100, token);
            }
            throw new InvalidOperationException($"游戏窗口尺寸未稳定或比例不匹配（原客户区 {_clientSize}，最后采样 {last}），未连接或发送游戏输入。");
        }

        public async Task ReconnectAsync(CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            RequireBoundWindow();
            var tasker = await processor.GetTaskerAsync(token);
            token.ThrowIfCancellationRequested();
            if (tasker == null)
                throw new InvalidOperationException("游戏窗口重新连接失败。");
            try { RequireBoundWindow(); }
            catch
            {
                await StopInputsAndAgentAsync(token);
                throw;
            }
        }

        private void RequireBoundWindow()
        {
            _target!.RequireSame(Identity(_target.Pid));
            var selected = processor.ViewModel?.CurrentDevice as DesktopWindowInfo;
            var current = ReadWindow(_window);
            if (selected?.Handle != _window || processor.Config.DesktopWindow.HWnd != _window
                || current.Pid != _target.Pid || !current.Visible || !current.HasUsableBounds
                || _windowIdentity?.Matches(current.Identity) != true)
                throw new InvalidOperationException("连接目标已变化，未采用其它窗口或继续登录。");
        }

        public Task<TaskAttempt> ResumeAsync(int slot, CancellationToken token) => RunEntryAsync(resumeEntry!, true, token);

        private async Task StopSelectedProcessAsync(TaskProcessIdentity expected, CancellationToken token)
        {
            Process? selected;
            try { selected = Process.GetProcessById(expected.Pid); }
            catch (ArgumentException) { return; }
            using (selected)
            {
                lock (ScriptStudioGuard.SyncRoot)
                {
                    token.ThrowIfCancellationRequested();
                    if (selected.HasExited) return;
                    expected.RequireSame(Identity(expected.Pid));
                    EnsureUnshared(expected.Pid);
                    selected.Kill(); // Never kill a process tree or all processes sharing a name.
                }
                await selected.WaitForExitAsync(token).WaitAsync(TimeSpan.FromSeconds(15), token);
            }
        }

        private async Task<DesktopWindowInfo> FindNewWindowAsync(int pid, Stopwatch clock, CancellationToken token)
        {
            var name = processor.ViewModel?.GetCurrentControllerName();
            var profile = MaaProcessor.Interface?.Controller?.FirstOrDefault(item => item.Name == name && item.ControllerType == MaaControllerTypes.Win32)?.Win32
                ?? throw new InvalidOperationException("原控制器窗口筛选规则不存在。");
            var readiness = new TaskWindowReadiness(pid, _windowIdentity);
            var lastObservation = "";
            while (clock.Elapsed < TimeSpan.FromSeconds(60))
            {
                token.ThrowIfCancellationRequested();
                _target!.RequireSame(Identity(pid));
                var windows = MaaProcessor.Toolkit.Desktop.Window.Find().Where(window => WindowPid(window.Handle) == pid
                    && Matches(window.Name, profile.WindowRegex) && Matches(window.ClassName, profile.ClassRegex)).ToArray();
                var samples = windows.Select(window => ReadWindow(window.Handle)).ToArray();
                var observation = string.Join("; ", samples.Select(sample => sample.ToString()));
                if (observation != lastObservation)
                {
                    LoggerHelper.Info($"游戏管理等待窗口：PID={pid}，期望={_windowIdentity?.ToString() ?? "造梦盒子主窗口"}，候选=[{observation}]。");
                    lastObservation = observation;
                }
                var candidate = readiness.Observe(samples, clock.Elapsed);
                if (candidate != null)
                {
                    var selected = windows.Single(window => window.Handle == candidate.Handle);
                    if (MatchesWindow(candidate, selected, pid)) return selected;
                }
                await Task.Delay(250, token);
            }
            throw new TimeoutException("等待重启后的原游戏窗口超时；临时窗口和其它窗口均未连接。");
        }

        private void EnsureUnshared(int pid)
        {
            RequireUnshared(processor, pid);
        }

        private bool IsLostTarget()
        {
            if (_target == null) return false;
            try { _target.RequireSame(Identity(_target.Pid)); return WindowPid(_window) != _target.Pid; }
            catch (ArgumentException) { return true; }
            catch (InvalidOperationException) { return false; }
        }
    }

    private static bool Matches(string? value, string? pattern) => string.IsNullOrEmpty(pattern)
        || Regex.IsMatch(value ?? "", pattern, RegexOptions.None, TimeSpan.FromSeconds(1));
    private static TaskProcessIdentity Identity(int pid)
    {
        using var process = Process.GetProcessById(pid);
        if (process.HasExited) throw new ArgumentException("游戏进程已结束。");
        return new(pid, process.StartTime.ToUniversalTime().Ticks,
            process.MainModule?.FileName ?? throw new InvalidOperationException("无法核验游戏程序路径。"));
    }
    private static int WindowPid(IntPtr window)
    {
        if (window == IntPtr.Zero) return 0;
        GetWindowThreadProcessId(window, out var pid);
        return unchecked((int)pid);
    }
    private static TaskWindowCandidate ReadWindow(IntPtr window) =>
        TaskWindowCoordinates.InPhysicalPixels(SetThreadDpiAwarenessContext, () => ReadWindowPhysical(window));

    private static TaskWindowCandidate ReadWindowPhysical(IntPtr window)
    {
        var title = new StringBuilder(1024);
        var className = new StringBuilder(256);
        GetWindowText(window, title, title.Capacity);
        GetClassName(window, className, className.Capacity);
        var firstOk = GetClientRect(window, out var first);
        var outerOk = GetWindowRect(window, out var outer);
        var secondOk = GetClientRect(window, out var second);
        return new(window, WindowPid(window), new(title.ToString(), className.ToString()),
            new(second.Right - second.Left, second.Bottom - second.Top),
            new(outer.Right - outer.Left, outer.Bottom - outer.Top), IsWindowVisible(window) && !IsIconic(window),
            firstOk && outerOk && secondOk && first.Right == second.Right && first.Bottom == second.Bottom
                && first.Left == second.Left && first.Top == second.Top);
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct WindowRectangle { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetClientRect(IntPtr window, out WindowRectangle rectangle);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetWindowRect(IntPtr window, out WindowRectangle rectangle);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr window, StringBuilder text, int maximum);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr window, StringBuilder text, int maximum);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr window);
}
