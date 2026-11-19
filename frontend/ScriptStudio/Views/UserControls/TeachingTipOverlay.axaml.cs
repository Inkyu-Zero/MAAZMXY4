using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Shapes;
using Avalonia.Input;
using Avalonia.Media;
using Avalonia.Threading;
using Avalonia.VisualTree;

namespace MFAAvalonia.Views.UserControls;

/// <summary>Page-local explanations. Placement and arrows reuse the pinned MFA teaching tip.</summary>
public partial class TeachingTipOverlay : UserControl
{
    private readonly Border _tipBorder, _overlayMask;
    private readonly Grid _overlayRoot;
    private readonly TextBlock _titleBlock, _descBlock, _countBlock;
    private readonly Button _prevButton, _nextButton, _closeButton;
    private readonly Polygon _arrow;
    private readonly DispatcherTimer _trackingTimer = new() { Interval = TimeSpan.FromMilliseconds(200) };
    private IReadOnlyList<TutorialStep> _steps = Array.Empty<TutorialStep>();
    private int _currentStep, _generation;
    private bool _attached;
    private Control? _previousFocus;
    private Func<bool>? _canContinue;
    public bool IsRunning { get; private set; }
    public int CurrentStep => _currentStep;
    public event Action<int>? StepChanged;
    public event Action<TourDismissal, int>? Dismissed;

    public TeachingTipOverlay()
    {
        Avalonia.Markup.Xaml.AvaloniaXamlLoader.Load(this);
        _overlayRoot = this.FindControl<Grid>("OverlayRoot")!;
        _overlayMask = this.FindControl<Border>("OverlayMask")!;
        _tipBorder = this.FindControl<Border>("TipBorder")!;
        _titleBlock = this.FindControl<TextBlock>("TipTitle")!;
        _descBlock = this.FindControl<TextBlock>("TipDescription")!;
        _countBlock = this.FindControl<TextBlock>("StepCount")!;
        _prevButton = this.FindControl<Button>("PrevButton")!;
        _nextButton = this.FindControl<Button>("NextButton")!;
        _closeButton = this.FindControl<Button>("CloseButton")!;
        _arrow = this.FindControl<Polygon>("TipArrow")!;
        _prevButton.Click += (_, _) => Move(-1);
        _nextButton.Click += (_, _) => { if (!CanContinue()) return; if (_currentStep == _steps.Count - 1) StopTutorial(TourDismissal.Completed); else Move(1); };
        _closeButton.Click += (_, _) => StopTutorial(TourDismissal.Skipped);
        _tipBorder.KeyDown += (_, e) =>
        {
            if (e.Key == Key.Escape && OwnsInput(e.Source)) { e.Handled = true; StopTutorial(TourDismissal.Skipped); }
        };
        _trackingTimer.Tick += (_, _) => PositionTip();
        AttachedToVisualTree += (_, _) => _attached = true;
        DetachedFromVisualTree += (_, _) => { _attached = false; StopTutorial(TourDismissal.Interrupted); };
    }

    public bool OwnsInput(object? source) => source is Visual visual &&
        (ReferenceEquals(visual, _tipBorder) || visual.GetVisualAncestors().Contains(_tipBorder));

    public void StartTutorial(IReadOnlyList<TutorialStep> steps, int index = 0, Func<bool>? canContinue = null)
    {
        if (!_attached || steps.Count == 0) return;
        StopTutorial(TourDismissal.Interrupted);
        _canContinue = canContinue;
        _steps = steps; _currentStep = Math.Clamp(index, 0, steps.Count - 1);
        _previousFocus = TopLevel.GetTopLevel(this)?.FocusManager?.GetFocusedElement() as Control;
        IsRunning = IsVisible = true;
        var generation = ++_generation;
        ShowStep();
        _trackingTimer.Start();
        Dispatcher.UIThread.Post(() =>
        {
            if (!IsRunning || !_attached || generation != _generation || !CanContinue()) return;
            PositionTip(); _nextButton.Focus();
        }, DispatcherPriority.Loaded);
    }

    private void Move(int delta)
    {
        if (!CanContinue()) return;
        _currentStep = Math.Clamp(_currentStep + delta, 0, _steps.Count - 1);
        ShowStep();
    }

    private void ShowStep()
    {
        var step = _steps[_currentStep];
        _titleBlock.Text = step.Title;
        _descBlock.Text = step.Description();
        _countBlock.Text = $"{_currentStep + 1} / {_steps.Count}";
        _prevButton.IsVisible = _currentStep > 0;
        if (!_prevButton.IsVisible && _prevButton.IsFocused) _nextButton.Focus();
        _nextButton.Content = _currentStep == _steps.Count - 1 ? "完成" : "下一步";
        _arrow.IsVisible = _overlayMask.IsVisible = false;
        StepChanged?.Invoke(_currentStep);
        var generation = _generation;
        Dispatcher.UIThread.Post(() => { if (IsRunning && _attached && generation == _generation) PositionTip(); }, DispatcherPriority.Render);
    }

    public void StopTutorial(TourDismissal reason)
    {
        ++_generation; _trackingTimer.Stop();
        var running = IsRunning;
        var restoreFocus = OwnsInput(TopLevel.GetTopLevel(this)?.FocusManager?.GetFocusedElement());
        IsRunning = IsVisible = false;
        _canContinue = null;
        _arrow.IsVisible = _overlayMask.IsVisible = false;
        if (running) Dismissed?.Invoke(reason, _currentStep);
        _steps = Array.Empty<TutorialStep>();
        if (restoreFocus && _previousFocus?.IsEffectivelyVisible == true) _previousFocus.Focus();
        _previousFocus = null;
    }

    private bool CanContinue()
    {
        if (!IsRunning) return false;
        if (_canContinue?.Invoke() != false) return true;
        StopTutorial(TourDismissal.Interrupted);
        return false;
    }

    // Intersect clipping ancestors, including ScrollViewer viewports; never scroll or expand a target.
    public static Rect? VisibleBounds(Control target, Control relativeTo)
    {
        if (!target.IsEffectivelyVisible || target.Bounds.Width < 1 || target.Bounds.Height < 1) return null;
        var transform = target.TransformToVisual(relativeTo);
        if (transform == null) return null;
        var bounds = new Rect(target.Bounds.Size).TransformToAABB(transform.Value);
        bounds = bounds.Intersect(new Rect(relativeTo.Bounds.Size));
        foreach (var parent in target.GetVisualAncestors())
        {
            if (!parent.IsVisible) return null;
            if (parent.ClipToBounds && parent.TransformToVisual(relativeTo) is { } matrix)
                bounds = bounds.Intersect(new Rect(parent.Bounds.Size).TransformToAABB(matrix));
        }
        return bounds.Width >= 1 && bounds.Height >= 1 ? bounds : null;
    }

    private void PositionTip()
    {
        if (!_attached || !CanContinue()) return;
        var width = _overlayRoot.Bounds.Width; var height = _overlayRoot.Bounds.Height;
        if (width < 24 || height < 80) return;
        _arrow.IsVisible = _overlayMask.IsVisible = false;
        _tipBorder.Width = Math.Min(350, width - 16);
        _tipBorder.MaxHeight = Math.Max(1, height - 16);
        var step = _steps[_currentStep];
        Control? target = null;
        try { target = step.FindTarget?.Invoke(); _descBlock.Text = step.Description(); }
        catch { } // A disappearing target falls back to the same explanation, never a business error.
        _tipBorder.Measure(new Size(_tipBorder.Width, _tipBorder.MaxHeight));
        var tipSize = _tipBorder.DesiredSize;
        var rect = target == null ? null : VisibleBounds(target, _overlayRoot);
        var x = Math.Max(8, (width - tipSize.Width) / 2);
        var y = Math.Max(8, (height - tipSize.Height) / 2);
        if (rect is { } visible)
        {
            var cutout = visible.Inflate(3).Intersect(new Rect(_overlayRoot.Bounds.Size));
            var placement = CalculateBestPlacement(cutout, tipSize, width, height, step.PreferredPlacement);
            PositionArrow(placement, cutout, tipSize, out x, out y);
            x = Math.Clamp(x, 8, Math.Max(8, width - tipSize.Width - 8));
            y = Math.Clamp(y, 8, Math.Max(8, height - tipSize.Height - 8));
            if (!new Rect(x, y, tipSize.Width, tipSize.Height).Intersects(cutout))
            {
                Canvas.SetLeft(_overlayMask, cutout.X); Canvas.SetTop(_overlayMask, cutout.Y);
                _overlayMask.Width = cutout.Width; _overlayMask.Height = cutout.Height;
                _overlayMask.IsVisible = true;
                AdjustArrowToTip(placement, cutout, x, y, tipSize);
            }
            else _arrow.IsVisible = false;
        }
        Canvas.SetLeft(_tipBorder, x); Canvas.SetTop(_tipBorder, y);
    }

    private TipPlacement CalculateBestPlacement(
        Rect targetBounds,
        Size tipSize,
        double canvasW,
        double canvasH,
        TipPlacement preferred)
    {
        if (HasSpace(preferred, targetBounds, tipSize, canvasW, canvasH))
            return preferred;

        TipPlacement[] fallbacks =
            [TipPlacement.Bottom, TipPlacement.Top, TipPlacement.Left, TipPlacement.Right];
        foreach (var p in fallbacks)
        {
            if (HasSpace(p, targetBounds, tipSize, canvasW, canvasH))
                return p;
        }

        return TipPlacement.Bottom;
    }

    private static bool HasSpace(TipPlacement placement, Rect target, Size tip, double cw, double ch)
    {
        const double arrowSize = 12;
        return placement switch
        {
            TipPlacement.Top => target.Y - tip.Height - arrowSize > 0,
            TipPlacement.Bottom => target.Bottom + tip.Height + arrowSize < ch,
            TipPlacement.Left => target.X - tip.Width - arrowSize > 0,
            TipPlacement.Right => target.Right + tip.Width + arrowSize < cw,
            _ => true
        };
    }

    /// <summary>
    /// Calculates initial tip position and sets arrow shape for the given placement.
    /// The arrow fills the space between target and tip (arrowSize == gap).
    /// </summary>
    private void PositionArrow(TipPlacement placement,
        Rect targetBounds,
        Size tipSize,
        out double tipX,
        out double tipY)
    {
        const double arrowSize = 12;

        if (_arrow == null)
        {
            tipX = tipY = 0;
            return;
        }

        switch (placement)
        {
            case TipPlacement.Bottom:
                tipX = targetBounds.Center.X - tipSize.Width / 2;
                tipY = targetBounds.Bottom + arrowSize;
                _arrow.Points =
                [
                    new Point(0, arrowSize),
                    new Point(arrowSize, 0),
                    new Point(arrowSize * 2, arrowSize)
                ];
                _arrow.IsVisible = true;
                break;

            case TipPlacement.Top:
                tipX = targetBounds.Center.X - tipSize.Width / 2;
                tipY = targetBounds.Y - tipSize.Height - arrowSize;
                _arrow.Points =
                [
                    new Point(0, 0),
                    new Point(arrowSize, arrowSize),
                    new Point(arrowSize * 2, 0)
                ];
                _arrow.IsVisible = true;
                break;

            case TipPlacement.Left:
                tipX = targetBounds.X - tipSize.Width - arrowSize;
                tipY = targetBounds.Center.Y - tipSize.Height / 2;
                _arrow.Points =
                [
                    new Point(0, 0),
                    new Point(arrowSize, arrowSize),
                    new Point(0, arrowSize * 2)
                ];
                _arrow.IsVisible = true;
                break;

            case TipPlacement.Right:
                tipX = targetBounds.Right + arrowSize;
                tipY = targetBounds.Center.Y - tipSize.Height / 2;
                _arrow.Points =
                [
                    new Point(arrowSize, 0),
                    new Point(0, arrowSize),
                    new Point(arrowSize, arrowSize * 2)
                ];
                _arrow.IsVisible = true;
                break;

            default:
                tipX = tipY = 0;
                _arrow.IsVisible = false;
                break;
        }
    }

    /// <summary>
    /// Positions the arrow so its base is flush with the (clamped) tip border edge.
    /// The arrow overlaps the tip by a few pixels to eliminate any visible seam.
    /// Arrow lateral position tracks the target center, clamped within tip bounds
    /// (away from rounded corners).
    /// </summary>
    private void AdjustArrowToTip(TipPlacement placement,
        Rect targetBounds,
        double tipX,
        double tipY,
        Size tipSize)
    {
        if (_arrow == null) return;

        const double arrowSize = 12;
        const double cornerMargin = 20; // keep arrow away from rounded corners (CornerRadius=10)
        const double overlap = 3; // overlap into tip to eliminate anti-aliasing seam

        switch (placement)
        {
            case TipPlacement.Bottom:
            {
                var arrowX = Clamp(targetBounds.Center.X - arrowSize,
                    tipX + cornerMargin, tipX + tipSize.Width - cornerMargin - arrowSize * 2);
                Canvas.SetLeft(_arrow, arrowX);
                Canvas.SetTop(_arrow, tipY - arrowSize + overlap);
                break;
            }
            case TipPlacement.Top:
            {
                var arrowX = Clamp(targetBounds.Center.X - arrowSize,
                    tipX + cornerMargin, tipX + tipSize.Width - cornerMargin - arrowSize * 2);
                Canvas.SetLeft(_arrow, arrowX);
                Canvas.SetTop(_arrow, tipY + tipSize.Height - overlap);
                break;
            }
            case TipPlacement.Right:
            {
                var arrowY = Clamp(targetBounds.Center.Y - arrowSize,
                    tipY + cornerMargin, tipY + tipSize.Height - cornerMargin - arrowSize * 2);
                Canvas.SetLeft(_arrow, tipX - arrowSize + overlap);
                Canvas.SetTop(_arrow, arrowY);
                break;
            }
            case TipPlacement.Left:
            {
                var arrowY = Clamp(targetBounds.Center.Y - arrowSize,
                    tipY + cornerMargin, tipY + tipSize.Height - cornerMargin - arrowSize * 2);
                Canvas.SetLeft(_arrow, tipX + tipSize.Width - overlap);
                Canvas.SetTop(_arrow, arrowY);
                break;
            }
        }
    }

    private static double Clamp(double value, double min, double max)
    {
        if (min > max) return (min + max) / 2; // fallback: center if range is invalid
        return Math.Max(min, Math.Min(value, max));
    }

}
public enum TourDismissal { Completed, Skipped, Interrupted }
public sealed class TutorialStep
{
    public string Id { get; init; } = "";
    public string Title { get; init; } = "";
    public Func<string> Description { get; init; } = () => "";
    public Func<Control?>? FindTarget { get; init; }
    public TipPlacement PreferredPlacement { get; init; } = TipPlacement.Bottom;
}
public enum TipPlacement { Top, Bottom, Left, Right }
