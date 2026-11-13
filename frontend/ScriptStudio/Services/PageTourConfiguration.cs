using System;
using MFAAvalonia.Configuration;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Services;

public static class PageTourConfiguration
{
    private static bool _freshInstallation;
    private static bool _captured;

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
