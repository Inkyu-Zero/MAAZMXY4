using System;
using System.Collections.Generic;
using System.Linq;
using MFAAvalonia.Configuration;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Services;

public static class PageTourConfiguration
{
    private static bool _freshInstallation;
    private static bool _captured;
    public const string ResetKey = "PageTours.ResetOnRestart";
    private static readonly HashSet<string> OpenedScopes = new(StringComparer.OrdinalIgnoreCase);

    public static bool IsResetPending => ConfigurationManager.Current.Config.TryGetValue(ResetKey, out var pending) && pending is true;

    public static void RequestReset()
    {
        var config = ConfigurationManager.Current;
        // A new request may never be consumed by a later page/configuration visit in this process.
        OpenedScopes.Add(config.FileName);
        config.SetValue(ResetKey, true);
    }

    // Run after staged restores, before the host creates its first configuration files.
    public static void CaptureFirstRun(string directory, string globalFile)
    {
        if (_captured) return;
        _captured = true;
        _freshInstallation = PageTourProgress.IsFreshConfiguration(directory, globalFile);
    }

    public static PageTourProgress Current()
    {
        var config = ConfigurationManager.Current;
        var values = config.Config;
        var filename = config.FileName;
        var saved = values.TryGetValue(PageTourProgress.Key, out var value) ? value?.ToString() : null;
        if (OpenedScopes.Add(filename) && IsResetPending)
        {
            try
            {
                var reset = PageTourProgress.ResetPages(saved, PageTourCatalog.Pages.Select(page => page.Id));
                config.SetValue(PageTourProgress.Key, reset);
                config.SetValue(ResetKey, false);
                saved = reset;
            }
            catch (Exception error) { LoggerHelper.Warning("教程状态重置未保存：" + error.Message); }
        }
        var progress = new PageTourProgress(saved, _freshInstallation,
            config.ContainsKey(ConfigurationKeys.HasCompletedFirstUseTutorial), json =>
            {
                if (ReferenceEquals(ConfigurationManager.Current, config) && ReferenceEquals(values, config.Config) && filename == config.FileName)
                    config.SetValue(PageTourProgress.Key, json);
            }, message => LoggerHelper.Warning(message));
        progress.Initialize();
        return progress;
    }
}
