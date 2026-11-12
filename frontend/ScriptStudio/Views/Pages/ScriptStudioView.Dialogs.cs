using System;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;

namespace MFAAvalonia.Views.Pages;

public sealed partial class ScriptStudioView
{
    private async Task SaveAsync() => await SaveDocumentAsync(false);
    private async Task SaveAsAsync() => await SaveDocumentAsync(true);

    private async Task<bool> SaveDocumentAsync(bool saveAs)
    {
        if (_vm.Document == null) return false;
        await ApplyPendingEditsAsync();
        string? filename = null;
        if (_vm.File == null || saveAs)
        {
            var suggestion = _vm.Name + (saveAs ? "-副本" : "");
            var name = await AskNameAsync(saveAs ? "另存为" : "保存脚本", suggestion);
            if (name == null) return false;
            filename = name + ".json";
            await _vm.ChangeAsync("rename", new JsonObject { ["name"] = name });
        }
        await _vm.ChangeAsync("save", filename == null ? null : new JsonObject { ["file"] = filename });
        await RefreshLibraryAsync(); _status.Text = "已保存“" + _vm.Name + "”。任务下轮会读取保存后的版本。";
        return true;
    }

    private async Task<string?> AskNameAsync(string title, string suggestion, string? currentFile = null)
    {
        if (TopLevel.GetTopLevel(this) is not Window owner) return null;
        var dialog = new Window { Title = title, Width = 430, CanResize = false, SizeToContent = SizeToContent.Height,
            WindowStartupLocation = WindowStartupLocation.CenterOwner, ShowInTaskbar = false };
        var content = new StackPanel { Spacing = 14, Margin = new Thickness(22) };
        content.Children.Add(new TextBlock { Text = title, FontSize = 20, FontWeight = FontWeight.SemiBold });
        var name = new TextBox { Text = suggestion, Watermark = "脚本名称" };
        content.Children.Add(Field("脚本名称", name));
        var error = new TextBlock { Foreground = Brushes.IndianRed, TextWrapping = TextWrapping.Wrap, FontSize = 12 };
        content.Children.Add(error);
        var save = new Button { Content = "保存", IsDefault = true, Padding = new Thickness(18, 8) };
        var cancel = new Button { Content = "取消", IsCancel = true, Padding = new Thickness(18, 8) };
        void Submit()
        {
            var value = name.Text?.Trim() ?? "";
            if (value.Length == 0 || value.StartsWith('.') || value.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
            { error.Text = "请输入有效名称，不能包含 / \\ : * ? 等文件名符号。"; return; }
            if (value.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) value = value[..^5].Trim();
            if (_files.Any(file => file?["file"]?.GetValue<string>() != currentFile &&
                string.Equals(file?["file"]?.GetValue<string>(), value + ".json", StringComparison.OrdinalIgnoreCase)))
            { error.Text = "同名脚本已存在，请换一个名称。"; return; }
            dialog.Close(value);
        }
        save.Click += (_, _) => Submit(); cancel.Click += (_, _) => dialog.Close((string?)null);
        content.Children.Add(new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right,
            Spacing = 8, Children = { cancel, save } });
        dialog.Content = content;
        dialog.Opened += (_, _) => { name.Focus(); name.SelectAll(); };
        return await ShowStudioDialogAsync<string?>(dialog, owner);
    }

    private async Task<bool> ConfirmDeleteAsync(string message)
    {
        if (TopLevel.GetTopLevel(this) is not Window owner) return false;
        var dialog = new Window { Title = "删除脚本", Width = 440, CanResize = false, SizeToContent = SizeToContent.Height,
            WindowStartupLocation = WindowStartupLocation.CenterOwner, ShowInTaskbar = false };
        var content = new StackPanel { Spacing = 18, Margin = new Thickness(22) };
        content.Children.Add(new TextBlock { Text = message, TextWrapping = TextWrapping.Wrap });
        var remove = new Button { Content = "删除", Padding = new Thickness(18, 8) };
        var cancel = new Button { Content = "取消", IsCancel = true, Padding = new Thickness(18, 8) };
        remove.Click += (_, _) => dialog.Close(true); cancel.Click += (_, _) => dialog.Close(false);
        content.Children.Add(new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right,
            Spacing = 8, Children = { cancel, remove } });
        dialog.Content = content; return await ShowStudioDialogAsync<bool>(dialog, owner);
    }

    private async Task<T> ShowStudioDialogAsync<T>(Window dialog, Window owner)
    {
        // Use the same dynamic palette as MFAA's Suki dialogs, including theme changes while open.
        dialog.Bind(Window.BackgroundProperty, new DynamicResourceExtension("SukiCardBackground"));
        dialog.Bind(Window.ForegroundProperty, new DynamicResourceExtension("SukiText"));
        try { return await dialog.ShowDialog<T>(owner); }
        finally { if (IsEffectivelyVisible) Focus(); }
    }
}
