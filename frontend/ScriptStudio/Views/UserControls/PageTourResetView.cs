using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Helper;
using MFAAvalonia.Services;

namespace MFAAvalonia.Views.UserControls;

public sealed class PageTourResetView : UserControl
{
    private readonly TextBlock _status = new() { TextWrapping = TextWrapping.Wrap, Opacity = .75 };

    public PageTourResetView()
    {
        var panel = new StackPanel { Spacing = 8 };
        panel.Children.Add(new TextBlock { Text = "页面教程", FontSize = 16, FontWeight = FontWeight.SemiBold });
        var reset = new Button { Content = "重置教程状态", HorizontalAlignment = HorizontalAlignment.Left, Padding = new Thickness(12, 7) };
        reset.Click += (_, _) =>
        {
            try { PageTourConfiguration.RequestReset(); Refresh(); }
            catch (Exception error) { _status.Text = "重置请求未保存：" + error.Message; LoggerHelper.Warning(_status.Text); }
        };
        panel.Children.Add(reset); panel.Children.Add(_status); Content = panel;
        AttachedToVisualTree += (_, _) => Refresh();
        DataContextChanged += (_, _) => Refresh();
    }

    private void Refresh() => _status.Text = PageTourConfiguration.IsResetPending
        ? "已安排重置，重启小助手后生效。"
        : "重启小助手后，各页面首次进入时会重新显示教程。";
}
