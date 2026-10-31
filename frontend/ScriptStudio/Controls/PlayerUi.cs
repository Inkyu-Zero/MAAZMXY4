using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;

namespace MFAAvalonia.Controls;

/// <summary>Shared readable type and spacing for the player-facing workspaces.</summary>
public static class PlayerUi
{
    public static readonly FontFamily Font = FontFamily.Parse("Microsoft YaHei UI, Segoe UI");
    public const double BodySize = 14, CaptionSize = 12, SectionSize = 16, TitleSize = 24;
    public static void Apply(UserControl view) { view.FontFamily = Font; view.FontSize = BodySize; }
    public static Border Card(Control content) => new()
    {
        Child = content, Padding = new Thickness(16), CornerRadius = new CornerRadius(10),
        BorderThickness = new Thickness(1), BorderBrush = new SolidColorBrush(Color.FromArgb(45, 128, 128, 128))
    };
    public static TextBlock Text(string text, double size = BodySize, bool secondary = false) => new()
    {
        Text = text, FontSize = size, TextWrapping = TextWrapping.Wrap, Opacity = secondary ? .75 : 1
    };
}
