using System;
using System.Globalization;
using System.Collections.Generic;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using MFAAvalonia.Services;
using MFAAvalonia.Controls;
using MFAAvalonia.ViewModels.Pages;

namespace MFAAvalonia.Views.UserControls;

/// <summary>Show the saved restart policy separately from per-task AFK and retry options.</summary>
public sealed class GameManagementView : UserControl
{
    private readonly CheckBox _failure = new() { Content = "任务无法继续时，重启后续跑" };
    private readonly CheckBox _memory = new() { Content = "内存超过 1.8 GB 自动重启" };
    private readonly CheckBox _interval = new() { Content = "按间隔自动重启" };
    private readonly TextBox _minutes = new() { Text = "60", Width = 80 };
    private readonly TextBox _exe = new() { Watermark = "现有造梦盒子 EXE 路径" };
    private readonly TextBox _account = new() { Watermark = "盒子历史账号中的名称" };
    private readonly TextBox _role = new() { Watermark = "用于核对存档的角色名" };
    private readonly ComboBox _slot = new() { ItemsSource = new[] { "存档 1", "存档 2", "存档 3", "存档 4", "存档 5", "存档 6", "存档 7", "存档 8" }, SelectedIndex = 0 };
    private readonly TextBlock _savedState = Text("当前生效：未启用自动重启", 12);
    private readonly TextBlock _feedback = Text("重启开关即时保存；程序与存档修改后请保存设置。", 12);
    private bool _busy;
    private bool _loading;
    private TaskQueueViewModel? Page => DataContext as TaskQueueViewModel;

    public GameManagementView()
    {
        PlayerUi.Apply(this);
        var panel = new StackPanel { Spacing = 14, Margin = new Thickness(2, 4, 8, 8) };
        panel.Children.Add(Text("先保存连接与存档，再选择重启条件。挂机与战败重试在任务设置中调整。", 13));
        var policies = new StackPanel { Spacing = 8 };
        policies.Children.Add(Title("2. 重启与续跑"));
        policies.Children.Add(_savedState);
        foreach (var check in new[] { _failure, _memory, _interval })
        {
            check.Content = Text(check.Content?.ToString() ?? "", 13);
            check.MinHeight = 32;
            check.IsCheckedChanged += async (_, _) => await SavePolicyChange();
        }
        policies.Children.Add(_failure);
        policies.Children.Add(Text("先在游戏内完成战败重试。重试耗尽或返回流程无法恢复时，才重启盒子并继续未完成的轮次。", 12));
        policies.Children.Add(_memory);
        policies.Children.Add(_interval);
        var interval = new WrapPanel { Orientation = Orientation.Horizontal };
        interval.Children.Add(Text("重启间隔", 12)); interval.Children.Add(_minutes); interval.Children.Add(Text("分钟", 12));
        interval.Children.Add(Button("应用间隔", () =>
        {
            Service().Save(Service().Options with { IntervalMinutes = ReadMinutes() });
            ShowSavedPolicy(Service().Options); ShowDraftHint(); return Task.CompletedTask;
        }));
        foreach (var child in interval.Children) child.Margin = new Thickness(0, 0, 8, 6);
        policies.Children.Add(interval);
        var login = new StackPanel { Spacing = 8 };
        login.Children.Add(Title("1. 连接与存档"));
        login.Children.Add(Text("盒子程序", 12));
        login.Children.Add(_exe);
        var paths = new WrapPanel();
        paths.Children.Add(Button("选择 EXE", ChooseExe));
        paths.Children.Add(Button("使用已选窗口", () =>
        {
            if (Page != null) _exe.Text = GameManagementService.For(Page.Processor).SelectedExePath();
            return Task.CompletedTask;
        }));
        login.Children.Add(paths);
        login.Children.Add(Text("历史账号名称", 12));
        login.Children.Add(_account);
        login.Children.Add(Text("角色名称与存档位置", 12));
        var roleRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,110"), ColumnSpacing = 8 };
        roleRow.Children.Add(_role); Grid.SetColumn(_slot, 1); _slot.HorizontalAlignment = HorizontalAlignment.Stretch;
        roleRow.Children.Add(_slot); login.Children.Add(roleRow);
        login.Children.Add(Button("保存设置", Save));
        panel.Children.Add(Section(login));
        policies.Children.Add(Text("内存与定时重启会等待本轮结束。已完成轮数保留；同一轮连续恢复 3 次仍失败时停止并提示。", 12));
        panel.Children.Add(Section(policies));
        panel.Children.Add(_feedback);
        panel.Children.Add(Title("手动操作"));
        var actions = new WrapPanel();
        actions.Children.Add(Button("启动并连接", async () => { await Save(); await Service().StartAsync(); }));
        actions.Children.Add(Button("重启", async () => { if (Page?.Processor.IsTaskRunActive != true) await Save(); await Service().RequestRestartAsync(); }));
        actions.Children.Add(Button("关闭盒子", async () => { await Save(); await Service().CloseAsync(); }));
        panel.Children.Add(actions);
        foreach (var field in new[] { _exe, _account, _role, _minutes })
            field.TextChanged += (_, _) => ShowDraftHint();
        _slot.SelectionChanged += (_, _) => ShowDraftHint();
        Content = new ScrollViewer { Content = panel, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
        DataContextChanged += (_, _) => Load();
        AttachedToVisualTree += (_, _) => Load();
    }

    private static TextBlock Text(string value, double size) => new()
    {
        Text = value, FontSize = size, TextWrapping = TextWrapping.Wrap,
        VerticalAlignment = VerticalAlignment.Center, Opacity = size < 13 ? .78 : 1
    };
    private static TextBlock Title(string value) => new() { Text = value, FontSize = PlayerUi.SectionSize, FontWeight = FontWeight.SemiBold };
    private static Border Section(Control content) => new()
    {
        Child = content, Padding = new Thickness(12), CornerRadius = new CornerRadius(8),
        BorderThickness = new Thickness(1), BorderBrush = new SolidColorBrush(Color.FromArgb(45, 128, 128, 128))
    };

    private void ShowSavedPolicy(GameManagementOptions options)
    {
        var enabled = new List<string>();
        if (options.RestartOnFailure) enabled.Add("重试耗尽 / 任务失败");
        if (options.RestartOnMemory) enabled.Add("内存超限");
        if (options.RestartOnInterval) enabled.Add($"每 {options.IntervalMinutes} 分钟");
        _savedState.Text = "当前生效：" + (enabled.Count == 0 ? "未启用自动重启" : string.Join("、", enabled) + "时重启");
        _minutes.IsEnabled = options.RestartOnInterval;
    }

    private void ShowDraftHint()
    {
        if (_loading || Page == null) return;
        var saved = Service().Options;
        var dirty = _exe.Text?.Trim() != saved.ExePath || _account.Text?.Trim() != saved.AccountName
            || _role.Text?.Trim() != saved.RoleName || _slot.SelectedIndex + 1 != saved.SaveSlot
            || _minutes.Text != saved.IntervalMinutes.ToString(CultureInfo.InvariantCulture);
        _feedback.Text = dirty ? "有未保存的设置。连接与存档请点击“保存设置”，重启间隔请点击“应用间隔”。"
            : "设置已保存。重启开关即时生效，游戏内战败重试由任务设置控制。";
    }

    private async Task SavePolicyChange()
    {
        if (_loading || Page == null) return;
        try
        {
            if (_busy) throw new InvalidOperationException("请等待当前盒子操作完成后再修改重启设置。");
            var saved = Service().Options;
            var minutes = saved.IntervalMinutes;
            if (_interval.IsChecked == true && !saved.RestartOnInterval) minutes = ReadMinutes();
            Service().Save(saved with
            {
                RestartOnFailure = _failure.IsChecked == true,
                RestartOnMemory = _memory.IsChecked == true,
                RestartOnInterval = _interval.IsChecked == true,
                IntervalMinutes = minutes
            });
            ShowSavedPolicy(Service().Options);
            ShowDraftHint();
            await Task.CompletedTask;
        }
        catch (Exception error)
        {
            // Rejected saves must not leave an unchecked switch above a still
            // enabled persisted policy. Preserve edits to the login fields.
            _loading = true;
            try
            {
                var saved = Service().Options;
                _failure.IsChecked = saved.RestartOnFailure; _memory.IsChecked = saved.RestartOnMemory;
                _interval.IsChecked = saved.RestartOnInterval; ShowSavedPolicy(saved);
            }
            finally { _loading = false; }
            ShowError(error);
        }
    }

    private void ShowError(Exception error)
    {
        _feedback.Text = "未保存：" + error.Message;
        Page?.Processor.AddLog("游戏管理：" + error.Message);
    }

    private GameManagementService Service() => Page == null
        ? throw new InvalidOperationException("当前实例尚未就绪。") : GameManagementService.For(Page.Processor);

    private Button Button(string title, Func<Task> action)
    {
        var button = new Button { Content = title, Margin = new Thickness(0, 0, 8, 6), Padding = new Thickness(12, 7), MinHeight = 34, FontSize = 13 };
        button.Click += async (_, _) =>
        {
            if (_busy) return;
            _busy = true;
            try { await action(); }
            catch (Exception error) { ShowError(error); }
            finally { _busy = false; }
        };
        return button;
    }

    private void Load()
    {
        if (Page == null) return;
        _loading = true;
        try
        {
            var options = Service().Options;
            _failure.IsChecked = options.RestartOnFailure; _memory.IsChecked = options.RestartOnMemory;
            _interval.IsChecked = options.RestartOnInterval; _minutes.Text = options.IntervalMinutes.ToString(CultureInfo.InvariantCulture);
            _exe.Text = options.ExePath; _account.Text = options.AccountName; _role.Text = options.RoleName;
            _slot.SelectedIndex = options.SaveSlot - 1;
            ShowSavedPolicy(options);
        }
        catch (Exception error) { Page.Processor.AddLog("游戏管理设置未载入：" + error.Message); }
        finally { _loading = false; }
    }

    private Task Save()
    {
        var minutes = _interval.IsChecked == true ? ReadMinutes() : Service().Options.IntervalMinutes;
        Service().Save(new GameManagementOptions
        {
            RestartOnFailure = _failure.IsChecked == true, RestartOnMemory = _memory.IsChecked == true,
            RestartOnInterval = _interval.IsChecked == true, IntervalMinutes = minutes,
            ExePath = _exe.Text?.Trim() ?? "", AccountName = _account.Text?.Trim() ?? "",
            RoleName = _role.Text?.Trim() ?? "", SaveSlot = _slot.SelectedIndex + 1
        });
        ShowSavedPolicy(Service().Options);
        _feedback.Text = "设置已保存；重启开关与当前实例同步。";
        return Task.CompletedTask;
    }

    private int ReadMinutes()
    {
        if (!int.TryParse(_minutes.Text, NumberStyles.None, CultureInfo.InvariantCulture, out var minutes)
            || minutes is < 1 or > 10080)
            throw new InvalidOperationException("重启间隔请输入 1～10080 分钟。");
        return minutes;
    }

    private async Task ChooseExe()
    {
        var provider = TopLevel.GetTopLevel(this)?.StorageProvider;
        if (provider == null) return;
        var files = await provider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = "选择现有造梦盒子程序", AllowMultiple = false,
            FileTypeFilter = new[] { new FilePickerFileType("Windows 程序") { Patterns = new[] { "*.exe" } } }
        });
        if (files.Count == 1) _exe.Text = files[0].TryGetLocalPath();
    }
}
