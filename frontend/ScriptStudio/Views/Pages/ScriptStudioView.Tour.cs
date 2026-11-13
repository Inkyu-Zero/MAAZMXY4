using System;
using System.Linq;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.VisualTree;

namespace MFAAvalonia.Views.Pages;

public sealed partial class ScriptStudioView
{
    public string GuidePageId => _tabs.SelectedIndex == 1 ? "timeline_editor" : "script_library";

    // Called only by an explicit replay choice, never by the tutorial's step buttons.
    public async Task<bool> PrepareGuideNavigationAsync(string page) =>
        !_busy && !_vm.Capturing && (page == GuidePageId || await EnsureSavedAsync());

    public void SelectGuidePage(string page) => _tabs.SelectedIndex = page == "timeline_editor" ? 1 : 0;

    public Control? FindGuideTarget(string step, Func<Control, bool> visible)
    {
        Control? Named(string name) => this.GetVisualDescendants().OfType<Control>().FirstOrDefault(c => c.Name == name && visible(c));
        return step switch
        {
            "library" => Named("TourScriptLibrary"),
            "recording" => _recordingExpander,
            "binding" => Named("ScriptTaskBinding") ?? Named("TourScriptLibrary"),
            "editor_tab" => _tabs.Items[1] as Control,
            "timeline" => _timelineScroll,
            "inspector" => _inspectorPane,
            "save_undo" => Named("TourScriptToolbar"),
            _ => null
        };
    }
}
