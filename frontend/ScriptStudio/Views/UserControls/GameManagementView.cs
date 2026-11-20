using System;
using System.Globalization;
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
    private readonly CheckBox _memory = new() { Content = "内存超过" };
    private readonly TextBox _memoryLimit = new() { Text = "1.8", Watermark = "1.8", Width = 80, Name = "MemoryLimitGb" };
    private readonly CheckBox _interval = new() { Content = "按间隔自动重启" };
    private readonly TextBox _minutes = new() { Text = "60", Width = 80 };
    private readonly TextBox _exe = new() { Watermark = "现有造梦盒子 EXE 路径" };
    private readonly TextBox _account = new() { Watermark = "选填，留空使用盒子当前账号" };
    private readonly TextBox _role = new() { Watermark = "选填，留空仅核对存档位" };
    private readonly ComboBox _slot = new() { ItemsSource = new[] { "存档 1", "存档 2", "存档 3", "存档 4", "存档 5", "存档 6", "存档 7", "存档 8" }, SelectedIndex = 0 };
    private readonly TextBlock _feedback = Text("", 12);
    private bool _busy;
    private bool _loading;
    private TaskQueueViewModel? Page => DataContext as TaskQueueViewModel;

    public GameManagementView()
    {
        PlayerUi.Apply(this);
        var panel = new StackPanel { Spacing = 14, Margin = new Thickness(2, 4, 8, 8) };
        var actions = new WrapPanel();
        var start = Button("启动并连接盒子", async () =>
        {
            await Service().StartAsync();
            Feedback("");
        });
        start.Classes.Add("Flat");
        start.Background = start.BorderBrush = new SolidColorBrush(Color.Parse("#1765E6"));
        start.Foreground = Brushes.White;
        actions.Children.Add(start);
        actions.Children.Add(Button("重启盒子", async () =>
        {
            await Service().RequestRestartAsync(); Feedback("");
        }));
        actions.Children.Add(Button("关闭盒子", async () =>
        {
            await Service().CloseAsync(); Feedback("");
        }));
        panel.Children.Add(actions);
        _feedback.IsVisible = false;
        panel.Children.Add(_feedback);
        var policies = new StackPanel { Spacing = 8 };
        policies.Children.Add(Title("自动维护"));
        foreach (var check in new[] { _failure, _memory, _interval })
        {
            check.Content = Text(check.Content?.ToString() ?? "", 13);
            check.MinHeight = 32;
            check.IsCheckedChanged += async (_, _) => await SavePolicyChange();
        }
        policies.Children.Add(_failure);
        var memory = new WrapPanel { Orientation = Orientation.Horizontal };
        memory.Children.Add(_memory); memory.Children.Add(_memoryLimit); memory.Children.Add(Text("GB 自动重启", 13));
        memory.Children.Add(Button("应用阈值", () =>
        {
            if (!TryReadMemoryLimit(out var limit)) { Feedback(MemoryLimitHint()); return Task.CompletedTask; }
            Service().Save(Service().Options with { MemoryLimitGb = limit });
            ShowDraftHint(); return Task.CompletedTask;
        }));
        foreach (var child in memory.Children) child.Margin = new Thickness(0, 0, 8, 6);
        policies.Children.Add(memory);
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
        ToolTip.SetTip(_failure, "失败重试耗尽后才重启续跑，每轮最多 3 次。挂机与战败重试在任务设置中调整。");
        ToolTip.SetTip(memory, "内存重启仅在任务执行期间生效，等待本轮结束后重启，并保留已完成轮数。");
        panel.Children.Add(Section(policies));
        var login = new StackPanel { Spacing = 8 };
        login.Children.Add(Title("账号与续跑"));
        var binding = new StackPanel { Spacing = 8 };
        binding.Children.Add(Title("盒子绑定"));
        binding.Children.Add(Text("盒子程序", 12));
        binding.Children.Add(_exe);
        var paths = new WrapPanel();
        paths.Children.Add(Button("选择 EXE", ChooseExe));
        paths.Children.Add(Button("使用内置盒子", () => { _exe.Text = Service().BundledExePath; return Task.CompletedTask; }));
        paths.Children.Add(Button("使用已选窗口", () =>
        {
            if (Page != null) _exe.Text = GameManagementService.For(Page.Processor).SelectedExePath();
            return Task.CompletedTask;
        }));
        binding.Children.Add(paths);
        binding.Children.Add(Text("改绑后点击“保存设置”。启动、重启和关闭始终使用已保存的绑定。", 12));
        login.Children.Add(Text("历史账号名称", 12));
        login.Children.Add(_account);
        login.Children.Add(Text("角色名称与存档位置", 12));
        var roleRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,110"), ColumnSpacing = 8 };
        roleRow.Children.Add(_role); Grid.SetColumn(_slot, 1); _slot.HorizontalAlignment = HorizontalAlignment.Stretch;
        roleRow.Children.Add(_slot); login.Children.Add(roleRow);
        login.Children.Add(Button("保存设置", Save));
        panel.Children.Add(Section(login));
        var advanced = new StackPanel { Spacing = 14 };
        advanced.Children.Add(Section(binding));
        advanced.Children.Add(Button("保存设置", Save));
        panel.Children.Add(new Expander { Header = "高级设置", IsExpanded = false, Content = advanced,
            HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Stretch });
        foreach (var field in new[] { _exe, _account, _role, _minutes, _memoryLimit })
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
        _minutes.IsEnabled = options.RestartOnInterval;
        _memoryLimit.IsEnabled = options.RestartOnMemory;
    }

    private void Feedback(string message) { _feedback.Text = message; _feedback.IsVisible = !string.IsNullOrEmpty(message); }

    private void ShowDraftHint()
    {
        if (_loading || Page == null) return;
        var saved = Service().Options;
        var dirty = _exe.Text?.Trim() != saved.ExePath || _account.Text?.Trim() != saved.AccountName
            || _role.Text?.Trim() != saved.RoleName || _slot.SelectedIndex + 1 != saved.SaveSlot
            || _minutes.Text != saved.IntervalMinutes.ToString(CultureInfo.InvariantCulture)
            || !TryReadMemoryLimit(out var limit) || limit != saved.EffectiveMemoryLimitGb;
        Feedback(dirty ? "有未保存的设置。连接与存档请点击“保存设置”，内存阈值或重启间隔请点击对应的应用按钮。" : "");
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
        Feedback("操作提示：" + error.Message);
        Page?.Processor.AddLog($"error: 游戏管理操作失败。{error.Message} {TaskAttempt.ScreenshotHint}");
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
            _memoryLimit.Text = options.EffectiveMemoryLimitGb.ToString(CultureInfo.InvariantCulture);
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
        var saved = Service().Options;
        var validMemoryLimit = TryReadMemoryLimit(out var memoryLimit);
        var path = _exe.Text?.Trim() ?? "";
        Service().Save(saved with
        {
            RestartOnFailure = _failure.IsChecked == true, RestartOnMemory = _memory.IsChecked == true,
            RestartOnInterval = _interval.IsChecked == true, IntervalMinutes = minutes,
            MemoryLimitGb = validMemoryLimit ? memoryLimit : saved.EffectiveMemoryLimitGb,
            ExePath = path, UseBundledBox = string.IsNullOrWhiteSpace(path) || string.Equals(path, Service().BundledExePath, StringComparison.OrdinalIgnoreCase),
            AccountName = _account.Text?.Trim() ?? "",
            RoleName = _role.Text?.Trim() ?? "", SaveSlot = _slot.SelectedIndex + 1
        });
        ShowSavedPolicy(Service().Options);
        Feedback(validMemoryLimit ? "" : MemoryLimitHint());
        return Task.CompletedTask;
    }

    private bool TryReadMemoryLimit(out decimal value) =>
        decimal.TryParse(_memoryLimit.Text, NumberStyles.AllowDecimalPoint | NumberStyles.AllowLeadingSign | NumberStyles.AllowLeadingWhite | NumberStyles.AllowTrailingWhite,
            CultureInfo.InvariantCulture, out value) && GameManagementOptions.IsValidMemoryLimit(value);

    private string MemoryLimitHint() => $"内存阈值请输入大于 0 的 GB 数值；继续使用已保存的 {Service().Options.EffectiveMemoryLimitGb.ToString(CultureInfo.InvariantCulture)} GB。";

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
