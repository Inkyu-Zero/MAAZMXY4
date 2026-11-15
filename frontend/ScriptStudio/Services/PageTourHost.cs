using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Threading;
using MFAAvalonia.Views.UserControls;

namespace MFAAvalonia.Services;

public sealed record PageTourPage(string Id, Control Owner, object? Context, object Scope, IReadOnlyList<TutorialStep> Steps)
{
    public bool SameEntry(PageTourPage? other) => other != null && Id == other.Id &&
        ReferenceEquals(Owner, other.Owner) && ReferenceEquals(Context, other.Context) && ReferenceEquals(Scope, other.Scope);
}

/// <summary>Observes existing UI state. Only explicit replay may ask the host to navigate.</summary>
public sealed class PageTourHost : IDisposable
{
    private readonly Control _root;
    private readonly TeachingTipOverlay _overlay;
    private readonly Func<PageTourPage?> _page;
    private readonly Func<bool> _busy;
    private readonly Func<bool> _obscured;
    private readonly Func<PageTourProgress> _progress;
    private readonly DispatcherTimer _watch = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private PageTourPage? _entry, _active;
    private PageTourProgress? _activeProgress;
    private PageTourPage? _pending;
    private int? _resumeStep;
    private bool _disposed, _queued, _replaying;
    private int _navigationVersion;
    private CancellationTokenSource? _replayCancellation;

    public PageTourHost(Control root, TeachingTipOverlay overlay, Func<PageTourPage?> page,
        Func<bool> busy, Func<PageTourProgress> progress, Func<bool>? obscured = null)
    {
        _root = root; _overlay = overlay; _page = page; _busy = busy; _progress = progress;
        _obscured = obscured ?? (() => false);
        root.LayoutUpdated += LayoutUpdated;
        root.DetachedFromVisualTree += Detached;
        root.AddHandler(InputElement.PointerPressedEvent, OutsidePointer, RoutingStrategies.Tunnel, true);
        root.AddHandler(InputElement.KeyDownEvent, OutsideKey, RoutingStrategies.Tunnel, true);
        overlay.StepChanged += SaveStep; overlay.Dismissed += Dismissed;
        _watch.Tick += Watch;
        QueueObserve();
    }

    private void LayoutUpdated(object? sender, EventArgs e) => QueueObserve();
    private void Detached(object? sender, VisualTreeAttachmentEventArgs e) => Dispose();
    private void Watch(object? sender, EventArgs e) => Observe();
    private void QueueObserve()
    {
        if (_disposed || _queued) return;
        _queued = true;
        Dispatcher.UIThread.Post(() => { _queued = false; if (!_disposed) Observe(); }, DispatcherPriority.Background);
    }

    public void Observe()
    {
        if (_disposed) return;
        var page = _page();
        if (page?.SameEntry(_entry) == true)
        {
            if (_busy()) { CancelPending(); _overlay.StopTutorial(TourDismissal.Interrupted); return; }
            if (_pending != null && !_obscured())
            {
                var index = _resumeStep;
                CancelPending(); Start(page, replay: false, resumeStep: index);
            }
            else if (_overlay.IsRunning && _obscured()) Pause(page);
            return;
        }
        CancelPending();
        _overlay.StopTutorial(TourDismissal.Interrupted);
        _entry = page;
        // An occupied entry is consumed for this visit, not marked completed. No late popup when work ends.
        if (page != null && !_replaying && !_busy())
        {
            if (_obscured()) { _pending = page; _watch.Start(); }
            else Start(page, replay: false);
        }
    }

    public static bool HasModalWindow(Control root) => TopLevel.GetTopLevel(root) is Window window &&
        window.OwnedWindows.Any(w => w.IsVisible && w.IsDialog);

    private void CancelPending() { _pending = null; _resumeStep = null; _watch.Stop(); }

    private void Pause(PageTourPage page)
    {
        _pending = page; _resumeStep = _overlay.CurrentStep;
        _overlay.StopTutorial(TourDismissal.Interrupted);
        _watch.Start();
    }

    private void Start(PageTourPage page, bool replay, int? resumeStep = null)
    {
        var progress = _progress();
        var index = resumeStep ?? progress.StartIndex(page.Id, page.Steps.Select(s => s.Id).ToArray(), replay);
        if (index == null) return;
        _active = page; _activeProgress = progress;
        _overlay.StartTutorial(page.Steps, index.Value, () =>
        {
            if (_disposed || !page.SameEntry(_page()) || _busy()) return false;
            if (!_obscured()) return true;
            Pause(page); return false;
        });
        if (_overlay.IsRunning) _watch.Start();
        else { _active = null; _activeProgress = null; }
    }

    private void SaveStep(int index)
    {
        if (_active != null) _activeProgress?.Record(_active.Id, _active.Steps[index].Id, "reading");
    }

    private void Dismissed(TourDismissal reason, int index)
    {
        _watch.Stop();
        if (_active != null)
            _activeProgress?.Record(_active.Id, _active.Steps[index].Id,
                reason == TourDismissal.Completed ? "completed" : reason == TourDismissal.Skipped ? "skipped" : "reading");
        _active = null; _activeProgress = null;
    }

    private void OutsidePointer(object? sender, PointerPressedEventArgs e) => OutsideInput(e.Source);
    private void OutsideKey(object? sender, KeyEventArgs e) => OutsideInput(e.Source);
    private void OutsideInput(object? source)
    {
        if (_overlay.OwnsInput(source)) return;
        ++_navigationVersion;
        _replayCancellation?.Cancel();
        CancelPending();
        _overlay.StopTutorial(TourDismissal.Interrupted);
        // Never handle the input: the business control keeps its original behavior.
    }

    public async Task<bool> ReplayAsync(string id, Func<CancellationToken, Task<bool>> navigate)
    {
        if (_disposed || _replaying || _busy() || _obscured()) return false;
        _replaying = true;
        var request = ++_navigationVersion;
        using var cancellation = new CancellationTokenSource();
        _replayCancellation = cancellation;
        CancelPending();
        _overlay.StopTutorial(TourDismissal.Interrupted);
        try
        {
            if (!await navigate(cancellation.Token)) return false;
            // Bounded layout wait only for explicitly requested navigation. No business operation is awaited here.
            for (var attempt = 0; attempt < 8; attempt++)
            {
                await Task.Delay(50);
                if (_disposed || request != _navigationVersion || _busy()) return false;
                Observe();
                if (_entry?.Id != id || _obscured()) continue;
                Start(_entry, replay: true);
                return _overlay.IsRunning;
            }
            return false;
        }
        finally
        {
            if (!_disposed) Observe(); // Consume a cancelled destination while automatic display is suspended.
            _replayCancellation = null;
            _replaying = false;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true; ++_navigationVersion;
        CancelPending();
        _replayCancellation?.Cancel();
        _overlay.StopTutorial(TourDismissal.Interrupted);
        _watch.Stop(); _watch.Tick -= Watch;
        _root.LayoutUpdated -= LayoutUpdated; _root.DetachedFromVisualTree -= Detached;
        _root.RemoveHandler(InputElement.PointerPressedEvent, OutsidePointer);
        _root.RemoveHandler(InputElement.KeyDownEvent, OutsideKey);
        _overlay.StepChanged -= SaveStep; _overlay.Dismissed -= Dismissed;
    }
}
