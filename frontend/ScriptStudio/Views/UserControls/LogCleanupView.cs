using System;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Services;

namespace MFAAvalonia.Views.UserControls;

public sealed class LogCleanupView : UserControl
{
    private readonly LogCleanup _cleanup;
    private readonly ToggleSwitch _retention = new() { Name = "LogRetentionEnabled", Content = "启用日志滚动刷新" };
    private readonly TextBlock _retentionStatus = new() { Name = "LogRetentionStatus", TextWrapping = TextWrapping.Wrap, Opacity = .8 };
    private readonly Button _preview = new() { Name = "LogCleanupPreview", Content = "清空日志…" };
    private readonly Button _confirm = new() { Name = "LogCleanupConfirm", Content = "确认清空", IsVisible = false };
    private readonly Button _cancel = new() { Name = "LogCleanupCancel", Content = "取消", IsVisible = false };
    private readonly TextBlock _status = new() { Name = "LogCleanupStatus", TextWrapping = TextWrapping.Wrap, Opacity = .8 };
    private LogCleanup.Plan? _plan;
    private CancellationTokenSource? _operation;
    private bool _attached;
    private bool _updatingRetention, _savingRetention;
    private int _generation;

    public LogCleanupView() : this(new LogCleanup(AppPaths.DataRoot)) { }
    public LogCleanupView(LogCleanup cleanup)
    {
        _cleanup = cleanup;
        var panel = new StackPanel { Spacing = 10 };
        panel.Children.Add(new TextBlock { Text = "日志清理", FontSize = 16, FontWeight = FontWeight.SemiBold });
        _retention.Classes.Add("Switch");
        panel.Children.Add(_retention);
        panel.Children.Add(new TextBlock
        {
            Text = "默认启用，所有实例共用此设置并记住选择。启用时以 64 MB 为滚动目标，自动回收最旧的已关闭日志块；当前未封块或占用中的文件可能使总量超出目标。关闭后仍正常写入日志，也可手动清空。",
            TextWrapping = TextWrapping.Wrap
        });
        panel.Children.Add(_retentionStatus);
        panel.Children.Add(new TextBlock
        {
            Text = "清理历史日志以释放磁盘空间，请先停止任务和录制。正在使用或变化的日志会保留；不处理截图、任务状态、配置、录制和备份。",
            TextWrapping = TextWrapping.Wrap
        });
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var button in new[] { _preview, _confirm, _cancel })
        { button.Padding = new Thickness(12, 7); button.Margin = new Thickness(0, 0, 8, 4); buttons.Children.Add(button); }
        panel.Children.Add(buttons); panel.Children.Add(_status); Content = panel;
        _status.Text = LogCleanup.IsSupported ? "点击后先查看数量和大小，确认后才删除。" : "日志清理目前支持 Windows 客户端。";
        _preview.IsEnabled = LogCleanup.IsSupported;
        _preview.Click += async (_, _) => await PreviewAsync();
        _confirm.Click += async (_, _) => await ConfirmAsync();
        _cancel.Click += (_, _) => CancelPreview();
        _retention.IsCheckedChanged += async (_, _) =>
        {
            if (!_updatingRetention && !_savingRetention && _attached)
                await SetRetentionEnabledAsync(_retention.IsChecked == true);
        };
        AttachedToVisualTree += (_, _) =>
        {
            _attached = true;
            LogRetention.SettingChanged += OnRetentionChanged;
            _retentionStatus.Text = _savingRetention ? "正在保存…" : "";
            RefreshRetention();
        };
        DetachedFromVisualTree += (_, _) =>
        {
            LogRetention.SettingChanged -= OnRetentionChanged;
            _attached = false; _generation++; _operation?.Cancel(); ResetPreview();
            _status.Text = "已离开日志清理。再次操作时请重新查看。";
        };
    }

    private void OnRetentionChanged() => Dispatcher.UIThread.Post(() => { if (_attached) RefreshRetention(); });
    private void RefreshRetention()
    {
        _updatingRetention = true;
        try { _retention.IsChecked = LogRetention.Enabled; }
        finally { _updatingRetention = false; }
        _retention.IsEnabled = LogCleanup.IsSupported && !_savingRetention;
    }
    public async Task SetRetentionEnabledAsync(bool enabled)
    {
        if (_savingRetention || !_attached || !LogCleanup.IsSupported) return;
        _savingRetention = true; _retention.IsEnabled = false;
        _retentionStatus.Text = "正在保存…";
        try
        {
            await LogRetention.SetEnabledAsync(enabled);
            if (_attached) _retentionStatus.Text = LogRetention.Enabled ? "已启用自动滚动回收。" : "已关闭自动滚动回收。";
        }
        catch (Exception error)
        {
            LoggerHelper.Warning("日志滚动设置未完成：" + error.Message);
            if (_attached) _retentionStatus.Text = "设置未完成：" + error.Message;
        }
        finally
        {
            _savingRetention = false;
            if (_attached) RefreshRetention();
        }
    }

    public Task PreviewAsync() => RunAsync(async (token, generation) =>
    {
        ResetPreview(); _status.Text = "正在统计日志…";
        var plan = await Task.Run(() => _cleanup.Inspect(token), token);
        if (!IsCurrent(generation)) return;
        _plan = plan;
        var unavailable = plan.Unavailable == 0 ? "" : $"另有 {plan.Unavailable} 项无法读取或属于链接，已跳过。";
        _status.Text = plan.Files == 0 ? "没有找到可清理的日志文件。" + unavailable
            : $"发现 {plan.Files} 个日志，约 {FormatSize(plan.Bytes)}。确认后永久删除；正在使用或确认前变化的文件会保留。" + unavailable;
        _confirm.IsVisible = _cancel.IsVisible = plan.Files > 0;
    });

    public Task ConfirmAsync() => RunAsync(async (token, generation) =>
    {
        var plan = _plan;
        if (plan == null) { _status.Text = "请先查看要清理的日志。"; return; }
        using var ownership = BeginCleanup();
        ResetPreview(); _status.Text = "正在清理日志…";
        var result = await Task.Run(() => _cleanup.Clear(plan, token));
        var message = $"已清理 {result.Deleted} 个日志，释放约 {FormatSize(result.Bytes)}。";
        if (result.Skipped > 0) message += $"跳过 {result.Skipped} 个文件（正在使用、已变化、已移除或无权限）。";
        if (plan.Unavailable > 0) message += $"扫描时另有 {plan.Unavailable} 项未处理。";
        if (result.Cancelled) message += "其余清理已取消。";
        LoggerHelper.Info("日志清理：" + message);
        if (IsCurrent(generation)) _status.Text = message;
    });

    public void CancelPreview()
    { if (_operation == null) { ResetPreview(); _status.Text = "已取消，未删除日志。"; } }
    private void ResetPreview() { _plan = null; _confirm.IsVisible = _cancel.IsVisible = false; }
    private bool IsCurrent(int generation) => _attached && generation == _generation;

    private const string CleanupReason = "正在清理日志，请稍候再开始任务或录制。";
    private static IDisposable BeginCleanup()
    {
        lock (ScriptStudioGuard.SyncRoot)
        {
            ScriptStudioGuard.ThrowIfCapturing();
            if (MaaProcessor.Processors.Any(processor => processor.IsTaskRunActive))
                throw new InvalidOperationException("请先停止所有任务和录制，再清空日志。");
            // Some Python loggers reopen on each line. Reuse the existing start
            // gate rather than letting a new task race the file cleanup.
            ScriptStudioGuard.UpdateBlockReason = CleanupReason;
            return new CleanupOwnership();
        }
    }
    private sealed class CleanupOwnership : IDisposable
    {
        private bool _disposed;
        public void Dispose()
        {
            lock (ScriptStudioGuard.SyncRoot)
            {
                if (_disposed) return;
                _disposed = true;
                if (ScriptStudioGuard.UpdateBlockReason == CleanupReason) ScriptStudioGuard.UpdateBlockReason = "";
            }
        }
    }

    private async Task RunAsync(Func<CancellationToken, int, Task> action)
    {
        if (_operation != null || !_attached || !LogCleanup.IsSupported) return;
        using var operation = new CancellationTokenSource();
        _operation = operation; var generation = _generation;
        _preview.IsEnabled = _confirm.IsEnabled = _cancel.IsEnabled = false;
        try { await action(operation.Token, generation); }
        catch (OperationCanceledException) { if (IsCurrent(generation)) _status.Text = "操作已取消。"; }
        catch (Exception error)
        {
            LoggerHelper.Warning("日志清理未完成：" + error.Message);
            if (IsCurrent(generation)) { ResetPreview(); _status.Text = "未完成：" + error.Message; }
        }
        finally
        {
            if (ReferenceEquals(_operation, operation))
            { _operation = null; _preview.IsEnabled = _confirm.IsEnabled = _cancel.IsEnabled = LogCleanup.IsSupported; }
        }
    }

    public static string FormatSize(long bytes)
    {
        string[] units = ["B", "KiB", "MiB", "GiB", "TiB"];
        var unit = 0; var size = (double)bytes;
        while (size >= 1024 && unit < units.Length - 1) { size /= 1024; unit++; }
        return unit == 0 ? $"{bytes} B" : $"{size:F2} {units[unit]}";
    }
}
