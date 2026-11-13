using System;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Styling;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using MFAAvalonia.Services;
using MFAAvalonia.Views.Pages;
using MFAAvalonia.Views.UserControls;
using MFAAvalonia.Views.UserControls.Settings;
using SukiUI.Controls;

namespace MFAAvalonia.Views.Mobile;

public partial class RootViewContent : UserControl
{
    private PageTourHost? _tour;
    private Flyout? _replayMenu;

    public RootViewContent()
    {
        InitializeComponent();
        Loaded += (_, _) =>
        {
            if (Design.IsDesignMode || _tour != null) return;
            // Save eligibility even when the first page is busy; restarting must not turn a new user into an upgrade.
            PageTourConfiguration.Current();
            var overlay = this.FindControl<TeachingTipOverlay>("TutorialOverlay")!;
            _tour = new PageTourHost(this, overlay, CurrentTourPage, TourBusy, PageTourConfiguration.Current);
        };
        Unloaded += (_, _) => { _replayMenu?.Hide(); _tour?.Dispose(); _tour = null; };
    }

    private SukiSideMenu? SideMenu => this.GetVisualDescendants().OfType<SukiSideMenu>().FirstOrDefault();
    private bool Visible(Control control) => TeachingTipOverlay.VisibleBounds(control, this) != null;
    private Control? Named(Control owner, params string[] names) => owner.GetVisualDescendants().OfType<Control>()
        .FirstOrDefault(c => names.Contains(c.Name) && Visible(c));

    private bool TourBusy()
    {
        // Read existing memory only. In particular, do not create a processor or game-management service here.
        lock (ScriptStudioGuard.SyncRoot)
            if (ScriptStudioGuard.IsCapturing || ScriptStudioGuard.GameManagementOperations != 0 ||
                !string.IsNullOrEmpty(ScriptStudioGuard.UpdateBlockReason)) return true;
        if (MaaProcessorManager.IsInstanceCreated && MaaProcessorManager.Instance.Instances.Any(p => p.IsTaskRunActive || p.IsConnecting)) return true;
        return TopLevel.GetTopLevel(this) is Window window && window.OwnedWindows.Any(w => w.IsVisible);
    }

    private PageTourPage? CurrentTourPage()
    {
        if (SideMenu is not { IsEffectivelyVisible: true } menu) return null;
        var scope = ConfigurationManager.Current.Config;
        if (menu.SelectedItem?.PageContent is ScriptStudioView script && Visible(script))
            return new PageTourPage(script.GuidePageId, script, script.DataContext, scope,
                PageTourCatalog.Steps(script.GuidePageId, step => script.FindGuideTarget(step, Visible)));
        if (menu.SelectedItem?.PageContent is InstanceContainerView home)
        {
            var task = home.GetVisualDescendants().OfType<TaskQueueView>().FirstOrDefault(Visible);
            if (task == null) return null;
            Control? Target(string step) => step switch
            {
                "connection" => Named(task, "DeviceSelectorPanel", "DeviceSelectorPanelCompact") ?? Named(task, "TopToolbar"),
                "task_list" => Named(task, "TaskListCard"),
                "task_settings" => Named(task, "SettingCard"),
                "game_management" => Named(task, "IntroductionCard"),
                "run_stop" => Named(task, "TaskToggleBorder", "TaskToggleBorder1"),
                "logs" => Named(task, "LogCard"),
                _ => null
            };
            return new PageTourPage("task_page", task, task.DataContext, scope, PageTourCatalog.Steps("task_page", Target));
        }
        if (menu.SelectedItem?.PageContent is SettingsView settings)
        {
            var version = settings.GetVisualDescendants().OfType<VersionUpdateSettingsUserControl>().FirstOrDefault();
            // Settings sections share a scrolling page. Merely constructing an offscreen section is not an entry.
            if (version == null || TeachingTipOverlay.VisibleBounds(version, this) is not { Height: >= 120 }) return null;
            Control? Target(string step) => Named(version, step switch
            { "version" => "TourVersion", "export_backup" => "TourExportBackup", "import_backup" => "TourImportBackup", _ => "" });
            return new PageTourPage("version_transfer", version, version.DataContext, scope, PageTourCatalog.Steps("version_transfer", Target));
        }
        return null;
    }

    // Kept for the framework's existing Settings/About tutorial button.
    public void TryStartTutorial()
    {
        if (_tour == null) return;
        var anchor = Named(this, "TutorialCard") ?? this;
        var choices = new StackPanel { Spacing = 8, MaxWidth = 300 };
        choices.Children.Add(new TextBlock { Text = "重看页面介绍", FontWeight = Avalonia.Media.FontWeight.SemiBold });
        if (TourBusy()) choices.Children.Add(new TextBlock { Text = "请在任务或录制结束后重看。", TextWrapping = Avalonia.Media.TextWrapping.Wrap });
        else foreach (var page in PageTourCatalog.Pages)
        {
            var button = new Button { Content = page.Label, HorizontalAlignment = Avalonia.Layout.HorizontalAlignment.Stretch };
            button.Click += async (_, _) =>
            {
                _replayMenu?.Hide();
                var tour = _tour;
                if (tour == null) return;
                try { await tour.ReplayAsync(page.Id, token => NavigateForReplayAsync(page.Id, token)); }
                catch (Exception error) { LoggerHelper.Warning("页面介绍未打开：" + error.Message); }
            };
            choices.Children.Add(button);
        }
        _replayMenu?.Hide();
        _replayMenu = new Flyout { Content = choices };
        _replayMenu.ShowAt(anchor);
    }

    private async Task<bool> NavigateForReplayAsync(string page, CancellationToken cancellation)
    {
        var tour = _tour;
        var menu = SideMenu;
        if (menu == null || TourBusy()) return false;
        var items = menu.Items.OfType<SukiSideMenuItem>().Concat(menu.FooterMenuItems.OfType<SukiSideMenuItem>()).ToArray();
        var script = items.Select(i => i.PageContent).OfType<ScriptStudioView>().FirstOrDefault();
        // The same dirty-document dialog as ordinary editor navigation; cancelling leaves the destination unopened.
        if (script != null && !await script.PrepareGuideNavigationAsync(page)) return false;
        if (cancellation.IsCancellationRequested || tour == null || !ReferenceEquals(tour, _tour) || TourBusy()) return false;
        var target = items.FirstOrDefault(i => page switch
        {
            "task_page" => i.PageContent is InstanceContainerView,
            "script_library" or "timeline_editor" => i.PageContent is ScriptStudioView,
            "version_transfer" => i.PageContent is SettingsView,
            _ => false
        });
        if (target == null) return false;
        menu.SelectedItem = target;
        if (target.PageContent is ScriptStudioView studio) studio.SelectGuidePage(page);
        if (target.PageContent is SettingsView settings)
        {
            await Dispatcher.UIThread.InvokeAsync(() =>
            {
                if (cancellation.IsCancellationRequested) return;
                settings.UpdateLayout();
                var version = settings.GetVisualDescendants().OfType<VersionUpdateSettingsUserControl>().FirstOrDefault();
                version?.FindControl<Control>("TourVersion")?.BringIntoView();
            }, DispatcherPriority.Loaded);
        }
        return true;
    }

    private void SwitchToDarkTheme(object? sender, RoutedEventArgs e) => Instances.GuiSettingsUserControlModel.BaseTheme = ThemeVariant.Dark;
    private void SwitchToLightTheme(object? sender, RoutedEventArgs e) => Instances.GuiSettingsUserControlModel.BaseTheme = ThemeVariant.Light;
}
