using Avalonia.Controls;
using MFAAvalonia.Helper;

namespace MFAAvalonia.Views.UserControls.Settings;

public partial class VersionUpdateSettingsUserControl : UserControl
{
    public VersionUpdateSettingsUserControl()
    {
        InitializeComponent();
        DataContext = Instances.VersionUpdateSettingsUserControlModel;
    }
}
