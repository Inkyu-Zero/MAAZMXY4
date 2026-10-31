using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MFAAvalonia.Services;
using MFAAvalonia.Controls;
using MFAAvalonia.ViewModels.Pages;

namespace MFAAvalonia.Views.Pages;

/// <summary>Offline PC-game catalogue, using the host's native navigation/theme.</summary>
public sealed class GameDatabaseView : UserControl
{
    private const int PageSize = 60;
    private readonly GameDatabaseViewModel _vm;
    private readonly TextBox _search = new() { Watermark = "搜索名称、获得地点或技能（空格组合关键词）" };
    private readonly ComboBox _category = new() { ItemsSource = new[] { "全部", "装备", "道具", "Boss" }, SelectedIndex = 0, MinWidth = 100 };
    private readonly ComboBox _subtype = new() { ItemsSource = new[] { "全部" }, SelectedIndex = 0, MinWidth = 130 };
    private readonly ListBox _results = new();
    private readonly StackPanel _details = new() { Spacing = 12, Margin = new Thickness(18, 8) };
    private readonly TextBlock _status = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _page = new();
    private readonly Button _previous = new() { Content = "上一页" }, _next = new() { Content = "下一页" }, _retry = new() { Content = "重新加载", IsVisible = false };
    private readonly Grid _filters = new() { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto"), ColumnSpacing = 8, RowSpacing = 8 };
    private readonly Grid _body = new() { ColumnDefinitions = new ColumnDefinitions("280,8,*") };
    private readonly GridSplitter _splitter = new() { ResizeDirection = GridResizeDirection.Columns, HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly ScrollViewer _detailScroll;
    private readonly DispatcherTimer _debounce = new() { Interval = TimeSpan.FromMilliseconds(160) };
    private GameDatabaseCatalog? _catalog;
    private GameDatabaseEntry[] _matches = [];
    private Bitmap? _bitmap;
    private int _pageIndex;
    private bool _loading, _filtering, _filterPending;
    private bool? _narrow;

    public GameDatabaseView(GameDatabaseViewModel viewModel)
    {
        _vm = viewModel; DataContext = viewModel; PlayerUi.Apply(this);
        _detailScroll = new ScrollViewer { Content = _details, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
        _results.ItemTemplate = new FuncDataTemplate<GameDatabaseEntry>((entry, _) => new StackPanel { Spacing = 3, Margin = new Thickness(6), Children =
        {
            new TextBlock { Text = entry?.Name, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap },
            new TextBlock { Text = entry == null ? "" : $"{entry.Category} · {entry.Subtype}", Opacity = .65, FontSize = 12 },
        }});
        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,*,Auto"), Margin = new Thickness(20), RowSpacing = 14 };
        var heading = new StackPanel { Spacing = 6 };
        heading.Children.Add(new TextBlock { Text = "游戏百科", FontSize = PlayerUi.TitleSize, FontWeight = FontWeight.Bold });
        heading.Children.Add(new TextBlock { Text = "查装备、找材料、了解 Boss 技能。", Opacity = .7, TextWrapping = TextWrapping.Wrap });
        root.Children.Add(heading);
        var filters = _filters;
        filters.Children.Add(_search); Grid.SetColumn(_category, 1); filters.Children.Add(_category); Grid.SetColumn(_subtype, 2); filters.Children.Add(_subtype);
        Grid.SetRow(filters, 1); root.Children.Add(filters);
        _body.Children.Add(_results); Grid.SetColumn(_splitter, 1); _body.Children.Add(_splitter); Grid.SetColumn(_detailScroll, 2); _body.Children.Add(_detailScroll);
        Grid.SetRow(_body, 2); root.Children.Add(_body);
        var footer = new StackPanel { Spacing = 7 };
        footer.Children.Add(new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10, Children = { _previous, _page, _next, _retry } });
        footer.Children.Add(_status); Grid.SetRow(footer, 3); root.Children.Add(footer);
        Content = root;
        _debounce.Tick += (_, _) => { _debounce.Stop(); Filter(); };
        _search.TextChanged += (_, _) => { _filterPending = true; _debounce.Stop(); _debounce.Start(); };
        _category.SelectionChanged += (_, _) => { if (!_filtering) { UpdateSubtypes(); Filter(); } };
        _subtype.SelectionChanged += (_, _) => { if (!_filtering) Filter(); };
        _results.SelectionChanged += (_, _) => ShowDetails(_results.SelectedItem as GameDatabaseEntry);
        _previous.Click += (_, _) => { _pageIndex--; RenderPage(); };
        _next.Click += (_, _) => { _pageIndex++; RenderPage(); };
        _retry.Click += async (_, _) => await LoadAsync();
        AttachedToVisualTree += async (_, _) =>
        {
            if (_catalog == null) await LoadAsync();
            else if (_filterPending) Filter();
            else ShowDetails(_results.SelectedItem as GameDatabaseEntry);
        };
        DetachedFromVisualTree += (_, _) => { _debounce.Stop(); ReleaseBitmap(); };
        SizeChanged += (_, _) => ResizePage();
        ShowDetails(null);
    }
    private async Task LoadAsync()
    {
        if (_loading) return;
        _loading = true; _retry.IsVisible = false; _status.Text = "正在载入离线数据库…";
        try { _catalog = await _vm.LoadAsync(); UpdateSubtypes(); Filter(); }
        catch (Exception error) { _status.Text = "数据库载入失败：" + error.Message; _retry.IsVisible = true; }
        finally { _loading = false; }
    }
    private void UpdateSubtypes()
    {
        _filtering = true;
        var category = _category.SelectedItem as string ?? "全部";
        _subtype.ItemsSource = new[] { "全部" }.Concat((_catalog?.Entries ?? []).Where(e => category == "全部" || e.Category == category)
            .Select(e => e.Subtype).Distinct().OrderBy(s => s, StringComparer.Ordinal)).ToArray();
        _subtype.SelectedIndex = 0; _filtering = false;
    }
    private void Filter()
    {
        if (_catalog == null) return;
        _filterPending = false;
        _matches = _catalog.Search(_search.Text, _category.SelectedItem as string ?? "全部", _subtype.SelectedItem as string ?? "全部");
        _pageIndex = 0; RenderPage();
        _status.Text = $"找到 {_matches.Length:N0} 项 · 全部 {_catalog.Entries.Count:N0} 项";
    }
    private void RenderPage()
    {
        var count = Math.Max(1, (_matches.Length + PageSize - 1) / PageSize);
        _pageIndex = Math.Clamp(_pageIndex, 0, count - 1);
        _results.ItemsSource = _matches.Skip(_pageIndex * PageSize).Take(PageSize).ToArray();
        _results.SelectedIndex = _matches.Length == 0 ? -1 : 0;
        _previous.IsEnabled = _pageIndex > 0; _next.IsEnabled = _pageIndex + 1 < count;
        _page.Text = $"第 {_pageIndex + 1} / {count} 页";
        if (_matches.Length == 0) ShowDetails(null);
    }
    private void ShowDetails(GameDatabaseEntry? entry)
    {
        ReleaseBitmap(); _details.Children.Clear(); _detailScroll.Offset = default;
        if (entry == null) { AddText(_catalog == null ? "载入后选择条目查看详细资料。" : "没有匹配条目，请减少关键词或调整分类。"); return; }
        var header = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 14 };
        if (_catalog != null && entry.Icon != null)
        {
            try
            {
                var path = Path.Combine(_catalog.Root, entry.Icon.Replace('/', Path.DirectorySeparatorChar));
                using var stream = File.OpenRead(path);
                _bitmap = Bitmap.DecodeToWidth(stream, 80);
                header.Children.Add(new Image { Source = _bitmap, Width = 64, Height = 64, Stretch = Stretch.Uniform });
            }
            catch (Exception error) when (error is IOException or ArgumentException or NotSupportedException) { /* Text remains usable when an optional icon is missing. */ }
        }
        header.Children.Add(new TextBlock { Text = entry.Name, FontSize = 23, FontWeight = FontWeight.Bold, VerticalAlignment = VerticalAlignment.Center, TextWrapping = TextWrapping.Wrap });
        _details.Children.Add(header);
        AddText($"{entry.Category} · {entry.Subtype}", true);
        foreach (var fact in GameDatabasePresentation.Facts(entry))
        {
            var content = new StackPanel { Spacing = 8 };
            content.Children.Add(new TextBlock { Text = fact.Label, FontWeight = FontWeight.SemiBold, FontSize = 15 });
            content.Children.Add(new SelectableTextBlock { Text = fact.Value, TextWrapping = TextWrapping.Wrap });
            _details.Children.Add(PlayerUi.Card(content));
        }
        if (entry.Skills.Length > 0)
        {
            AddTitle("技能");
            if (entry.Skills.Length == 0) AddText("官方条目暂未提供可整理的技能，请查阅下方来源。", true);
            foreach (var skill in entry.Skills)
            {
                var content = new StackPanel { Spacing = 6 };
                content.Children.Add(new TextBlock { Text = skill.Name, FontWeight = FontWeight.SemiBold, FontSize = 15 });
                content.Children.Add(new SelectableTextBlock { Text = skill.Description, TextWrapping = TextWrapping.Wrap });
                _details.Children.Add(PlayerUi.Card(content));
            }
        }
        if (entry.ConfigVariants.Length > 0)
        {
            var variants = new StackPanel { Spacing = 10 };
            variants.Children.Add(PlayerUi.Text("不同等级与场景的属性可能不同，以游戏内显示为准。", 12, true));
            for (var index = 0; index < entry.ConfigVariants.Length; index++)
            {
                var variant = entry.ConfigVariants[index];
                var stats = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 24, RowSpacing = 8 };
                foreach (var stat in variant.Stats)
                {
                    var row = stats.RowDefinitions.Count; stats.RowDefinitions.Add(new RowDefinition(GridLength.Auto));
                    var label = PlayerUi.Text(stat.Label, 13, true); Grid.SetRow(label, row); stats.Children.Add(label);
                    var value = new SelectableTextBlock { Text = GameDatabasePresentation.Stat(stat), FontWeight = FontWeight.SemiBold };
                    Grid.SetRow(value, row); Grid.SetColumn(value, 1); stats.Children.Add(value);
                }
                variants.Children.Add(new Expander { Header = GameDatabasePresentation.VariantTitle(variant), Content = stats,
                    IsExpanded = entry.ConfigVariants.Length == 1, HorizontalAlignment = HorizontalAlignment.Stretch });
            }
            _details.Children.Add(new Expander { Header = "更多属性参考", Content = variants, IsExpanded = false, HorizontalAlignment = HorizontalAlignment.Stretch });
        }
        var references = new StackPanel { Spacing = 8 };
        references.Children.Add(PlayerUi.Text("资料可能随游戏更新变化，获取方式与数值以游戏内为准。", 12, true));
        foreach (var reference in entry.References.Where(reference => reference.Kind != "client").DistinctBy(reference => reference.Url))
        {
            var button = new Button { Content = reference.Kind == "wiki" ? "查看社区攻略" : "查看官方图鉴", HorizontalAlignment = HorizontalAlignment.Left };
            button.Click += async (_, _) =>
            {
                if (!GameDatabaseCatalog.IsReferenceUrl(reference.Url)) return;
                try { if (TopLevel.GetTopLevel(this) is { } top) await top.Launcher.LaunchUriAsync(new Uri(reference.Url)); }
                catch (Exception error) { _status.Text = "无法打开来源：" + error.Message; }
            };
            references.Children.Add(button);
        }
        foreach (var fact in entry.WikiFacts.Where(fact => !string.IsNullOrWhiteSpace(fact.Value)))
            references.Children.Add(new Expander { Header = "社区补充资料", IsExpanded = false,
                Content = new SelectableTextBlock { Text = fact.Value, TextWrapping = TextWrapping.Wrap }, HorizontalAlignment = HorizontalAlignment.Stretch });
        _details.Children.Add(new Expander { Header = "资料来源与补充", Content = references, IsExpanded = false, HorizontalAlignment = HorizontalAlignment.Stretch });
    }
    private void AddTitle(string text, double size = 16) => _details.Children.Add(new TextBlock { Text = text, FontWeight = FontWeight.SemiBold, FontSize = size, Margin = new Thickness(0, 8, 0, 0), TextWrapping = TextWrapping.Wrap });
    private void AddText(string text, bool secondary = false) => _details.Children.Add(new SelectableTextBlock { Text = text, TextWrapping = TextWrapping.Wrap, Opacity = secondary ? .7 : 1 });
    private void ReleaseBitmap() { _bitmap?.Dispose(); _bitmap = null; }
    private void ResizePage()
    {
        var narrow = Bounds.Width < 760;
        if (_narrow == narrow) return;
        _narrow = narrow;
        _filters.ColumnDefinitions = new ColumnDefinitions(narrow ? "*,*" : "*,Auto,Auto");
        _filters.RowDefinitions = new RowDefinitions(narrow ? "Auto,Auto" : "Auto");
        Grid.SetColumnSpan(_search, narrow ? 2 : 1);
        Grid.SetRow(_category, narrow ? 1 : 0); Grid.SetRow(_subtype, narrow ? 1 : 0);
        Grid.SetColumn(_category, narrow ? 0 : 1); Grid.SetColumn(_subtype, narrow ? 1 : 2);
        _category.HorizontalAlignment = _subtype.HorizontalAlignment = HorizontalAlignment.Stretch;
        _body.ColumnDefinitions = new ColumnDefinitions(narrow ? "*" : "280,8,*");
        _body.RowDefinitions = new RowDefinitions(narrow ? "200,8,*" : "*");
        Grid.SetColumn(_splitter, narrow ? 0 : 1); Grid.SetColumn(_detailScroll, narrow ? 0 : 2);
        Grid.SetRow(_splitter, narrow ? 1 : 0); Grid.SetRow(_detailScroll, narrow ? 2 : 0);
        _splitter.ResizeDirection = narrow ? GridResizeDirection.Rows : GridResizeDirection.Columns;
    }
}
