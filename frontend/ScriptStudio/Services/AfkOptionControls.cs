using System.Collections.Generic;
using Avalonia.Controls;

namespace MFAAvalonia.Services;

/// <summary>Disable option rows without overwriting their saved values or child idle bindings.</summary>
public sealed class AfkOptionControls
{
    private readonly List<Control> _rows = new();
    public bool Enabled { get; private set; }
    public void Reset() { _rows.Clear(); Enabled = false; }
    public void Add(Control row)
    {
        _rows.Add(row);
        row.IsEnabled = !Enabled;
    }
    public void SetEnabled(bool enabled)
    {
        Enabled = enabled;
        foreach (var row in _rows) row.IsEnabled = !enabled;
    }
}
