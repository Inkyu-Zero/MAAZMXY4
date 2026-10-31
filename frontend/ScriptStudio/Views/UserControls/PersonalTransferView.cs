using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Platform.Storage;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using MFAAvalonia.Services;

namespace MFAAvalonia.Views.UserControls;

public sealed class PersonalTransferView : UserControl
{
    private readonly PersonalTransfer _transfer;
    private readonly Func<IDisposable> _begin;
    private readonly StackPanel _actions = new() { Spacing = 10 };
    private readonly TextBlock _status = new() { TextWrapping = Avalonia.Media.TextWrapping.Wrap };
    private readonly Button _confirm = new() { Content = "确认导入，下次启动生效", IsVisible = false };
    private readonly Button _cancel = new() { Content = "取消待导入备份", IsVisible = false };
    private string? _archive, _hash;
    private bool _busy;
    public PersonalTransferView() : this(new PersonalTransfer(AppPaths.DataRoot, AppPaths.ConfigDirectory), BeginOperation) { }
    public PersonalTransferView(PersonalTransfer transfer, Func<IDisposable> begin)
    {
        _transfer = transfer; _begin = begin;
        var panel = new StackPanel { Spacing = 12 };
        panel.Children.Add(new TextBlock { Text = "个人配置与脚本迁移", FontSize = 18, FontWeight = Avalonia.Media.FontWeight.SemiBold });
        panel.Children.Add(new TextBlock { Text = "将已保存的设置、任务次数与勾选、录制脚本、绑定和布局导出为一个 ZIP 备份。升级后，在新助手中导入即可迁移。导出前请先保存脚本并停止任务和录制。", TextWrapping = Avalonia.Media.TextWrapping.Wrap });
        _actions.Children.Add(new TextBlock { Text = "ZIP 压缩与解压已内置，无需安装或选择任何压缩工具。备份包含个人设置与路径，请妥善保存。", TextWrapping = Avalonia.Media.TextWrapping.Wrap, Opacity = .75 });
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
        buttons.Children.Add(Button("导出 ZIP…", PickExportAsync)); buttons.Children.Add(Button("导入 ZIP…", PickImportAsync));
        _actions.Children.Add(buttons); _actions.Children.Add(_confirm); _actions.Children.Add(_cancel);
        _confirm.Click += async (_, _) => await RunAsync(ConfirmImportAsync);
        _cancel.Click += async (_, _) => await RunAsync(() => { _transfer.CancelPending(); ResetPreview(); Refresh(); return Task.CompletedTask; });
        panel.Children.Add(_actions); panel.Children.Add(_status); Content = panel; Refresh();
    }
    private Button Button(string text, Func<Task> action)
    { var button = new Button { Content = text, Margin = new Thickness(0, 0, 8, 4) }; button.Click += async (_, _) => await RunAsync(action); return button; }
    private async Task RunAsync(Func<Task> action)
    {
        if (_busy) return;
        try { using var operation = _begin(); _busy = true; _actions.IsEnabled = false; await action(); }
        catch (Exception error) { _status.Text = "未完成：" + error.Message; }
        finally { _busy = false; _actions.IsEnabled = true; }
    }
    private void ResetPreview() { _archive = _hash = null; _confirm.IsVisible = false; }
    private void Refresh()
    {
        _cancel.IsVisible = _transfer.HasPending;
        _status.Text = _transfer.HasPending ? "备份已准备好。请正常关闭助手，再重新打开以完成导入；也可以取消。尚未覆盖当前配置。" : _transfer.LastResult;
    }
    private static readonly FilePickerFileType ZipFiles = new("个人备份 (*.zip)") { Patterns = new[] { "*.zip" } };
    private IStorageProvider Storage => TopLevel.GetTopLevel(this)?.StorageProvider ?? throw new InvalidOperationException("文件选择器尚未就绪。");
    private async Task PickExportAsync()
    {
        var file = await Storage.SaveFilePickerAsync(new FilePickerSaveOptions { Title = "导出个人配置与脚本", SuggestedFileName = "造梦小助手个人备份-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".zip", DefaultExtension = "zip", FileTypeChoices = new[] { ZipFiles }, ShowOverwritePrompt = true });
        if (file?.TryGetLocalPath() is string path) await ExportToAsync(path);
    }
    public async Task ExportToAsync(string path)
    {
        _status.Text = "正在打包个人配置与脚本…";
        await Task.Run(() => _transfer.ExportAsync(path)); _status.Text = "备份已导出：" + path;
    }
    private async Task PickImportAsync()
    {
        var files = await Storage.OpenFilePickerAsync(new FilePickerOpenOptions { Title = "选择助手导出的 ZIP 个人备份", AllowMultiple = false, FileTypeFilter = new[] { ZipFiles } });
        if (files.FirstOrDefault()?.TryGetLocalPath() is string path) await LoadPreviewAsync(path);
    }
    public async Task LoadPreviewAsync(string path)
    {
        ResetPreview(); if (_transfer.HasPending) throw new InvalidOperationException("请先完成或取消现有待导入备份。");
        _status.Text = "正在校验备份…";
        var hash = await Task.Run(() => PersonalTransfer.ArchiveHash(path));
        var preview = await Task.Run(() => _transfer.Inspect(path));
        if (hash != await Task.Run(() => PersonalTransfer.ArchiveHash(path))) throw new IOException("备份正在变化，请重新选择。");
        _archive = path; _hash = hash; _confirm.IsVisible = true;
        var created = DateTimeOffset.TryParse(preview.Created, out var date) ? date.ToLocalTime().ToString("yyyy-MM-dd HH:mm") : "未知";
        var version = string.IsNullOrWhiteSpace(preview.ResourceVersion) ? "未记录" : preview.ResourceVersion;
        var size = preview.Bytes < 1024 * 1024 ? $"{Math.Ceiling(preview.Bytes / 1024d):F0} KB" : $"{preview.Bytes / 1024d / 1024d:F2} MB";
        _status.Text = $"备份时间：{created}\n来源资源版本：{version}\n共 {preview.Files} 个文件，{size}；将覆盖 {preview.Overwrites} 个同名文件。其它文件保留。\n确认后将在下次启动时导入，并先备份原配置。仅导入你信任的个人备份；迁移到新电脑后请核对游戏路径与窗口。";
    }
    public async Task ConfirmImportAsync()
    {
        if (_archive == null || _hash == null) throw new InvalidOperationException("请先选择并校验备份。");
        var archive = _archive; var hash = _hash;
        await Task.Run(() => _transfer.Stage(archive, hash)); ResetPreview(); Refresh();
    }
    private static IDisposable BeginOperation()
    {
        lock (ScriptStudioGuard.SyncRoot)
        {
            ScriptStudioGuard.ThrowIfCapturing();
            if (MaaProcessor.Processors.Any(p => p.IsTaskRunActive)) throw new InvalidOperationException("请先停止所有任务，再导出或导入个人数据。");
            ScriptStudioGuard.UpdateBlockReason = "正在迁移个人数据，请稍候再开始任务或录制。";
            return new Operation();
        }
    }
    private sealed class Operation : IDisposable
    { public void Dispose() { lock (ScriptStudioGuard.SyncRoot) ScriptStudioGuard.UpdateBlockReason = ""; } }
}
